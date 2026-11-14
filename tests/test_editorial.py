"""Offline tests for the editorial intake/review harness.

No network, git, or shell. ``market_baseline`` is patched with a synthetic
frequency summary so no FRED fetch runs; the real ``propose`` is otherwise
exercised end to end. Supplemental source snapshots live in a temporary
directory, and every clock is injected.

Run with: python -m unittest discover -s tests -p 'test_editorial.py'
"""

import hashlib
from copy import deepcopy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tsunami import draft, editorial
from tsunami.ledger import digest, read_json, validate_issue, write_new


WEEK = "2026-11-20"
WINDOW = datetime(2026, 11, 14, 21, 31, 10, tzinfo=timezone.utc)
# A review clock after issuance; evidence (<= as_of) is never lookahead here.
REVIEW_CLOCK = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)
HOLIDAY_WEEK = "2026-11-27"


def fake_baseline(sha="80bc4bcbdcb196716c2cca223c074e6f94f91e98675eb76904e55a343101181a"):
    def _factory(forecast):
        market = forecast["market"]
        hits = 71 if market["direction"] == "gte" else 65
        return {
            "probability": (hits + 1) / (488 + 2),
            "hits": hits,
            "samples": 488,
            "range_start": "2016-11-15",
            "range_end": market["start_date"],
            "method": "synthetic test baseline; zero market levels stored",
            "source_url": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500&cosd=2016-11-15&coed=2026-11-13",
            "sha256": sha,
        }
    return _factory


def make_proposal(week=WEEK, now=WINDOW, baseline=None):
    with patch.object(draft, "market_baseline", baseline or fake_baseline()):
        return draft.propose(week, now=now)


def candidate_from(proposal):
    """A plausible edited candidate issue derived from a proposal's records."""
    return deepcopy(proposal["records"])


class PrepareTests(unittest.TestCase):
    def test_prepare_from_proposal_is_immutable_and_self_digested(self):
        proposal = make_proposal()
        packet = editorial.prepare(WEEK, proposal=proposal, now=REVIEW_CLOCK, root=Path.cwd())
        self.assertEqual(packet["schema"], "tsunami.editorial.packet")
        self.assertEqual(packet["week_ending"], WEEK)
        self.assertEqual(packet["proposal_status"], "proposed")
        self.assertEqual(packet["baseline_digest"], digest(proposal["records"]))
        # The self-digest verifies.
        editorial.verify_packet(packet)
        # Mutating any content invalidates the digest.
        tampered = {**packet, "week_ending": "2026-11-13"}
        with self.assertRaises(ValueError):
            editorial.verify_packet(tampered)

    def test_prepare_sources_from_propose_when_absent(self):
        # No explicit proposal: prepare calls propose internally.
        with patch.object(draft, "market_baseline", fake_baseline()):
            packet = editorial.prepare(WEEK, now=WINDOW, root=Path.cwd())
        self.assertEqual(packet["proposal_status"], "proposed")
        self.assertEqual(len(packet["proposal"]["records"]), 2)

    def test_snapshot_source_hashed_and_capped(self):
        proposal = make_proposal()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            snap = root / "nvidia.html"
            payload = b"<html>CEO transition</html>"
            snap.write_bytes(payload)
            sources = [{
                "title": "NVIDIA CEO transition snapshot",
                "url": "https://investor.nvidia.com/news/press-release-details/2026/x/",
                "available_at": "2026-11-13T08:00:00Z",
                "retrieved_at": "2026-11-14T09:00:00Z",
                "path": "nvidia.html",
            }]
            packet = editorial.prepare(WEEK, proposal=proposal, sources=sources,
                                       manifest_dir=root, now=REVIEW_CLOCK, root=root)
        receipt = packet["sources"][0]
        self.assertEqual(receipt["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertTrue(receipt["payload_verified"])
        self.assertEqual(receipt["snapshot_bytes"], len(payload))

    def test_link_only_source_marks_absence_of_payload_verification(self):
        proposal = make_proposal()
        sources = [{
            "title": "BLS CPI release",
            "url": "https://www.bls.gov/news.release/archives/cpi_11102026.htm",
            "available_at": "2026-11-10T13:30:00Z",
            "retrieved_at": "2026-11-14T09:00:00Z",
        }]
        packet = editorial.prepare(WEEK, proposal=proposal, sources=sources,
                                   now=REVIEW_CLOCK, root=Path.cwd())
        receipt = packet["sources"][0]
        self.assertFalse(receipt["payload_verified"])
        self.assertNotIn("snapshot_bytes", receipt)

    def test_declared_digest_without_snapshot_is_flagged_declared_only(self):
        proposal = make_proposal()
        sources = [{
            "title": "Canonical digest only",
            "url": "https://www.bls.gov/news.release/archives/cpi_11102026.htm",
            "available_at": "2026-11-10T13:30:00Z",
            "retrieved_at": "2026-11-14T09:00:00Z",
            "sha256": "a" * 64,
        }]
        packet = editorial.prepare(WEEK, proposal=proposal, sources=sources,
                                   now=REVIEW_CLOCK, root=Path.cwd())
        receipt = packet["sources"][0]
        self.assertFalse(receipt["payload_verified"])
        self.assertTrue(receipt["sha256_declared_only"])
        self.assertEqual(receipt["sha256"], "a" * 64)

    def test_snapshot_digest_conflict_rejected(self):
        proposal = make_proposal()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "s.html").write_bytes(b"content")
            sources = [{
                "title": "Conflicting digest",
                "url": "https://example.gov/x",
                "available_at": "2026-11-10T13:30:00Z",
                "retrieved_at": "2026-11-14T09:00:00Z",
                "path": "s.html",
                "sha256": "b" * 64,
            }]
            with self.assertRaises(ValueError):
                editorial.prepare(WEEK, proposal=proposal, sources=sources,
                                  manifest_dir=root, now=REVIEW_CLOCK, root=root)

    def test_future_availability_rejected(self):
        proposal = make_proposal()
        sources = [{
            "title": "From the future",
            "url": "https://example.gov/x",
            "available_at": "2027-01-01T00:00:00Z",
            "retrieved_at": "2027-01-01T00:00:00Z",
        }]
        with self.assertRaises(ValueError):
            editorial.prepare(WEEK, proposal=proposal, sources=sources,
                              now=REVIEW_CLOCK, root=Path.cwd())

    def test_unsafe_url_rejected(self):
        proposal = make_proposal()
        sources = [{
            "title": "Not https",
            "url": "http://example.gov/x",
            "available_at": "2026-11-10T13:30:00Z",
            "retrieved_at": "2026-11-14T09:00:00Z",
        }]
        with self.assertRaises(ValueError):
            editorial.prepare(WEEK, proposal=proposal, sources=sources,
                              now=REVIEW_CLOCK, root=Path.cwd())

    def test_path_traversal_rejected(self):
        proposal = make_proposal()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sources = [{
                "title": "escape",
                "url": "https://example.gov/x",
                "available_at": "2026-11-10T13:30:00Z",
                "retrieved_at": "2026-11-14T09:00:00Z",
                "path": "../secret",
            }]
            with self.assertRaises(ValueError):
                editorial.prepare(WEEK, proposal=proposal, sources=sources,
                                  manifest_dir=root, now=REVIEW_CLOCK, root=root)

    def test_symlink_escape_rejected(self):
        proposal = make_proposal()
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as d:
            target = Path(outside) / "secret.txt"
            target.write_bytes(b"secret")
            root = Path(d)
            link = root / "link.txt"
            link.symlink_to(target)
            sources = [{
                "title": "symlink escape",
                "url": "https://example.gov/x",
                "available_at": "2026-11-10T13:30:00Z",
                "retrieved_at": "2026-11-14T09:00:00Z",
                "path": "link.txt",
            }]
            with self.assertRaises(ValueError):
                editorial.prepare(WEEK, proposal=proposal, sources=sources,
                                  manifest_dir=root, now=REVIEW_CLOCK, root=root)

    def test_duplicate_agreeing_sources_collapse(self):
        proposal = make_proposal()
        entry = {
            "title": "Duplicate",
            "url": "https://example.gov/x",
            "available_at": "2026-11-10T13:30:00Z",
            "retrieved_at": "2026-11-14T09:00:00Z",
            "sha256": "c" * 64,
        }
        packet = editorial.prepare(WEEK, proposal=proposal, sources=[entry, dict(entry)],
                                   now=REVIEW_CLOCK, root=Path.cwd())
        self.assertEqual(packet["source_count"], 1)

    def test_holiday_abstention_packet_still_prepared(self):
        # The holiday week abstains, but a named-event issue can still build on it.
        proposal = make_proposal(week=HOLIDAY_WEEK,
                                 now=datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(proposal["status"], "abstained")
        packet = editorial.prepare(HOLIDAY_WEEK, proposal=proposal,
                                   now=datetime(2026, 11, 21, 16, 0, tzinfo=timezone.utc),
                                   root=Path.cwd())
        self.assertEqual(packet["proposal_status"], "abstained")
        self.assertIsNone(packet["baseline_digest"])
        self.assertEqual(packet["proposal"]["records"], [])
        editorial.verify_packet(packet)


class ReviewTests(unittest.TestCase):
    def packet_and_candidate(self):
        proposal = make_proposal()
        packet = editorial.prepare(WEEK, proposal=proposal, now=REVIEW_CLOCK, root=Path.cwd())
        candidate = candidate_from(proposal)
        return packet, candidate

    def test_real_shaped_pipeline_review(self):
        packet, candidate = self.packet_and_candidate()
        report = editorial.review(packet, candidate, WEEK, now=REVIEW_CLOCK)
        self.assertEqual(report["schema"], "tsunami.editorial.review")
        self.assertEqual(report["issue_sha256"], digest(validate_issue(candidate, WEEK)))
        self.assertEqual(report["forecast_count"], 2)
        # Proposal baselines anchored exactly.
        self.assertEqual(len(report["market_baselines_anchored"]), 2)
        self.assertIn("schema: validate_issue", report["checked"])
        self.assertIn("factual truth of any cited claim", report["not_checked"])

    def test_content_addressed_by_issue_digest(self):
        packet, candidate = self.packet_and_candidate()
        r1 = editorial.review(packet, candidate, WEEK, now=REVIEW_CLOCK)
        mutated = [dict(f) for f in candidate]
        mutated[0] = {**mutated[0], "probability": 0.2}
        r2 = editorial.review(packet, mutated, WEEK, now=REVIEW_CLOCK)
        self.assertNotEqual(r1["issue_sha256"], r2["issue_sha256"])

    def test_snapshot_source_is_retrievable_proof(self):
        proposal = make_proposal()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            snap = root / "cpi.html"
            snap.write_bytes(b"<html>CPI</html>")
            sha = hashlib.sha256(b"<html>CPI</html>").hexdigest()
            sources = [{
                "title": "CPI snapshot",
                "url": "https://www.bls.gov/news.release/archives/cpi_11102026.htm",
                "available_at": "2026-11-10T13:30:00Z",
                "retrieved_at": "2026-11-14T09:00:00Z",
                "path": "cpi.html",
            }]
            packet = editorial.prepare(WEEK, proposal=proposal, sources=sources,
                                       manifest_dir=root, now=REVIEW_CLOCK, root=root)
        candidate = candidate_from(proposal)
        # Cite the registered source in the first record's evidence.
        candidate[0]["evidence"].append({
            "title": "CPI snapshot",
            "url": "https://www.bls.gov/news.release/archives/cpi_11102026.htm",
            "available_at": "2026-11-10T13:30:00Z",
            "sha256": sha,
        })
        report = editorial.review(packet, candidate, WEEK, now=REVIEW_CLOCK)
        self.assertIn("https://www.bls.gov/news.release/archives/cpi_11102026.htm",
                      report["snapshot_verified_at_intake_urls"])

    def test_unregistered_evidence_rejected(self):
        packet, candidate = self.packet_and_candidate()
        candidate[0]["evidence"].append({
            "title": "Unregistered source",
            "url": "https://example.com/opinion",
            "available_at": "2026-11-10T00:00:00Z",
        })
        with self.assertRaisesRegex(ValueError, "missing from packet"):
            editorial.review(packet, candidate, WEEK, now=REVIEW_CLOCK)

    def test_repeated_review_has_stable_receipt(self):
        packet, candidate = self.packet_and_candidate()
        start = editorial.review(packet, candidate, WEEK, now=REVIEW_CLOCK)
        later = editorial.review(packet, candidate, WEEK, now=REVIEW_CLOCK.replace(hour=14))
        self.assertEqual(start, later)

    def test_changing_baseline_value_is_rejected_even_when_hash_is_unchanged(self):
        packet, candidate = self.packet_and_candidate()
        candidate[0]["baseline_probability"] = 0.5
        with self.assertRaisesRegex(ValueError, "frozen proposal or baseline"):
            editorial.review(packet, candidate, WEEK, now=REVIEW_CLOCK)

    def test_named_event_can_use_holiday_packet_and_link_only_citation(self):
        proposal = make_proposal(week=HOLIDAY_WEEK,
                                 now=datetime(2026, 11, 21, 15, tzinfo=timezone.utc))
        source = {"title":"Issuer press page", "url":"https://example.org/press",
                  "available_at":"2026-11-21T13:00:00Z"}
        clock = datetime(2026, 11, 21, 16, tzinfo=timezone.utc)
        packet = editorial.prepare(HOLIDAY_WEEK, proposal=proposal, sources=[source], now=clock)
        candidate = {
            "id": "named-event", "week_ending": HOLIDAY_WEEK, "mode": "prospective",
            "issued_at": "2026-11-21T15:00:00Z", "as_of_at": "2026-11-21T14:00:00Z",
            "deadline_at": "2026-11-27T21:00:00Z", "kind": "binary_event",
            "question": "Will the issuer file its report by Friday?", "rationale": "Its calendar lists Friday.",
            "resolution_rule": "Yes on the issuer's recorded filing.", "probability": 0.65,
            "baseline_probability": None, "resolution_hosts": ["example.org"],
            "risks": ["A delay in filing"], "evidence": [source],
        }
        result = editorial.review(packet, [candidate], HOLIDAY_WEEK, now=clock)
        self.assertEqual(result["cited_source_urls"], [source["url"]])
        self.assertEqual(result["market_baselines_anchored"], [])

    def test_edited_snapshot_digest_rejected(self):
        proposal = make_proposal()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "cpi.html").write_bytes(b"<html>CPI</html>")
            sources = [{
                "title": "CPI snapshot",
                "url": "https://www.bls.gov/x",
                "available_at": "2026-11-10T13:30:00Z",
                "retrieved_at": "2026-11-14T09:00:00Z",
                "path": "cpi.html",
            }]
            packet = editorial.prepare(WEEK, proposal=proposal, sources=sources,
                                       manifest_dir=root, now=REVIEW_CLOCK, root=root)
        candidate = candidate_from(proposal)
        candidate[0]["evidence"].append({
            "title": "CPI snapshot",
            "url": "https://www.bls.gov/x",
            "available_at": "2026-11-10T13:30:00Z",
            "sha256": "d" * 64,  # wrong digest
        })
        with self.assertRaises(ValueError):
            editorial.review(packet, candidate, WEEK, now=REVIEW_CLOCK)

    def test_edited_registered_availability_rejected(self):
        proposal = make_proposal()
        sources = [{
            "title": "Registered",
            "url": "https://www.bls.gov/x",
            "available_at": "2026-11-10T13:30:00Z",
            "retrieved_at": "2026-11-14T09:00:00Z",
        }]
        packet = editorial.prepare(WEEK, proposal=proposal, sources=sources,
                                   now=REVIEW_CLOCK, root=Path.cwd())
        candidate = candidate_from(proposal)
        candidate[0]["evidence"].append({
            "title": "Registered but availability edited",
            "url": "https://www.bls.gov/x",
            "available_at": "2026-11-09T13:30:00Z",  # changed
        })
        with self.assertRaises(ValueError):
            editorial.review(packet, candidate, WEEK, now=REVIEW_CLOCK)

    def test_lookahead_evidence_rejected(self):
        packet, candidate = self.packet_and_candidate()
        # Push as_of and evidence availability past the review clock.
        for f in candidate:
            f["as_of_at"] = "2027-01-01T00:00:00Z"
            f["issued_at"] = "2027-01-01T00:00:00Z"
            for e in f["evidence"]:
                e["available_at"] = "2027-01-01T00:00:00Z"
        with self.assertRaises(ValueError):
            editorial.review(packet, candidate, WEEK, now=REVIEW_CLOCK)

    def test_changed_market_anchor_rejected(self):
        packet, candidate = self.packet_and_candidate()
        candidate[0]["market"]["start_date"] = "2026-11-06"
        candidate[0]["market"]["end_date"] = "2026-11-13"
        with self.assertRaises(ValueError):
            editorial.review(packet, candidate, WEEK, now=REVIEW_CLOCK)

    def test_changed_baseline_digest_rejected(self):
        packet, candidate = self.packet_and_candidate()
        candidate[0]["baseline"]["sha256"] = "e" * 64
        with self.assertRaises(ValueError):
            editorial.review(packet, candidate, WEEK, now=REVIEW_CLOCK)

    def test_mutually_exclusive_tails_over_one_rejected(self):
        packet, candidate = self.packet_and_candidate()
        candidate[0]["probability"] = 0.7
        candidate[1]["probability"] = 0.6  # 1.3 > 1 for opposite tails
        with self.assertRaises(ValueError):
            editorial.review(packet, candidate, WEEK, now=REVIEW_CLOCK)

    def test_mutually_exclusive_tails_within_one_ok(self):
        packet, candidate = self.packet_and_candidate()
        candidate[0]["probability"] = 0.3
        candidate[1]["probability"] = 0.3
        report = editorial.review(packet, candidate, WEEK, now=REVIEW_CLOCK)
        self.assertEqual(report["forecast_count"], 2)

    def test_packet_for_wrong_week_rejected(self):
        packet, candidate = self.packet_and_candidate()
        with self.assertRaises(ValueError):
            editorial.review(packet, candidate, "2026-11-13", now=REVIEW_CLOCK)


class StageTests(unittest.TestCase):
    def _fixture(self, root: Path):
        proposal = make_proposal()
        packet = editorial.prepare(WEEK, proposal=proposal, now=REVIEW_CLOCK, root=root)
        candidate = candidate_from(proposal)
        report = editorial.review(packet, candidate, WEEK, now=REVIEW_CLOCK)
        return packet, candidate, report

    def test_stage_rechecks_the_entire_report(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            packet, candidate, report = self._fixture(root)
            with self.assertRaises(ValueError):
                editorial.stage(root, candidate, WEEK, packet, {**report, "evidence_checked": 99}, now=REVIEW_CLOCK)

    def test_stage_writes_once_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            packet, candidate, report = self._fixture(root)
            result = editorial.stage(root, candidate, WEEK, packet, report, now=REVIEW_CLOCK)
            self.assertTrue(Path(result["staged"]).exists())
            self.assertEqual(result["issue_sha256"], report["issue_sha256"])
            # An identical retry leaves the bytes untouched. A reviewed edit still cannot overwrite.
            self.assertEqual(result, editorial.stage(root, candidate, WEEK, packet, report, now=REVIEW_CLOCK))
            changed = deepcopy(candidate)
            changed[0]["probability"] = 0.11
            changed_review = editorial.review(packet, changed, WEEK, now=REVIEW_CLOCK)
            with self.assertRaisesRegex(ValueError, "already exists"):
                editorial.stage(root, changed, WEEK, packet, changed_review, now=REVIEW_CLOCK)

    def test_stage_rejects_report_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            packet, candidate, report = self._fixture(root)
            edited = [dict(f) for f in candidate]
            edited[0] = {**edited[0], "probability": 0.11}
            with self.assertRaises(ValueError):
                editorial.stage(root, edited, WEEK, packet, report, now=REVIEW_CLOCK)

    def test_stage_rejects_duplicate_ids_across_issues(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            packet, candidate, report = self._fixture(root)
            # Seed another week's issue that reuses one of the candidate IDs.
            other_week = "2026-11-13"
            other = [dict(f) for f in candidate]
            for f in other:
                f["week_ending"] = other_week
            write_new(root / "forecasts" / f"{other_week}.json", other)
            with self.assertRaises(ValueError):
                editorial.stage(root, candidate, WEEK, packet, report, now=REVIEW_CLOCK)

    def test_staged_issue_passes_validate_issue(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            packet, candidate, report = self._fixture(root)
            editorial.stage(root, candidate, WEEK, packet, report, now=REVIEW_CLOCK)
            saved = read_json(root / "forecasts" / f"{WEEK}.json")
            validate_issue(saved, WEEK)  # must not raise


if __name__ == "__main__":
    unittest.main()
