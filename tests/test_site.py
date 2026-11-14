"""Standard-library tests; temporary publications stay inside this repository."""
from __future__ import annotations

import copy
import hashlib
import html
import json
import tempfile
import unittest
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from tsunami.site import render


ROOT = Path(__file__).resolve().parents[1]


class Document(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.text = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)

    @property
    def links(self):
        return [attrs["href"] for tag, attrs in self.elements if tag == "a" and "href" in attrs]


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = cls(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)


def forecast(identity="live-01", **changes):
    record = {
        "id": identity,
        "issued_at": "2026-11-09T09:00:00+00:00",
        "week_ending": "2026-11-20",
        "as_of_at": "2026-11-09T08:30:00+00:00",
        "kind": "market_threshold",
        "question": "Will the market close above the threshold?",
        "probability": 0.65,
        "baseline_probability": 0.5,
        "rationale": "Momentum is positive, but uncertainty remains.",
        "risks": ["A policy surprise", "A reversal in momentum"],
        "resolution_rule": "Yes if the official close is strictly above the threshold.",
        "deadline_at": "2026-11-20T21:00:00+00:00",
        "evidence": [{"title": "Official series", "url": "https://example.org/data?a=1&b=2",
                      "available_at": "2026-11-09T08:00:00+00:00"}],
        "mode": "prospective",
        "method": "Frozen trailing-window estimate",
    }
    record.update(changes)
    return record


def resolved(identity="live-01", **changes):
    record = {"forecast_id": identity, "resolved_at": "2026-11-13T21:05:00Z",
              "outcome": 1, "brier": 0.1225, "baseline_brier": 0.25,
              "resolution_note": "Official close exceeded the threshold.",
              "source_urls": ["https://example.org/close"], "status": "resolved"}
    record.update(changes)
    return record


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".site-test-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.output = self.work / "publication"
        self.clock = patch("tsunami.site.datetime", Clock)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def publish(self, forecasts=None, results=None, replay=None):
        render(self.output, {} if replay is None else replay,
               [] if forecasts is None else forecasts, {} if results is None else results)
        return (self.output / "index.html").read_text(encoding="utf-8")

    def scores(self):
        return json.loads((self.output / "scores.json").read_text(encoding="utf-8"))

    def test_publication_landmarks_and_artifacts(self):
        data = forecast()
        replay = {"title": "Example study", "future_schema": {"nested": [1, 2, 3]}}
        source = self.publish([data], replay=replay)
        doc = Document(source)
        tags = [tag for tag, _ in doc.elements]
        for landmark in ("header", "nav", "main", "footer", "table", "caption", "article"):
            self.assertIn(landmark, tags)
        self.assertIn('<html lang="en">', source)
        self.assertIn("Tsunami Research", source)
        self.assertNotIn("script", tags)
        self.assertNotIn("img", tags)
        self.assertIn("replay.json", doc.links)
        self.assertIn("scores.json", doc.links)
        self.assertEqual(replay, json.loads((self.output / "replay.json").read_text()))
        self.assertEqual((ROOT / "tsunami/static/styles.css").read_bytes(),
                         (self.output / "styles.css").read_bytes())
        for href in doc.links:
            if not href.startswith(("http", "#")):
                self.assertTrue((self.output / href).is_file(), href)
        anchors = {attrs["id"] for _, attrs in doc.elements if "id" in attrs}
        for href in doc.links:
            if href.startswith("#"):
                self.assertIn(href[1:], anchors)
        self.assertTrue(any(attrs.get("scope") == "row" for tag, attrs in doc.elements if tag == "th"))
        self.assertTrue(any(attrs.get("scope") == "col" for tag, attrs in doc.elements if tag == "th"))
        self.assertTrue(any(attrs.get("role") == "region" and attrs.get("tabindex") == "0"
                            and attrs.get("aria-label") for _, attrs in doc.elements))
        bars = [attrs for _, attrs in doc.elements if attrs.get("class") == "bar"]
        self.assertTrue(bars)
        self.assertTrue(all(attrs.get("aria-hidden") == "true" for attrs in bars))
        self.assertIn("65%", "".join(doc.text))
        css = (self.output / "styles.css").read_text()
        self.assertIn("overflow-x: auto", css)
        self.assertIn("@media (max-width:", css)
        self.assertNotRegex(css, r"(?i)@import|https?://|url\(")

    def test_external_text_and_attribute_escaping(self):
        attack = '\"><script>alert("x")</script><img src=x onerror=alert(1)> & café'
        fields = {key: attack for key in ("question", "rationale", "resolution_rule", "method", "kind",
                                         "issued_at", "as_of_at", "week_ending", "deadline_at")}
        data = forecast(attack, **fields, risks=[attack], evidence=[{
            "title": attack, "url": 'https://example.org/?q=" onclick="bad&x=<tag>',
            "available_at": attack}])
        result = resolved(attack, resolution_note=attack, resolved_at=attack,
                          source_urls=["javascript:alert(1)"])
        source = self.publish([data], {attack: result}, {"title": attack, "cutoff_at": attack,
                                                       "outcome": {"date": attack, "note": attack}})
        doc = Document(source)
        self.assertNotIn("<script>", source)
        self.assertIn(html.escape(attack, quote=True), source)
        self.assertIn(attack, "".join(doc.text))
        for tag, attrs in doc.elements:
            self.assertNotIn(tag, ("script", "img", "iframe", "svg"))
            self.assertFalse(any(key.startswith("on") for key in attrs))
        self.assertIn('https://example.org/?q=" onclick="bad&x=<tag>', doc.links)
        self.assertNotIn("javascript:alert(1)", doc.links)
        self.assertEqual(data, json.loads(next((self.output / "forecasts").glob("*.json")).read_text()))

    def test_only_safe_evidence_and_resolution_urls_link(self):
        unsafe = ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<script>x</script>",
                  "file:///etc/passwd", "//example.org", "/relative", "ftp://example.org",
                  " https://example.org", "https://example.org\n", "java\nscript:alert(1)",
                  "https:\\evil.org", "https://user:pass@example.org", "https://[broken",
                  "https://example.org:bad", "https://", "https://example.org/\x00bad"]
        safe = ["https://example.org/a?x=1&y=2", "http://example.org/path", "HTTPS://example.org/upper"]
        data = forecast(evidence=[{"title": f"Source {i}", "url": url, "available_at": "2026-01-01"}
                                  for i, url in enumerate(unsafe + safe)])
        source = self.publish([data], {data["id"]: resolved(source_urls=unsafe + safe)})
        links = Document(source).links
        for url in unsafe:
            self.assertNotIn(url, links)
        for url in safe:
            self.assertEqual(links.count(url), 2)
        self.assertIn("link unavailable", source)

    def test_ids_cannot_inject_paths_and_snapshots_are_immutable(self):
        identities = ["../../escape", "/absolute", "a/b", "a\\b", "..", 'x\"#?<b>', "雪", ""]
        forecasts = [forecast(i) for i in identities]
        self.publish(forecasts)
        files = self.scores()["forecast_files"]
        self.assertEqual(set(files), set(identities))
        original = {}
        for identity, name in files.items():
            self.assertRegex(name, r"^forecasts/[0-9a-f]{64}\.json$")
            path = self.output / name
            self.assertTrue(path.resolve().is_relative_to(self.output))
            payload = path.read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), path.stem)
            self.assertEqual(json.loads(payload)["id"], identity)
            original[path] = (payload, path.stat().st_mtime_ns)
        self.publish(forecasts)
        for path, (payload, mtime) in original.items():
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(path.stat().st_mtime_ns, mtime)
        changed = copy.deepcopy(forecasts)
        changed[0]["probability"] = 0.1
        self.publish(changed)
        self.assertNotEqual(files[identities[0]], self.scores()["forecast_files"][identities[0]])
        for path, (payload, _) in original.items():
            self.assertEqual(path.read_bytes(), payload)
        self.assertEqual(len(list((self.output / "forecasts").glob("*.json"))), len(identities) + 1)

    def test_immutable_content_mismatch_fails_before_publishing(self):
        self.publish([forecast()])
        artifact = next((self.output / "forecasts").glob("*.json"))
        artifact.write_text("tampered", encoding="utf-8")
        old_index = (self.output / "index.html").read_bytes()
        with self.assertRaisesRegex(ValueError, "Immutable"):
            self.publish([forecast()], replay={"title": "Should not appear"})
        self.assertEqual((self.output / "index.html").read_bytes(), old_index)
        self.assertEqual(artifact.read_text(), "tampered")

    def test_schedule_labels_are_explicit_and_static(self):
        forecasts = [forecast("future"),
                     forecast("due", deadline_at="2026-11-14T07:00:00-05:00"),
                     forecast("past", deadline_at="2026-11-13T12:00:00Z"),
                     forecast("unknown", deadline_at="not a timestamp"),
                     forecast("archive", mode="archived_replay", as_of_at="2099-01-01T00:00:00Z")]
        source = self.publish(forecasts)
        self.assertIn("Unresolved · deadline ahead", source)
        self.assertIn("Deadline passed · awaiting resolution", source)
        self.assertIn("Unresolved · deadline unverified", source)
        self.assertIn("Latest live evidence as of", source)
        self.assertIn("2026-11-09T08:30:00+00:00", source)
        edition = source.split('<aside class="edition"', 1)[1].split("</aside>", 1)[0]
        self.assertNotIn("2099", edition)
        self.assertIn("2026-11-14T12:00:00+00:00", edition)
        for label in ("Issued", "Evidence as of", "Week ending", "Resolution deadline",
                      "Static edition built", "Prospective · live record", "Archived replay · not live"):
            self.assertIn(label, source)
        self.assertIn("not a promised publication schedule", source)
        self.assertIn("Passing a deadline does not resolve a question", source)
        doc = Document(source)
        self.assertFalse(any(attrs.get("datetime") == "not a timestamp" for _, attrs in doc.elements))

    def test_only_resolved_live_records_enter_summaries(self):
        forecasts = [forecast("yes"), forecast("no", baseline_probability=None), forecast("unscored"),
                     forecast("void"), forecast("pending"), forecast("replay", mode="archived_replay"),
                     forecast("bad-outcome"), forecast("wrong-id")]
        results = {
            "yes": resolved("yes", brier=0.04, baseline_brier=0.25),
            "no": resolved("no", outcome=0, brier=0.16, baseline_brier=None),
            "unscored": resolved("unscored", brier=None, baseline_brier=None),
            "void": {"forecast_id": "void", "status": "void", "brier": 0, "baseline_brier": 0,
                     "resolution_note": "Source withdrawn", "source_urls": []},
            "replay": resolved("replay", brier=1, baseline_brier=1),
            "bad-outcome": resolved("bad-outcome", outcome=True),
            "wrong-id": resolved("another-id"),
            "orphan": resolved("orphan", brier=1),
        }
        before = copy.deepcopy((forecasts, results))
        source = self.publish(forecasts, results)
        summary = self.scores()["summary"]
        self.assertEqual(summary["resolved_count"], 3)
        self.assertEqual(summary["scored_count"], 2)
        self.assertAlmostEqual(summary["mean_brier"], 0.1)
        self.assertEqual(summary["baseline_comparison_count"], 1)
        self.assertEqual(summary["paired_mean_brier"], 0.04)
        self.assertEqual(summary["mean_baseline_brier"], 0.25)
        self.assertEqual(results, self.scores()["results"])
        self.assertIn("Resolved · Yes", source)
        self.assertIn("Resolved · No", source)
        self.assertIn("Void · not scored", source)
        self.assertEqual(before, (forecasts, results))

    def test_empty_scores_are_null_not_zero(self):
        source = self.publish()
        summary = self.scores()["summary"]
        self.assertEqual(summary["resolved_count"], 0)
        self.assertIsNone(summary["mean_brier"])
        self.assertIsNone(summary["mean_baseline_brier"])
        self.assertIn("No live as-of timestamp available", source)
        self.assertIn("No forecasts published", source)
        self.assertIn("Not available", source)

    def test_zero_probabilities_and_scores_are_not_missing(self):
        source = self.publish([forecast(probability=0, baseline_probability=0)],
                              {"live-01": resolved(outcome=0, brier=0, baseline_brier=0)},
                              {"title": "Zero baseline", "baseline_probability": 0, "baseline_score": 0})
        self.assertIn('class="probability">0%</span>', source)
        self.assertIn("--probability: 0.00%", source)
        self.assertEqual(self.scores()["summary"]["mean_baseline_brier"], 0)
        replay = source.split('<div class="replay">', 1)[1].split('</div><p class="empty">', 1)[0]
        self.assertIn("0.000", replay)

    def test_replay_known_fields_and_chronological_observations(self):
        replay = {"title": "CPI / Treasury", "cutoff_at": "2024-01-11T13:30:00Z",
                  "predictions": {"baseline": {"probability_up": 0.6}},
                  "event": {"date": "2024-01-12", "up": 1, "brier": {"baseline": 0.16}},
                  "next": {"date": "2024-01-15", "up": 0, "brier": None}}
        source = self.publish(replay=replay)
        self.assertIn("CPI / Treasury", source)
        self.assertIn("2024-01-11T13:30:00Z", source)
        self.assertIn('class="probability">60%</span>', source)
        self.assertIn("0.160", source)
        self.assertIn("Chronological observations", source)
        self.assertLess(source.index("2024-01-12"), source.index("2024-01-15"))
        self.assertEqual(self.scores()["summary"]["resolved_count"], 0)
        self.assertEqual(replay, json.loads((self.output / "replay.json").read_text()))
        # Timeline order is chronological, not list order or lexical timezone order.
        replay["outcomes"] = [{"at": "2024-01-12T09:00:00-05:00", "outcome": "later-observation"},
                              {"at": "2024-01-12T13:00:00Z", "outcome": "earlier-observation"}]
        source = self.publish(replay=replay)
        self.assertLess(source.index("earlier-observation"), source.index("later-observation"))

    def test_scalar_replay_outcome_is_visible(self):
        source = self.publish(replay={"title": "Binary replay", "outcome": 0,
                                      "resolved_at": "2024-02-01T12:00:00Z",
                                      "baseline": {"probability": 0, "score": 0}})
        self.assertIn("Chronological observations", source)
        self.assertIn("2024-02-01T12:00:00Z", source)
        self.assertIn("Outcome: 0", source)
        self.assertIn("0.000", source)

    def test_probability_values_cannot_inject_css(self):
        source = self.publish([forecast(probability='0; background:url(https://example.org/evil)',
                                        baseline_probability=None)])
        self.assertNotIn("background:url", source)
        self.assertNotIn('class="bar"', source)
        self.assertIn("Not available", source)
        source = self.publish([forecast(probability=1, baseline_probability=0)])
        self.assertIn("--probability: 100.00%", source)
        self.assertIn("--probability: 0.00%", source)

    def test_unknown_replay_schema_remains_available(self):
        replay = {"title": "Future format", "predictions": [], "baseline": "unknown shape",
                  "event": None, "next": 7, "outcomes": [None, "entry"],
                  "opaque": {"details": ["<b>escaped in file, never embedded raw</b>"]}}
        source = self.publish(replay=replay)
        self.assertIn("Future format", source)
        self.assertIn("Detailed replay JSON", source)
        self.assertNotIn("<b>escaped", source)
        self.assertEqual(replay, json.loads((self.output / "replay.json").read_text()))

    def test_duplicate_ids_and_unknown_modes_fail_without_writes(self):
        for forecasts in ([forecast(), forecast()], [forecast(mode="unknown")]):
            with self.assertRaises(ValueError):
                self.publish(forecasts)
            self.assertFalse(self.output.exists())

    def test_output_must_remain_inside_repository(self):
        with self.assertRaisesRegex(ValueError, "inside this repository"):
            render(ROOT.parent / "outside-site-test", {}, [], {})
        with self.assertRaises(ValueError):
            render(ROOT, {}, [], {})
        self.assertFalse(self.output.exists())

    def test_symlink_destinations_are_rejected_before_any_write(self):
        real = self.work / "real"
        real.mkdir()
        self.output.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.publish()
        self.assertEqual(list(real.iterdir()), [])
        self.output.unlink()
        self.output.mkdir()
        (self.output / "forecasts").symlink_to(real, target_is_directory=True)
        for forecasts in ([], [forecast()]):
            with self.assertRaisesRegex(ValueError, "symlink"):
                self.publish(forecasts)
        self.assertFalse((self.output / "index.html").exists())
        (self.output / "forecasts").unlink()
        sentinel = self.work / "sentinel"
        sentinel.write_text("keep", encoding="utf-8")
        (self.output / "scores.json").symlink_to(sentinel)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.publish()
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertFalse((self.output / "index.html").exists())


if __name__ == "__main__":
    unittest.main()
