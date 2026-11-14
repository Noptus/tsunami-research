"""Synthetic ledger contract tests; no network, git, or real-source claims.

Run with: python -m unittest discover -s tests -p 'test_ledger.py'

Every timestamp is a fixed historical date. Network boundaries (FRED fetches
and ``utcnow``) are replaced with mocks so no socket is ever opened.
"""

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tsunami import ledger


# A Friday week-ending anchors every fixture. 2024-03-15 is a Friday. The market
# question observes the 2024-03-08 -> 2024-03-15 Friday close pair (exactly one
# week apart, ending on the week ending). Its lock window runs from the Friday
# 2024-03-08 16:00 ET close to Monday 2024-03-11 09:30 ET (= 13:30Z), so the one
# shared issuance time is a Saturday-evening weekend issue before that Monday.
WEEK = "2024-03-15"
ISSUED = "2024-03-09T15:00:00+00:00"       # Saturday, inside the market lock window
AS_OF = "2024-03-09T14:00:00+00:00"
DEADLINE = "2024-03-15T20:00:00+00:00"     # 16:00 ET on the local week ending


def event_forecast(**overrides):
    forecast = {
        "id": "nvidia-guidance-raise",
        "week_ending": WEEK,
        "as_of_at": AS_OF,
        "issued_at": ISSUED,
        "deadline_at": DEADLINE,
        "mode": "prospective",
        "kind": "binary_event",
        "question": "Will NVIDIA raise guidance?",
        "rationale": "Channel checks suggest strong demand.",
        "resolution_rule": "Resolves yes on an official guidance raise.",
        "probability": 0.6,
        "baseline_probability": 0.5,
        "risks": ["Macro shock could delay the announcement."],
        "evidence": [{
            "title": "Prior earnings call transcript",
            "url": "https://investor.nvidia.com/transcript",
            "available_at": "2024-03-01T00:00:00+00:00",
        }],
        "resolution_hosts": ["www.sec.gov", "investor.nvidia.com"],
        "resolve_no_not_before": "2024-03-22T20:00:00+00:00",
        "void_not_before": "2024-03-29T20:00:00+00:00",
    }
    forecast.update(overrides)
    return forecast


def market_forecast(**overrides):
    forecast = {
        "id": "sp500-weekly-up",
        "week_ending": WEEK,
        "as_of_at": AS_OF,
        "issued_at": ISSUED,
        "deadline_at": DEADLINE,
        "mode": "prospective",
        "kind": "market_threshold",
        "question": "Will the S&P 500 rise over the week?",
        "rationale": "Momentum appears positive.",
        "resolution_rule": "Resolves yes on a non-negative weekly return.",
        "probability": 0.55,
        "baseline_probability": 0.5,
        "risks": ["A sell-off could reverse the trend."],
        "evidence": [{
            "title": "FRED SP500 series",
            "url": "https://fred.stlouisfed.org/series/SP500",
            "available_at": "2024-03-01T00:00:00+00:00",
        }],
        "market": {
            "series": "SP500",
            "start_date": "2024-03-08",
            "end_date": "2024-03-15",
            "direction": "gte",
            "threshold_pct": 0.0,
        },
    }
    forecast.update(overrides)
    return forecast


class ValidateIssueTests(unittest.TestCase):
    def test_accepts_event_and_market_issue(self):
        issue = [event_forecast(), market_forecast()]
        self.assertIs(ledger.validate_issue(issue, WEEK), issue)

    def test_week_must_be_friday(self):
        with self.assertRaisesRegex(ValueError, "Friday"):
            ledger.validate_issue([event_forecast(week_ending="2024-03-14",
                                                  deadline_at="2024-03-14T20:00:00+00:00")],
                                   "2024-03-14")

    def test_lookahead_evidence_rejected(self):
        # A source "available" after the as-of instant is lookahead.
        forecast = event_forecast(evidence=[{
            "title": "Future filing",
            "url": "https://www.sec.gov/filing",
            "available_at": "2024-03-11T12:00:01+00:00",
        }])
        with self.assertRaisesRegex(ValueError, "Lookahead"):
            ledger.validate_issue([forecast], WEEK)

    def test_lookahead_timing_order_rejected(self):
        # as_of must precede issuance which must precede the deadline.
        forecast = event_forecast(as_of_at="2024-03-11T13:00:01+00:00")
        with self.assertRaisesRegex(ValueError, "Invalid issue timing"):
            ledger.validate_issue([forecast], WEEK)

    def test_issue_locked_more_than_one_week_early(self):
        early = "2024-03-07T13:00:00+00:00"  # more than 7 days before week ending
        forecast = event_forecast(as_of_at=early, issued_at=early)
        with self.assertRaisesRegex(ValueError, "more than one week"):
            ledger.validate_issue([forecast], WEEK)

    def test_single_shared_issuance_time(self):
        other = market_forecast(issued_at="2024-03-11T14:00:00+00:00")
        with self.assertRaisesRegex(ValueError, "share an issuance time"):
            ledger.validate_issue([event_forecast(), other], WEEK)

    def test_market_must_lock_before_monday_0930_et(self):
        # Monday 09:30 ET for a 2024-03-01 anchor is 2024-03-04T14:30Z. Issue and
        # deadline shift to the following week so issuance can be made too late.
        late_week = "2024-03-08"
        forecast = market_forecast(
            week_ending=late_week,
            as_of_at="2024-03-04T14:30:00+00:00",
            issued_at="2024-03-04T14:30:00+00:00",
            deadline_at="2024-03-08T21:00:00+00:00",
            market={"series": "SP500", "start_date": "2024-03-01", "end_date": "2024-03-08",
                    "direction": "gte", "threshold_pct": 0.0},
        )
        with self.assertRaisesRegex(ValueError, "before Monday"):
            ledger.validate_issue([forecast], late_week)

    def test_market_locks_just_before_monday_0930_et(self):
        early_week = "2024-03-08"
        forecast = market_forecast(
            week_ending=early_week,
            as_of_at="2024-03-04T14:29:00+00:00",
            issued_at="2024-03-04T14:29:00+00:00",
            deadline_at="2024-03-08T21:00:00+00:00",
            market={"series": "SP500", "start_date": "2024-03-01", "end_date": "2024-03-08",
                    "direction": "gte", "threshold_pct": 0.0},
        )
        self.assertEqual(len(ledger.validate_issue([forecast], early_week)), 1)

    def test_negative_barrier_before_void_barrier(self):
        forecast = event_forecast(
            resolve_no_not_before="2024-03-29T20:00:00+00:00",
            void_not_before="2024-03-22T20:00:00+00:00",
        )
        with self.assertRaisesRegex(ValueError, "Void barrier precedes"):
            ledger.validate_issue([forecast], WEEK)

    def test_barrier_cannot_precede_deadline(self):
        forecast = event_forecast(resolve_no_not_before="2024-03-15T19:00:00+00:00")
        with self.assertRaisesRegex(ValueError, "barrier precedes deadline"):
            ledger.validate_issue([forecast], WEEK)

    def test_named_event_requires_authorities(self):
        with self.assertRaisesRegex(ValueError, "resolution authorities"):
            ledger.validate_issue([event_forecast(resolution_hosts=[])], WEEK)
        with self.assertRaisesRegex(ValueError, "named event authority"):
            ledger.validate_issue([event_forecast(resolution_hosts=["not a host"])], WEEK)

    def test_cannot_mix_modes(self):
        archived = market_forecast(id="replay-market", mode="archived_replay")
        with self.assertRaisesRegex(ValueError, "mix archived and prospective"):
            ledger.validate_issue([event_forecast(), archived], WEEK)

    def test_duplicate_forecast_id(self):
        with self.assertRaisesRegex(ValueError, "Duplicate forecast"):
            ledger.validate_issue([event_forecast(), event_forecast()], WEEK)


class DigestAndWriteTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_digest_matches_exact_file_bytes(self):
        issue = [event_forecast()]
        path = self.root / "forecasts" / f"{WEEK}.json"
        ledger.write_new(path, issue)
        self.assertEqual(path.read_bytes(), ledger.json_bytes(issue))
        self.assertEqual(ledger.digest(issue), ledger.digest(ledger.read_json(path)))

    def test_identical_rewrite_is_idempotent(self):
        path = self.root / "r.json"
        ledger.write_new(path, {"a": 1})
        ledger.write_new(path, {"a": 1})  # no error on identical retry
        self.assertEqual(ledger.read_json(path), {"a": 1})

    def test_conflicting_rewrite_is_sealed(self):
        path = self.root / "r.json"
        ledger.write_new(path, {"a": 1})
        with self.assertRaisesRegex(ValueError, "already sealed"):
            ledger.write_new(path, {"a": 2})
        self.assertEqual(ledger.read_json(path), {"a": 1})  # ledger untouched


class PublicationReceiptTests(unittest.TestCase):
    def receipt(self, issue, **overrides):
        receipt = {
            "url": f"https://github.com/Noptus/tsunami-research/releases/tag/forecast-{WEEK}",
            "published_at": "2024-03-10T13:30:00+00:00",
            "immutable": True,
            "issue_sha256": ledger.digest(issue),
            "release_id": 101,
            "asset_id": 202,
        }
        receipt.update(overrides)
        return receipt

    def test_valid_receipt(self):
        issue = [event_forecast(), market_forecast()]
        ledger.validate_publication(self.receipt(issue), issue, WEEK)

    def test_requires_matching_released_tag(self):
        issue = [event_forecast()]
        bad = self.receipt(issue, url="https://github.com/Noptus/tsunami-research/releases/tag/forecast-2024-03-08")
        with self.assertRaisesRegex(ValueError, "dated GitHub release"):
            ledger.validate_publication(bad, issue, WEEK)

    def test_digest_mismatch_rejected(self):
        issue = [event_forecast()]
        with self.assertRaisesRegex(ValueError, "digest does not match"):
            ledger.validate_publication(self.receipt(issue, issue_sha256="0" * 64), issue, WEEK)

    def test_mutable_release_rejected(self):
        issue = [event_forecast()]
        with self.assertRaisesRegex(ValueError, "immutable"):
            ledger.validate_publication(self.receipt(issue, immutable=False), issue, WEEK)

    def test_late_publication_after_deadline(self):
        issue = [event_forecast()]
        late = self.receipt(issue, published_at="2024-03-15T20:00:00+00:00")  # at the deadline
        with self.assertRaisesRegex(ValueError, "precede all issue deadlines"):
            ledger.validate_publication(late, issue, WEEK)

    def test_market_publication_after_monday_lock(self):
        issue = [market_forecast()]
        # Monday 09:30 ET for the 2024-03-08 anchor is 2024-03-11T13:30Z.
        late = self.receipt(issue, published_at="2024-03-11T13:30:00+00:00")
        with self.assertRaisesRegex(ValueError, "Monday 09:30 ET"):
            ledger.validate_publication(late, issue, WEEK)


class MarketBaselineTests(unittest.TestCase):
    def _history(self):
        # 460 nonoverlapping Friday pairs; alternate up and flat returns so the
        # count of threshold hits is deterministic and above the 450 minimum.
        from datetime import date, timedelta
        values = {}
        friday = date(2015, 1, 2)  # a Friday
        level = Decimal("100")
        for i in range(470):
            values[friday] = level
            values[friday + timedelta(days=7)] = level + (Decimal("1") if i % 2 else Decimal("0"))
            friday += timedelta(days=14)
        return values

    def test_laplace_baseline_counts_friday_pairs(self):
        values = self._history()
        anchor = max(values)
        with patch.object(ledger, "fetch_sp500",
                          return_value=(values, "https://fred.example/csv", "a" * 64)) as fetch:
            forecast = market_forecast(market={
                "series": "SP500", "start_date": "2024-03-01", "end_date": "2024-03-08",
                "direction": "gte", "threshold_pct": 0.5,
            })
            # Anchor must be a Friday for the baseline; override start_date accordingly.
            forecast["market"]["start_date"] = anchor.isoformat()
            result = ledger.market_baseline(forecast)
        fetch.assert_called_once()
        self.assertGreaterEqual(result["samples"], 450)
        self.assertEqual(result["probability"], (result["hits"] + 1) / (result["samples"] + 2))
        self.assertEqual(result["sha256"], "a" * 64)
        self.assertNotIn("levels", result)  # raw prices are never stored

    def test_insufficient_pairs_rejected(self):
        from datetime import date, timedelta
        values = {}
        friday = date(2023, 1, 6)
        for i in range(10):
            values[friday] = Decimal("100")
            values[friday + timedelta(days=7)] = Decimal("101")
            friday += timedelta(days=14)
        anchor = max(values)
        forecast = market_forecast()
        forecast["market"]["start_date"] = anchor.isoformat()
        with patch.object(ledger, "fetch_sp500", return_value=(values, "https://fred/csv", "b" * 64)):
            with self.assertRaisesRegex(ValueError, "Insufficient Friday close pairs"):
                ledger.market_baseline(forecast)


class MarketObservationTests(unittest.TestCase):
    def test_resolution_delay_before_cutoff_returns_none(self):
        forecast = market_forecast()
        # Observed before deadline + 8h: no resolution yet.
        just_before = datetime(2024, 3, 16, 3, 0, tzinfo=timezone.utc)
        self.assertIsNone(ledger.market_observation(forecast, now=just_before))

    def test_resolved_observation(self):
        forecast = market_forecast()
        from datetime import date
        values = {date(2024, 3, 8): Decimal("100"), date(2024, 3, 15): Decimal("101")}
        now = datetime(2024, 3, 16, 5, 0, tzinfo=timezone.utc)
        with patch.object(ledger, "fetch_sp500",
                          return_value=(values, "https://fred/csv", "c" * 64)):
            observation = ledger.market_observation(forecast, now=now)
        self.assertEqual(observation["status"], "resolved")
        self.assertEqual(observation["outcome"], 1)  # +1% return meets gte 0
        self.assertEqual(observation["forecast_sha256"], ledger.digest(forecast))

    def test_missing_market_close_before_void_cutoff_returns_none(self):
        forecast = market_forecast()
        from datetime import date
        values = {date(2024, 3, 8): Decimal("100")}  # end close missing
        now = datetime(2024, 3, 16, 5, 0, tzinfo=timezone.utc)
        with patch.object(ledger, "fetch_sp500",
                          return_value=(values, "https://fred/csv", "d" * 64)):
            self.assertIsNone(ledger.market_observation(forecast, now=now))

    def test_missing_market_close_voids_after_cutoff(self):
        forecast = market_forecast()
        from datetime import date
        values = {date(2024, 3, 8): Decimal("100")}  # end close still missing
        # void_not_before is not set, so default cutoff is end + 11 days 17:00 ET.
        now = datetime(2024, 4, 1, 0, 0, tzinfo=timezone.utc)
        with patch.object(ledger, "fetch_sp500",
                          return_value=(values, "https://fred/csv", "e" * 64)):
            observation = ledger.market_observation(forecast, now=now)
        self.assertEqual(observation["status"], "void")
        self.assertNotIn("outcome", observation)
        self.assertIn("2024-03-15", observation["resolution_note"])


class ScoreTests(unittest.TestCase):
    def _resolved_market(self, outcome):
        forecast = market_forecast()
        url = ledger._fred_url("SP500", _d("2024-03-08"), _d("2024-03-15"))
        observation = {
            "forecast_id": forecast["id"],
            "forecast_sha256": ledger.digest(forecast),
            "resolved_at": "2024-03-16T05:00:00+00:00",
            "source_urls": [url],
            "source_sha256": "f" * 64,
            "status": "resolved",
            "outcome": outcome,
            "resolution_note": "FRED closes resolved the threshold.",
        }
        return forecast, observation

    def test_matched_brier(self):
        forecast, observation = self._resolved_market(1)
        with patch.object(ledger, "utcnow",
                          return_value=datetime(2024, 3, 16, 6, 0, tzinfo=timezone.utc)):
            scored = ledger.score(forecast, observation)
        self.assertAlmostEqual(scored["brier"], (0.55 - 1) ** 2)
        self.assertAlmostEqual(scored["baseline_brier"], (0.5 - 1) ** 2)

    def test_amended_forecast_not_scoreable(self):
        # A forecast amended after the fact no longer matches the receipt digest.
        forecast, observation = self._resolved_market(1)
        amended = market_forecast(probability=0.9)  # changed probability
        with patch.object(ledger, "utcnow",
                          return_value=datetime(2024, 3, 16, 6, 0, tzinfo=timezone.utc)):
            with self.assertRaisesRegex(ValueError, "wrong forecast digest"):
                ledger.score(amended, observation)

    def test_void_has_no_brier_and_respects_cutoff(self):
        forecast = market_forecast()
        url = ledger._fred_url("SP500", _d("2024-03-08"), _d("2024-03-15"))
        observation = {
            "forecast_id": forecast["id"],
            "forecast_sha256": ledger.digest(forecast),
            "resolved_at": "2024-03-30T00:00:00+00:00",
            "source_urls": [url],
            "source_sha256": "a" * 64,
            "status": "void",
            "resolution_note": "FRED did not provide closes by the void cutoff.",
        }
        with patch.object(ledger, "utcnow",
                          return_value=datetime(2024, 3, 31, 0, 0, tzinfo=timezone.utc)):
            scored = ledger.score(forecast, observation)
        self.assertIsNone(scored["brier"])
        self.assertIsNone(scored["baseline_brier"])

    def test_void_too_early_rejected(self):
        forecast = market_forecast()
        url = ledger._fred_url("SP500", _d("2024-03-08"), _d("2024-03-15"))
        observation = {
            "forecast_id": forecast["id"],
            "forecast_sha256": ledger.digest(forecast),
            "resolved_at": "2024-03-16T05:00:00+00:00",  # before the end+11d cutoff
            "source_urls": [url],
            "source_sha256": "a" * 64,
            "status": "void",
            "resolution_note": "Premature void.",
        }
        with patch.object(ledger, "utcnow",
                          return_value=datetime(2024, 3, 17, 0, 0, tzinfo=timezone.utc)):
            with self.assertRaisesRegex(ValueError, "void review is too early"):
                ledger.score(forecast, observation)

    def test_named_event_dual_source_and_negative_barrier(self):
        forecast = event_forecast()
        observation = {
            "forecast_id": forecast["id"],
            "forecast_sha256": ledger.digest(forecast),
            "resolved_at": "2024-03-22T21:00:00+00:00",  # after resolve_no barrier
            "source_urls": ["https://www.sec.gov/filing", "https://investor.nvidia.com/pr"],
            "status": "resolved",
            "outcome": 0,
            "resolution_note": "Official SEC filing and issuer archive confirm no raise.",
        }
        with patch.object(ledger, "utcnow",
                          return_value=datetime(2024, 3, 23, 0, 0, tzinfo=timezone.utc)):
            scored = ledger.score(forecast, observation)
        self.assertAlmostEqual(scored["brier"], (0.6 - 0) ** 2)

    def test_named_event_negative_resolution_too_early(self):
        forecast = event_forecast()
        observation = {
            "forecast_id": forecast["id"],
            "forecast_sha256": ledger.digest(forecast),
            "resolved_at": "2024-03-20T00:00:00+00:00",  # before resolve_no barrier
            "source_urls": ["https://www.sec.gov/filing", "https://investor.nvidia.com/pr"],
            "status": "resolved",
            "outcome": 0,
            "resolution_note": "Premature negative resolution.",
        }
        with patch.object(ledger, "utcnow",
                          return_value=datetime(2024, 3, 21, 0, 0, tzinfo=timezone.utc)):
            with self.assertRaisesRegex(ValueError, "Negative-resolution review is too early"):
                ledger.score(forecast, observation)

    def test_named_event_requires_both_sec_and_issuer(self):
        forecast = event_forecast()
        observation = {
            "forecast_id": forecast["id"],
            "forecast_sha256": ledger.digest(forecast),
            "resolved_at": "2024-03-22T21:00:00+00:00",
            "source_urls": ["https://www.sec.gov/filing"],  # issuer archive missing
            "status": "resolved",
            "outcome": 0,
            "resolution_note": "Only SEC reviewed.",
        }
        with patch.object(ledger, "utcnow",
                          return_value=datetime(2024, 3, 23, 0, 0, tzinfo=timezone.utc)):
            with self.assertRaisesRegex(ValueError, "SEC and issuer archive"):
                ledger.score(forecast, observation)

    def test_market_requires_exact_declared_source(self):
        forecast, observation = self._resolved_market(1)
        observation["source_urls"] = ["https://fred.stlouisfed.org/graph/fredgraph.csv?id=OTHER"]
        with patch.object(ledger, "utcnow",
                          return_value=datetime(2024, 3, 16, 6, 0, tzinfo=timezone.utc)):
            with self.assertRaisesRegex(ValueError, "exact declared FRED series"):
                ledger.score(forecast, observation)


def _d(value):
    from datetime import date
    return date.fromisoformat(value)


class LoadLedgerTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _write_issue(self, issue):
        ledger.write_new(self.root / "forecasts" / f"{WEEK}.json", issue)

    def test_publication_excludes_registered_draft(self):
        # A prospective issue with no publication receipt is skipped by default.
        self._write_issue([event_forecast()])
        records, results = ledger.load_ledger(self.root)
        self.assertEqual(records, [])
        self.assertEqual(results, {})

    def test_unpublished_included_when_allowed(self):
        self._write_issue([event_forecast()])
        records, _ = ledger.load_ledger(self.root, allow_unpublished=True)
        self.assertEqual(len(records), 1)
        self.assertNotIn("publication", records[0])

    def test_orphan_publication_rejected(self):
        ledger.write_new(self.root / "publications" / f"{WEEK}.json", {"url": "x"})
        with self.assertRaisesRegex(ValueError, "Orphan publication"):
            ledger.load_ledger(self.root)

    def test_orphan_resolution_rejected(self):
        self._write_issue([event_forecast()])
        ledger.write_new(self.root / "results" / "unknown-id.json", {"status": "void"})
        with self.assertRaisesRegex(ValueError, "Orphan resolution"):
            ledger.load_ledger(self.root, allow_unpublished=True)

    def test_published_issue_is_attached(self):
        issue = [event_forecast()]
        self._write_issue(issue)
        receipt = {
            "url": f"https://github.com/Noptus/tsunami-research/releases/tag/forecast-{WEEK}",
            "published_at": "2024-03-10T13:30:00+00:00",
            "immutable": True,
            "issue_sha256": ledger.digest(issue),
            "release_id": 1,
            "asset_id": 2,
        }
        ledger.write_new(self.root / "publications" / f"{WEEK}.json", receipt)
        records, _ = ledger.load_ledger(self.root)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["publication"], receipt)


if __name__ == "__main__":
    unittest.main()
