"""Synthetic ablation fixtures; no network, external dependencies, or real-source claims.

Run with: python -m unittest discover -s tests -p 'test_study.py'
"""

import gzip
import hashlib
import json
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from tsunami import cpi, study


EASTERN = ZoneInfo("America/New_York")

# A minimal BLS opening that satisfies tsunami.cpi._parse_bls. The monthly
# headline and core changes are templated per event so the fixtures exercise
# real parsing rather than injected numbers.
BLS_TEMPLATE = """<!doctype html><html><body><pre>
Transmission of material in this release is embargoed until
8:30 a.m. (ET) {weekday}, {month} {day}, {year}
</pre><h1>CONSUMER PRICE INDEX</h1>
<p>The Consumer Price Index for All Urban Consumers (CPI-U) {headline} for the
month on a seasonally adjusted basis, the U.S. Bureau of Labor Statistics
reported today.</p>
<p>The index for all items less food and energy {core} for the month.</p>
</body></html>"""

MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")


def phrase(value):
    if value > 0:
        return f"rose {value:.1f} percent"
    if value < 0:
        return f"fell {abs(value):.1f} percent"
    return "was unchanged"


def build_bls(release, headline, core):
    embargo = datetime.combine(release, time(8, 30), EASTERN)
    return BLS_TEMPLATE.format(
        weekday=release.strftime("%A"), month=MONTHS[release.month - 1],
        day=release.day, year=release.year,
        headline=phrase(headline), core=phrase(core)), embargo


class StudyFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = {"events": [], "yields": {}}
        self.yields = {}
        self._build_events()

    def _build_events(self, count=40):
        # Monthly CPI releases on the 12th; one DGS2 observation each prior,
        # release, and next business day so every event can be labeled.
        release = date(2018, 1, 12)
        level = Decimal("2.00")
        day = date(2017, 12, 1)
        # Seed enough history before the first release.
        while day < release - timedelta(days=1):
            if day.weekday() < 5:
                self.yields[day] = level
                level += Decimal("0.01")
            day += timedelta(days=1)
        for index in range(count):
            # Deterministic but varied headline/core signs.
            headline = (0.4, -0.2, 0.0, 0.3)[index % 4]
            core = (0.3, -0.1, 0.1, 0.0)[index % 4]
            self._add_event(release, headline, core)
            # Prior, release, next business-day observations around each release.
            prior = self._prev_weekday(release)
            nxt = self._next_weekday(release)
            # Alternate the lag direction and the realized next-session move.
            step = Decimal("0.02") if index % 2 == 0 else Decimal("-0.02")
            self.yields[prior] = level
            self.yields[release] = level + step
            self.yields[nxt] = level + step + (Decimal("0.03") if (index + headline) and index % 3 else Decimal("-0.01"))
            level = self.yields[nxt]
            release = self._add_month(release)
        self.last_release = self._prev_release(release)
        self._write_yields()
        self.save()

    @staticmethod
    def _prev_weekday(day):
        day -= timedelta(days=1)
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        return day

    @staticmethod
    def _next_weekday(day):
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        return day

    @staticmethod
    def _add_month(day):
        month = day.month + 1
        year = day.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        return date(year, month, 12)

    @staticmethod
    def _prev_release(day):
        month = day.month - 1
        year = day.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        return date(year, month, 12)

    def _add_event(self, release, headline, core):
        text, embargo = build_bls(release, headline, core)
        original = text.encode("utf-8")
        compressed = gzip.compress(original, mtime=0)
        name = f"events/{release.isoformat()}.html.gz"
        (self.root / name).parent.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_bytes(compressed)
        self.manifest["events"].append({
            "release_date": release.isoformat(),
            "embargo_at": embargo.isoformat(),
            "source_url": f"https://www.bls.gov/news.release/archives/cpi_{release:%m%d%Y}.htm",
            "path": name,
            "sha256": hashlib.sha256(compressed).hexdigest(),
            "source_sha256": hashlib.sha256(original).hexdigest(),
            "retrieved_at": "2025-01-01T00:00:00+00:00",
        })

    def _write_yields(self):
        vintage = max(self.yields)
        rows = [f"observation_date,DGS2_{vintage:%Y%m%d}"]
        for day in sorted(self.yields):
            rows.append(f"{day},{self.yields[day]}")
        text = "\n".join(rows) + "\n"
        data = text.encode("utf-8")
        (self.root / "yields.csv").write_bytes(data)
        self.manifest["yields"] = {
            "path": "yields.csv",
            "sha256": hashlib.sha256(data).hexdigest(),
            "url": f"https://alfred.stlouisfed.org/graph/?id=DGS2&vintage_date={vintage.isoformat()}",
            "vintage_date": vintage.isoformat(),
            "retrieved_at": "2025-01-01T00:00:00+00:00",
        }

    def save(self):
        (self.root / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def assert_rejected(self, pattern):
        self.save()
        with self.assertRaisesRegex(cpi.ReplayError, pattern):
            study.study(self.root)


class StudyTests(StudyFixture):
    def test_complete_ablation(self):
        result = study.study(self.root)
        self.assertEqual(result["mode"], "archived ablation")
        self.assertEqual(result["counts"]["events"], 40)
        self.assertEqual(result["counts"]["minimum_training_events"], study.MIN_TRAINING_EVENTS)
        # Every event is labelable, so scored = labeled - minimum training.
        self.assertEqual(result["counts"]["labeled"], 40)
        self.assertEqual(result["counts"]["skipped"], 0)
        self.assertEqual(result["counts"]["scored"], 40 - study.MIN_TRAINING_EVENTS)
        self.assertEqual(result["summary"]["scored"], result["counts"]["scored"])
        json.dumps(result, allow_nan=False)

    def test_predictions_are_probabilities_and_delta_is_difference(self):
        result = study.study(self.root)
        for event in result["scored_events"]:
            for key in ("baseline_probability_up", "challenger_probability_up"):
                self.assertGreater(event[key], 0.0)
                self.assertLess(event[key], 1.0)
            self.assertIn(event["realized_up"], (0, 1))
            self.assertAlmostEqual(
                event["baseline_brier"], (event["baseline_probability_up"] - event["realized_up"]) ** 2)
            self.assertAlmostEqual(
                event["challenger_brier"], (event["challenger_probability_up"] - event["realized_up"]) ** 2)
            self.assertAlmostEqual(
                event["brier_delta"], event["challenger_brier"] - event["baseline_brier"])

    def test_summary_means_match_per_event_scores(self):
        result = study.study(self.root)
        scored = result["scored_events"]
        self.assertAlmostEqual(result["summary"]["mean_baseline_brier"],
                               sum(e["baseline_brier"] for e in scored) / len(scored))
        self.assertAlmostEqual(result["summary"]["mean_challenger_brier"],
                               sum(e["challenger_brier"] for e in scored) / len(scored))
        self.assertAlmostEqual(result["summary"]["mean_brier_delta"],
                               result["summary"]["mean_challenger_brier"]
                               - result["summary"]["mean_baseline_brier"])

    def test_cpi_text_does_not_affect_baseline(self):
        # Flipping the CPI narrative must leave the market-only baseline untouched
        # while generally changing the challenger that consumes those features.
        first = study.study(self.root)
        for record in self.manifest["events"][:2]:
            path = self.root / record["path"]
            original = gzip.decompress(path.read_bytes()).decode("utf-8")
            flipped = original.replace("rose", "TEMP").replace("fell", "rose").replace("TEMP", "fell")
            data = flipped.encode("utf-8")
            compressed = gzip.compress(data, mtime=0)
            path.write_bytes(compressed)
            record["sha256"] = hashlib.sha256(compressed).hexdigest()
            record["source_sha256"] = hashlib.sha256(data).hexdigest()
        self.save()
        second = study.study(self.root)
        base_first = [e["baseline_probability_up"] for e in first["scored_events"]]
        base_second = [e["baseline_probability_up"] for e in second["scored_events"]]
        self.assertEqual(base_first, base_second)
        chall_first = [e["challenger_probability_up"] for e in first["scored_events"]]
        chall_second = [e["challenger_probability_up"] for e in second["scored_events"]]
        self.assertNotEqual(chall_first, chall_second)

    def test_predict_before_score_order(self):
        # The study must stage every probability before any realized outcome is
        # scored. Trace when predictions are computed and confirm the current
        # event's own label never influences its challenger probability.
        order = []
        original_challenger = study._challenger_probability

        def traced_challenger(train, current, features=study.FEATURES):
            order.append("predict")
            self.assertNotIn("up", current)
            self.assertNotIn("next_change_basis_points", current)
            return original_challenger(train, current, features)

        with mock.patch.object(study, "_challenger_probability", traced_challenger):
            result = study.study(self.root)
        self.assertGreater(order.count("predict"), 0)
        self.assertTrue(all("baseline_brier" in e for e in result["scored_events"]))

    def test_no_imputation_skips_unobserved_release(self):
        # Drop the release-day observation for one event: it must be skipped,
        # not imputed, and excluded from scoring.
        target = self.manifest["events"][5]["release_date"]
        drop = date.fromisoformat(target)
        del self.yields[drop]
        self._write_yields()
        self.save()
        result = study.study(self.root)
        reasons = {e["release_date"]: e["reason"] for e in result["skipped_events"]}
        self.assertIn(target, reasons)
        self.assertIn("release", reasons[target])
        self.assertNotIn(target, {e["release_date"] for e in result["scored_events"]})

    def test_label_availability_calendar_bound(self):
        # Push one event's next observation far past the release so the
        # conservative calendar bound skips it.
        target = date.fromisoformat(self.manifest["events"][6]["release_date"])
        nxt = self._next_weekday(target)
        far = target + timedelta(days=study.LABEL_GAP_CALENDAR_DAYS + 3)
        while far.weekday() >= 5:
            far += timedelta(days=1)
        self.yields[far] = self.yields.pop(nxt)
        self._write_yields()
        self.save()
        result = study.study(self.root)
        reasons = {e["release_date"]: e["reason"] for e in result["skipped_events"]}
        self.assertIn(target.isoformat(), reasons)
        self.assertIn("bound", reasons[target.isoformat()])

    def test_minimum_training_gate(self):
        # No event may be scored before the predeclared minimum history exists.
        result = study.study(self.root)
        for event in result["scored_events"]:
            self.assertGreaterEqual(event["training_events"], study.MIN_TRAINING_EVENTS)
        # The first scored event sees exactly the minimum.
        self.assertEqual(result["scored_events"][0]["training_events"], study.MIN_TRAINING_EVENTS)

    def test_source_links_recorded_per_event(self):
        result = study.study(self.root)
        by_date = {r["release_date"]: r for r in self.manifest["events"]}
        for event in result["scored_events"]:
            source = event["source"]
            record = by_date[event["release_date"]]
            self.assertEqual(source["source_url"], record["source_url"])
            self.assertEqual(source["sha256"], record["sha256"])
            self.assertEqual(source["source_sha256"], record["source_sha256"])
            self.assertTrue(source["source_url"].startswith("https://www.bls.gov"))

    def test_tamper_gzip_artifact(self):
        record = self.manifest["events"][0]
        path = self.root / record["path"]
        path.write_bytes(path.read_bytes() + b"x")
        self.assert_rejected("SHA256 mismatch: events")

    def test_tamper_original_source_hash(self):
        self.manifest["events"][0]["source_sha256"] = "0" * 64
        self.assert_rejected("Original source SHA256 mismatch")

    def test_tamper_yields(self):
        path = self.root / "yields.csv"
        path.write_bytes(path.read_bytes() + b" ")
        self.assert_rejected("SHA256 mismatch: yields")

    def test_yields_vintage_before_last_event_rejected(self):
        self.manifest["yields"]["url"] = (
            "https://alfred.stlouisfed.org/graph/?id=DGS2&vintage_date=2018-01-01")
        self.manifest["yields"]["vintage_date"] = "2018-01-01"
        self.assert_rejected("frozen at or after the last event")

    def test_duplicate_release_date_rejected(self):
        duplicate = dict(self.manifest["events"][0])
        self.manifest["events"].append(duplicate)
        self.assert_rejected("Duplicate event release date")

    def test_embargo_must_match_manifest(self):
        self.manifest["events"][0]["embargo_at"] = "2018-01-12T09:30:00-05:00"
        self.assert_rejected("BLS embargo does not match")

    def test_missing_manifest_keys(self):
        del self.manifest["yields"]
        self.assert_rejected("must contain events and yields")

    def test_path_containment(self):
        self.manifest["events"][0]["path"] = "../escape.gz"
        self.assert_rejected("path")


class LogisticTests(unittest.TestCase):
    def test_separable_signal_is_learned(self):
        # Label perfectly determined by a single feature; the fitted logistic
        # should assign a high probability to a positive instance.
        train = []
        for i in range(60):
            value = 1.0 if i % 2 == 0 else -1.0
            train.append({"lag_sign": value, "headline_monthly_percent": value,
                          "core_monthly_percent": value, "up": int(value > 0)})
        current = {"lag_sign": 1.0, "headline_monthly_percent": 1.0, "core_monthly_percent": 1.0}
        result = study._challenger_probability(train, current)
        self.assertGreater(result["probability_up"], 0.5)
        self.assertEqual(result["samples"], 60)

    def test_constant_feature_standardization_is_safe(self):
        # A zero-variance feature must not raise (std falls back to 1.0).
        train = [{"lag_sign": 0.0, "headline_monthly_percent": 0.0,
                  "core_monthly_percent": 0.0, "up": i % 2} for i in range(30)]
        current = {"lag_sign": 0.0, "headline_monthly_percent": 0.0, "core_monthly_percent": 0.0}
        result = study._challenger_probability(train, current)
        self.assertAlmostEqual(result["probability_up"], 0.5, places=2)

    def test_baseline_is_event_type_laplace(self):
        train = [{"lag_sign": 1, "up": 1} for _ in range(3)] + \
                [{"lag_sign": 1, "up": 0} for _ in range(1)] + \
                [{"lag_sign": -1, "up": 0} for _ in range(5)]
        result = study._baseline_probability(train, 1)
        self.assertEqual(result["samples"], 4)
        self.assertEqual(result["up"], 3)
        self.assertAlmostEqual(result["probability_up"], (3 + 1) / (4 + 2))

    def test_baseline_unseen_event_type_uses_prior(self):
        result = study._baseline_probability([{"lag_sign": 1, "up": 1}], -1)
        self.assertEqual(result["samples"], 0)
        self.assertAlmostEqual(result["probability_up"], 0.5)


if __name__ == "__main__":
    unittest.main()
