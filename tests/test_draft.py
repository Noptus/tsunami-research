"""Offline tests for deterministic weekly forecast *proposal* generation.

No network, filesystem writes, shell, or git. ``market_baseline`` is patched with
a synthetic frequency summary so the FRED fetch never runs; a dedicated test also
exercises the missing-anchor / missing-endpoint failure path by making the patched
baseline raise.

Run with: python -m unittest discover -s tests -p 'test_draft.py'
"""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from tsunami import draft
from tsunami.cpi import ReplayError


# A synthetic baseline summary. It carries only a frequency, a source URL, and a
# digest: never a raw index level, matching the no-raw-values rule.
def fake_baseline(hits=71, samples=488,
                  url="https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500&cosd=2016-11-15&coed=2026-11-13",
                  sha="80bc4bcbdcb196716c2cca223c074e6f94f91e98675eb76904e55a343101181a"):
    def _factory(forecast):
        market = forecast["market"]
        return {
            "probability": (hits + 1) / (samples + 2),
            "hits": hits,
            "samples": samples,
            "range_start": "2016-11-15",
            "range_end": market["start_date"],
            "method": "synthetic test baseline; zero market levels stored",
            "source_url": url,
            "sha256": sha,
        }
    return _factory


# A clock inside the valid issuance window for the 2026-11-20 week:
# after the 2026-11-13 close (16:00 ET) and before Monday 2026-11-16 09:30 ET.
WINDOW = datetime(2026, 11, 14, 21, 31, 10, tzinfo=timezone.utc)
WEEK = "2026-11-20"


class ProposeTests(unittest.TestCase):
    def propose(self, week=WEEK, now=WINDOW, baseline=None):
        with patch.object(draft, "market_baseline", baseline or fake_baseline()):
            return draft.propose(week, now=now)

    # --- happy path -------------------------------------------------------
    def test_proposes_symmetric_pair_with_shared_baseline(self):
        result = self.propose()
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result["week_ending"], WEEK)
        self.assertEqual(len(result["records"]), 2)
        up, down = result["records"]
        self.assertEqual(up["market"]["direction"], "gte")
        self.assertEqual(down["market"]["direction"], "lte")
        self.assertEqual(up["market"]["threshold_pct"], 2.0)
        self.assertEqual(down["market"]["threshold_pct"], -2.0)
        # Symmetric +/-2% thresholds and one Friday-to-Friday window.
        for record in result["records"]:
            self.assertEqual(record["market"]["start_date"], "2026-11-13")
            self.assertEqual(record["market"]["end_date"], WEEK)
            self.assertEqual(record["market"]["series"], "SP500")
        # Both tails cite the identical baseline source digest and URL.
        self.assertEqual(up["baseline"]["sha256"], down["baseline"]["sha256"])
        self.assertEqual(up["evidence"][0]["sha256"], down["evidence"][0]["sha256"])
        self.assertEqual(result["baseline_source"]["sha256"], up["baseline"]["sha256"])

    def test_dates_appear_in_ids(self):
        result = self.propose()
        for record in result["records"]:
            self.assertTrue(record["id"].endswith(WEEK))
        self.assertNotEqual(result["records"][0]["id"], result["records"][1]["id"])

    def test_baseline_used_as_forecast_and_no_raw_values(self):
        result = self.propose()
        base = fake_baseline()({"market": {"start_date": "2026-11-13"}})
        for record in result["records"]:
            self.assertEqual(record["baseline_probability"], base["probability"])
            self.assertEqual(record["probability"], base["probability"])
            # No raw SP500 level stored anywhere in the record's baseline block.
            self.assertNotIn("values", record["baseline"])
            self.assertNotIn("levels", record["baseline"])
            self.assertEqual(set(record["baseline"]) & {"close", "price", "level"}, set())

    def test_timestamps_are_observation_then_issue(self):
        result = self.propose()
        for record in result["records"]:
            # as_of (data observed) and issued both stamp the injected clock.
            self.assertEqual(record["as_of_at"], "2026-11-14T21:31:10+00:00")
            self.assertEqual(record["issued_at"], "2026-11-14T21:31:10+00:00")

    def test_evidence_marks_title_and_availability(self):
        result = self.propose()
        for record in result["records"]:
            evidence = record["evidence"][0]
            self.assertIn("FRED SP500", evidence["title"])
            self.assertIn("baseline snapshot", evidence["title"])
            self.assertEqual(evidence["available_at"], record["as_of_at"])

    def test_no_fabricated_causal_theory(self):
        result = self.propose()
        for record in result["records"]:
            # The draft explicitly defers any contemporaneous thesis to editors.
            self.assertIn("pending editorial research", record["rationale"])

    def test_default_void_semantics_present(self):
        result = self.propose()
        for record in result["records"]:
            self.assertIn("Void if", record["resolution_rule"])
            self.assertIn("2026-12-01", record["resolution_rule"])

    # --- non-Friday -------------------------------------------------------
    def test_non_friday_week_abstains(self):
        result = self.propose(week="2026-11-19")  # a Thursday
        self.assertEqual(result["status"], "abstained")
        self.assertEqual(result["records"], [])
        self.assertIn("Friday", result["reason"])

    # --- timing / opening cutoff -----------------------------------------
    def test_before_anchor_close_abstains(self):
        # 2026-11-13 15:59 ET is before the anchor close.
        too_early = datetime(2026, 11, 13, 20, 59, tzinfo=timezone.utc)
        result = self.propose(now=too_early)
        self.assertEqual(result["status"], "abstained")
        self.assertIn("Too early", result["reason"])

    def test_at_or_after_monday_lock_abstains(self):
        # Monday 2026-11-16 09:30 ET == 14:30 UTC (EST).
        at_lock = datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc)
        result = self.propose(now=at_lock)
        self.assertEqual(result["status"], "abstained")
        self.assertIn("Deadline passed", result["reason"])

    def test_just_inside_window_proposes(self):
        just_after = datetime(2026, 11, 13, 21, 0, 1, tzinfo=timezone.utc)  # 16:00:01 ET
        just_before = datetime(2026, 11, 16, 14, 29, 59, tzinfo=timezone.utc)
        for clock in (just_after, just_before):
            with self.subTest(clock=clock):
                self.assertEqual(self.propose(now=clock)["status"], "proposed")

    def test_naive_clock_abstains(self):
        result = self.propose(now=datetime(2026, 11, 14, 21, 31, 10))
        self.assertEqual(result["status"], "abstained")
        self.assertIn("timezone-aware", result["reason"])

    # --- missing anchor / missing endpoints ------------------------------
    def test_missing_anchor_abstains(self):
        def raising(forecast):
            raise ValueError("Insufficient Friday close pairs")
        result = self.propose(baseline=raising)
        self.assertEqual(result["status"], "abstained")
        self.assertIn("Baseline unavailable", result["reason"])
        self.assertIn("unavailable", result["reason"])

    def test_missing_endpoint_fetch_error_abstains(self):
        def raising(forecast):
            raise ReplayError("Unexpected SP500 date")
        result = self.propose(baseline=raising)
        self.assertEqual(result["status"], "abstained")
        self.assertEqual(result["records"], [])

    # --- holiday week -----------------------------------------------------
    def test_holiday_week_requires_editorial_input(self):
        # Week ending 2026-11-27: window 2026-11-20..2026-11-27 contains
        # Thanksgiving 2026-11-26.
        result = self.propose(week="2026-11-27",
                              now=datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "abstained")
        self.assertIn("holiday", result["reason"].lower())
        self.assertIn("early close", result["reason"].lower())

    # --- baseline source mismatch ----------------------------------------
    def test_mismatched_baseline_digest_fails_closed(self):
        calls = {"n": 0}

        def alternating(forecast):
            calls["n"] += 1
            sha = "a" * 64 if calls["n"] == 1 else "b" * 64
            return fake_baseline(sha=sha)(forecast)

        result = self.propose(baseline=alternating)
        self.assertEqual(result["status"], "abstained")
        self.assertIn("digest", result["reason"].lower())


class LedgerContractTests(unittest.TestCase):
    """The emitted draft must satisfy the published ledger issue contract."""

    def test_proposal_passes_validate_issue(self):
        from tsunami.ledger import validate_issue
        with patch.object(draft, "market_baseline", fake_baseline()):
            result = draft.propose(WEEK, now=WINDOW)
        # Should not raise.
        validated = validate_issue([dict(r) for r in result["records"]], WEEK)
        self.assertEqual(len(validated), 2)


if __name__ == "__main__":
    unittest.main()
