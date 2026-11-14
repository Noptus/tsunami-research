"""Synthetic contract fixtures; no network, external dependencies, or real-source claims.

Run with: python -m unittest discover -s tests -p 'test_cpi.py'
"""

import gzip
import hashlib
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tsunami import cpi


BLS = """<!doctype html><html><body><pre>
Transmission of material in this release is embargoed until
8:30 a.m. (ET) Tuesday, March 12, 2024
</pre><h1>CONSUMER PRICE INDEX - FEBRUARY 2024</h1>
<p>The Consumer Price Index for All Urban Consumers (CPI-U) increased
0.4 percent in February on a seasonally adjusted basis, after rising 0.3
percent in January, the U.S. Bureau of Labor Statistics reported today.</p>
<p>Over the last 12 months, the all items index increased 3.2 percent before
seasonal adjustment.</p>
<p>The index for all items less food and energy rose 0.4 percent in February,
as it did in January.</p></body></html>"""


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = {
            "release_date": "2024-03-12",
            "embargo_at": "2024-03-12T08:30:00-04:00",
            "cutoff_at": "2024-03-12T08:35:00-04:00",
            "outcome_event_date": "2024-03-12",
            "outcome_next_date": "2024-03-13",
            "sources": {},
        }
        self._add_source("bls", "bls.html", BLS,
                         "https://www.bls.gov/news.release/archives/cpi_03122024.htm",
                         "2024-03-12T08:30:00-04:00", "original BLS HTML archive")
        rows = ["observation_date,DGS2_20240311"]
        day = date(2017, 1, 3)
        level = Decimal("2.00")
        self.training_values = {}
        index = 0
        while day < date(2024, 3, 12):
            if day.weekday() < 5:
                # Include up, down and flat observations; no holiday-calendar claim.
                level += (Decimal("0.01"), Decimal("-0.01"), Decimal("0"))[index % 3]
                self.training_values[day] = level
                rows.append(f"{day},{level}")
                index += 1
            day += timedelta(days=1)
        self._add_source("training", "training.csv", "\n".join(rows) + "\n",
                         "https://alfred.stlouisfed.org/graph/?id=DGS2&vintage_date=2024-03-11",
                         "2024-03-11T20:00:00-04:00", "fixed ALFRED vintage")
        self.anchor_day = max(self.training_values)
        self.anchor = self.training_values[self.anchor_day]
        self._add_source("outcome", "outcome.csv",
                         f"date,DGS2\n2024-03-12,{self.anchor + Decimal('0.05')}\n"
                         f"2024-03-13,{self.anchor + Decimal('0.03')}\n",
                         "https://alfred.stlouisfed.org/graph/?id=DGS2&revision_date=2024-03-14",
                         "2024-03-14T09:00:00-04:00", "archival fixed vintage")
        self.save()

    def _add_source(self, name, path, text, url, available, evidence):
        data = text.encode("utf-8")
        (self.root / path).write_bytes(data)
        self.manifest["sources"][name] = {
            "path": path, "url": url, "sha256": hashlib.sha256(data).hexdigest(),
            "available_at": available, "retrieved_at": "2025-01-01T00:00:00+00:00",
            "evidence_type": evidence,
        }

    def save(self):
        (self.root / "event.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def source_text(self, name):
        return (self.root / self.manifest["sources"][name]["path"]).read_text(encoding="utf-8")

    def replace_source(self, name, text):
        record = self.manifest["sources"][name]
        data = text.encode("utf-8")
        (self.root / record["path"]).write_bytes(data)
        record["sha256"] = hashlib.sha256(data).hexdigest()
        self.save()

    def assert_rejected(self, pattern):
        self.save()
        with self.assertRaisesRegex(cpi.ReplayError, pattern):
            cpi.replay(self.root)

    def test_complete_audit_and_scores(self):
        result = cpi.replay(self.root)
        self.assertEqual(result["mode"], "archived replay")
        self.assertTrue(result["eligibility"]["archived_replay"])
        self.assertFalse(result["eligibility"]["forecast_inputs_retrieved_by_cutoff"])
        self.assertFalse(result["eligibility"]["prospective_forecast"])
        self.assertEqual(result["cpi"]["headline_monthly_percent"], 0.4)
        self.assertEqual(result["cpi"]["core_monthly_percent"], 0.4)
        self.assertIn("not a learned or tested", result["cpi"]["thesis"])
        self.assertEqual(result["predictions"]["baseline"]["samples"], 252)
        self.assertEqual(result["predictions"]["baseline"]["up"], 84)
        self.assertAlmostEqual(result["predictions"]["baseline"]["probability_up"], 85 / 254)
        self.assertEqual(result["training"]["vintage_date"], "2024-03-11")
        self.assertEqual(result["outcome_series"]["vintage_date"], "2024-03-14")
        self.assertEqual(result["event"]["reference_date"], "2024-03-11")
        self.assertEqual(result["event"]["change_basis_points"], 5)
        self.assertEqual(result["event"]["up"], 1)
        self.assertEqual(result["next"]["change_basis_points"], -2)
        self.assertIsNone(result["event"]["brier"])
        self.assertEqual(result["next"]["reference_date"], "2024-03-12")
        for name, model in result["predictions"].items():
            self.assertEqual(model["mode"], "archived replay")
            self.assertAlmostEqual(result["next"]["brier"][name], model["probability_up"] ** 2)
        self.assertEqual(result["baseline_brier"], result["next"]["brier"]["baseline"])
        for name, source in result["sources"].items():
            for key, value in self.manifest["sources"][name].items():
                self.assertEqual(source[key], value)
            self.assertTrue(source["contained_path_verified"])
            self.assertEqual(source["sha256"], source["verified_sha256"])
        self.assertFalse(result["sources"]["outcome"]["available_by_cutoff"])
        json.dumps(result, allow_nan=False)

    def test_predictions_frozen_before_outcome_read_or_hash(self):
        operations = []
        original_read = cpi._verified_text
        original_predict = cpi._predict

        def read(source):
            operations.append(source["name"])
            return original_read(source)

        def predict(training):
            self.assertNotIn("outcome", operations)
            result = original_predict(training)
            operations.append("predictions complete")
            return result

        with patch.object(cpi, "_verified_text", side_effect=read), patch.object(cpi, "_predict", side_effect=predict):
            cpi.replay(self.root)
        self.assertEqual(operations, ["bls", "training", "predictions complete", "outcome"])

    def test_outcome_bytes_not_read_on_forecast_failure(self):
        self.replace_source("training", "date,DGS2\n2024-03-12,4.2\n")
        original = Path.read_bytes
        opened = []

        def tracked(path):
            opened.append(path.name)
            return original(path)

        with patch.object(Path, "read_bytes", tracked):
            self.assert_rejected("lookahead")
        self.assertNotIn("outcome.csv", opened)

    def test_outcome_values_do_not_change_predictions(self):
        first = cpi.replay(self.root)
        self.replace_source("outcome", "date,DGS2\n2024-03-12,1.0\n2024-03-13,1.1\n")
        second = cpi.replay(self.root)
        self.assertEqual(first["predictions"], second["predictions"])
        self.assertNotEqual(first["next"]["brier"], second["next"]["brier"])
        self.assertEqual(second["next"]["up"], 1)
        for name, model in second["predictions"].items():
            self.assertAlmostEqual(second["next"]["brier"][name], (model["probability_up"] - 1) ** 2)

    def test_cpi_narrative_does_not_change_probability(self):
        first = cpi.replay(self.root)
        self.replace_source("bls", BLS.replace("increased\n0.4 percent", "fell\n0.2 percent")
                            .replace("rose 0.4 percent", "was unchanged"))
        second = cpi.replay(self.root)
        self.assertEqual(first["predictions"], second["predictions"])
        self.assertEqual(second["cpi"]["headline_monthly_percent"], -0.2)
        self.assertEqual(second["cpi"]["core_monthly_percent"], 0)
        self.assertIn("cooling", second["cpi"]["thesis"])

    def test_tamper_each_source(self):
        for name in self.manifest["sources"]:
            with self.subTest(name=name):
                path = self.root / self.manifest["sources"][name]["path"]
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                self.assert_rejected(f"SHA256 mismatch: {name}")
                path.write_bytes(original)

    def test_forecast_availability_after_cutoff(self):
        for name in ("bls", "training"):
            with self.subTest(name=name):
                record = self.manifest["sources"][name]
                original = record["available_at"]
                record["available_at"] = "2024-03-12T12:35:01+00:00"
                self.assert_rejected("Forecast input available after cutoff")
                record["available_at"] = original

    def test_outcome_must_be_strictly_after_cutoff(self):
        self.manifest["sources"]["outcome"]["available_at"] = self.manifest["cutoff_at"]
        self.assert_rejected("strictly after cutoff")

    def test_retrieval_before_availability(self):
        self.manifest["sources"]["training"]["retrieved_at"] = "2016-01-01T00:00:00Z"
        self.assert_rejected("Retrieval precedes")

    def test_contemporaneous_capture_is_still_archived_replay(self):
        for name in ("bls", "training"):
            self.manifest["sources"][name]["retrieved_at"] = self.manifest["cutoff_at"]
        self.save()
        result = cpi.replay(self.root)
        self.assertTrue(result["eligibility"]["forecast_inputs_retrieved_by_cutoff"])
        self.assertFalse(result["eligibility"]["prospective_forecast"])

    def test_naive_timestamps_every_field(self):
        for target, key in [(self.manifest, "embargo_at"), (self.manifest, "cutoff_at")] + [
            (record, key) for record in self.manifest["sources"].values()
            for key in ("available_at", "retrieved_at")
        ]:
            with self.subTest(key=key, target=target):
                original = target[key]
                target[key] = "2024-03-12T08:30:00"
                self.assert_rejected("timezone-aware")
                target[key] = original

    def test_manifest_date_validation(self):
        for key, value, pattern in (
            ("release_date", "2024-02-30", "Invalid date"),
            ("release_date", "20240312", "YYYY-MM-DD"),
            ("release_date", "2024-03-11", "Eastern embargo"),
            ("outcome_event_date", "2024-03-11", "Outcome event"),
            ("outcome_next_date", "2024-03-12", "next date"),
            ("cutoff_at", "2024-03-12T08:29:59-04:00", "at or after embargo"),
            ("cutoff_at", "2024-03-13T08:35:00-04:00", "on release day"),
        ):
            with self.subTest(key=key, value=value):
                original = self.manifest[key]
                self.manifest[key] = value
                self.assert_rejected(pattern)
                self.manifest[key] = original

    def test_utc_timestamps_match_dst_embargo(self):
        self.manifest["embargo_at"] = "2024-03-12T12:30:00Z"
        self.manifest["cutoff_at"] = "2024-03-12T12:35:00+00:00"
        self.save()
        cpi.replay(self.root)

    def test_bls_cannot_be_available_before_embargo(self):
        self.manifest["sources"]["bls"]["available_at"] = "2024-03-12T08:29:00-04:00"
        self.assert_rejected("before embargo")

    def test_containment(self):
        record = self.manifest["sources"]["training"]
        original = record["path"]
        for bad in ("../training.csv", str(self.root / "training.csv"), "absent.csv"):
            with self.subTest(path=bad):
                record["path"] = bad
                self.assert_rejected("path")
        record["path"] = original

    def test_symlink_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "data.csv"
            target.write_text(self.source_text("training"), encoding="utf-8")
            link = self.root / "escape.csv"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            self.manifest["sources"]["training"]["path"] = "escape.csv"
            self.assert_rejected("not contained")

    def test_duplicate_json_keys(self):
        (self.root / "event.json").write_text('{"release_date":"2024-03-12","release_date":"2024-03-13"}')
        with self.assertRaisesRegex(cpi.ReplayError, "Duplicate JSON key"):
            cpi.replay(self.root)

    def test_training_rejects_event_and_future_even_when_missing(self):
        original = self.source_text("training")
        for day, value in (("2024-03-12", "4.2"), ("2024-03-13", "."), ("2024-03-12", "")):
            with self.subTest(day=day, value=value):
                self.replace_source("training", original + f"{day},{value}\n")
                self.assert_rejected("Training lookahead")

    def test_bad_csv_rows(self):
        for row, pattern in (
            ("2024-02-30,4.2", "Invalid date"),
            ("20240311,4.2", "YYYY-MM-DD"),
            ("2024-03-11,nan", "Malformed DGS2"),
            ("2024-03-11,inf", "Malformed DGS2"),
            ("2024-03-11,4x", "Malformed DGS2"),
            ("2024-03-11,1e2", "Malformed DGS2"),
            ("2024-03-11,4.2,extra", "Malformed CSV"),
            ("", "Malformed CSV"),
            ("2024-03-11,4.2\n2024-03-11,.", "Duplicate observation"),
        ):
            with self.subTest(row=row):
                self.replace_source("training", "date,DGS2\n" + row + "\n")
                self.assert_rejected(pattern)

    def test_missing_values_and_unsorted_rows(self):
        self.replace_source("training", "﻿date,DGS2\n2024-03-11,4.1\n2024-03-08,\n"
                            "2024-03-06,4.0\n2024-03-07,.\n")
        result = cpi.replay(self.root)
        self.assertEqual(result["training"]["missing"], 2)
        self.assertEqual(result["training"]["observed"], 2)
        self.assertEqual(result["predictions"]["baseline"]["probability_up"], 2 / 3)
        self.assertTrue(result["predictions"]["challenger"]["fallback_to_baseline"])

    def test_no_observations_and_insufficient_training(self):
        for text, pattern in (("date,DGS2\n2024-03-11,.\n", "no observed"),
                              ("date,DGS2\n2024-03-11,4.0\n", "At least two")):
            self.replace_source("training", text)
            self.assert_rejected(pattern)

    def test_invalid_headers(self):
        for header in ("DATE,DGS2", "date,DGS10", "date,DGS2,other", "date,date", "date,DGS2_20240230"):
            self.replace_source("training", header + "\n2024-03-08,4.0\n2024-03-11,4.1\n")
            self.assert_rejected("header|Invalid date")

    def test_training_vintage_must_be_prior_calendar_day(self):
        self.manifest["sources"]["training"]["url"] = (
            "https://alfred.stlouisfed.org/graph/?id=DGS2&vintage_date=2024-03-08")
        self.assert_rejected("conflicting fixed vintage")

    def test_conflicting_duplicate_vintage_parameters(self):
        self.manifest["sources"]["training"]["url"] += "&vintage_date=2024-03-12"
        self.assert_rejected("conflicting fixed vintage")

    def test_header_can_supply_vintage(self):
        self.manifest["sources"]["training"]["url"] = "https://alfred.stlouisfed.org/graph/?id=DGS2"
        self.save()
        result = cpi.replay(self.root)
        self.assertEqual(result["training"]["vintage_evidence"], ["header DGS2_20240311"])

    def test_missing_vintage_rejected(self):
        self.manifest["sources"]["outcome"]["url"] = "https://alfred.stlouisfed.org/graph/?id=DGS2"
        self.assert_rejected("Missing or conflicting fixed vintage")

    def test_fixed_realtime_interval(self):
        self.manifest["sources"]["training"]["url"] = (
            "https://api.stlouisfed.org/fred/series/observations?series_id=DGS2"
            "&realtime_start=2024-03-11&realtime_end=2024-03-11")
        self.save()
        cpi.replay(self.root)
        self.manifest["sources"]["training"]["url"] = (
            "https://api.stlouisfed.org/fred/series/observations?series_id=DGS2"
            "&realtime_start=2024-03-11")
        self.assert_rejected("both fixed endpoints")

    def test_outcome_vintage_is_availability_day_not_retrieval_day(self):
        self.manifest["sources"]["outcome"]["url"] = (
            "https://alfred.stlouisfed.org/graph/?id=DGS2&revision_date=2025-01-01")
        self.assert_rejected("expected 2024-03-14")

    def test_outcome_missing_duplicate_and_future(self):
        for rows, pattern in (
            ("2024-03-12,.\n2024-03-13,4.0\n", "observation missing"),
            ("2024-03-12,4.0\n2024-03-13,\n", "observation missing"),
            ("2024-03-12,4.0\n2024-03-12,4.1\n2024-03-13,4.2\n", "Duplicate observation"),
            ("2024-03-12,4.0\n2024-03-13,4.1\n2024-03-15,4.2\n", "after its archival vintage"),
        ):
            self.replace_source("outcome", "date,DGS2\n" + rows)
            self.assert_rejected(pattern)

    def test_next_date_must_be_next_observed_session(self):
        self.manifest["outcome_next_date"] = "2024-03-14"
        self.replace_source("outcome", self.source_text("outcome") + "2024-03-14,4.0\n")
        self.assert_rejected("not the next observed session")

    def test_outcome_reveals_missing_training_anchor(self):
        self.replace_source("training", "date,DGS2\n2024-03-07,4.0\n2024-03-08,4.1\n")
        self.replace_source("outcome", "date,DGS2\n2024-03-11,4.2\n2024-03-12,4.3\n2024-03-13,4.4\n")
        self.assert_rejected("anchor misses")

    def test_flat_sessions_are_non_up(self):
        self.replace_source("outcome", f"date,DGS2\n2024-03-12,{self.anchor}\n2024-03-13,{self.anchor}\n")
        result = cpi.replay(self.root)
        self.assertEqual(result["event"]["up"], 0)
        self.assertEqual(result["next"]["up"], 0)
        self.assertIsNone(result["event"]["brier"])
        for name, model in result["predictions"].items():
            self.assertEqual(result["next"]["brier"][name], model["probability_up"] ** 2)

    def test_outcome_vintage_uses_eastern_calendar(self):
        self.manifest["sources"]["outcome"]["available_at"] = "2024-03-15T01:00:00Z"
        self.save()
        self.assertEqual(cpi.replay(self.root)["outcome_series"]["vintage_date"], "2024-03-14")

    def test_gzip_evidence_hashes_stored_and_original_bytes(self):
        original = self.source_text("outcome").encode("utf-8")
        compressed = gzip.compress(original, mtime=0)
        record = self.manifest["sources"]["outcome"]
        record.update(encoding="gzip", source_sha256=hashlib.sha256(original).hexdigest(),
                      sha256=hashlib.sha256(compressed).hexdigest())
        (self.root / record["path"]).write_bytes(compressed)
        self.save()
        result = cpi.replay(self.root)
        self.assertEqual(result["sources"]["outcome"]["verified_sha256"], record["sha256"])
        record["source_sha256"] = "0" * 64
        self.assert_rejected("Original source SHA256 mismatch")


class ModelTests(unittest.TestCase):
    def series(self, changes):
        level = Decimal("4.0")
        day = date(2020, 1, 1)
        result = {day: level}
        for change in changes:
            day += timedelta(days=1)
            level += Decimal(str(change))
            result[day] = level
        return result

    def test_exact_baseline_window_and_flats(self):
        result = cpi._predict(self.series([1] * 100 + [0] * 251 + [-1]))
        baseline = result["baseline"]
        self.assertEqual(baseline["samples"], 252)
        self.assertEqual(baseline["up"], 0)
        self.assertEqual(baseline["non_up"], 252)
        self.assertEqual(baseline["probability_up"], 1 / 254)

    def test_sign_conditioning_uses_lag_not_target(self):
        result = cpi._predict(self.series([1, -1] * 30 + [1]))
        challenger = result["challenger"]
        self.assertEqual(challenger["latest_lag_sign"], 1)
        self.assertEqual(challenger["samples"], 30)
        self.assertEqual(challenger["up"], 0)
        self.assertEqual(challenger["probability_up"], 1 / 32)
        self.assertFalse(challenger["fallback_to_baseline"])

    def test_minimum_twenty_pairs_boundary(self):
        for count, fallback in ((20, True), (21, False)):
            with self.subTest(changes=count):
                result = cpi._predict(self.series([1] * count))
                self.assertEqual(result["challenger"]["samples"], count - 1)
                self.assertEqual(result["challenger"]["fallback_to_baseline"], fallback)
                expected = result["baseline"]["probability_up"] if fallback else 21 / 22
                self.assertEqual(result["challenger"]["probability_up"], expected)

    def test_challenger_window(self):
        result = cpi._predict(self.series([1] * 400))
        self.assertEqual(result["challenger"]["candidate_pairs"], 252)
        self.assertEqual(result["challenger"]["samples"], 252)

    def test_weekend_prior_vintage_is_calendar_not_business_day(self):
        source = {"record": {"url": "https://alfred.stlouisfed.org/graph/?vintage_date=2024-03-10"}}
        observations, facts = cpi._parse_series(
            "date,DGS2\n2024-03-08,4.0\n", source, date(2024, 3, 11) - timedelta(days=1))
        self.assertIn(date(2024, 3, 8), observations)
        self.assertEqual(facts["vintage_date"], "2024-03-10")


class BLSParsingTests(unittest.TestCase):
    def parse(self, text=BLS, instant="2024-03-12T08:30:00-04:00"):
        return cpi._parse_bls(text, datetime.fromisoformat(instant))

    def test_positive_negative_stable_wording(self):
        for phrase, expected in (("rose 0.2 percent", 0.2), ("increased by 0.2 percent", 0.2),
                                 ("fell 0.2 percent", -0.2), ("declined 0.2 percent", -0.2),
                                 ("decreased 0.2 percent", -0.2), ("was unchanged", 0),
                                 ("remained stable", 0), ("showed no change", 0)):
            with self.subTest(phrase=phrase):
                text = BLS.replace("increased\n0.4 percent", phrase).replace("rose 0.4 percent", phrase)
                result = self.parse(text)
                self.assertEqual(result["headline_monthly_percent"], expected)
                self.assertEqual(result["core_monthly_percent"], expected)

    def test_html_entities_inline_markup_and_hidden_text(self):
        text = BLS.replace("0.4 percent", "<strong>0.4</strong>&nbsp;percent")
        text = text.replace("<body>", "<body><script>The Consumer Price Index for All Urban Consumers (CPI-U) fell 99 percent.</script>")
        result = self.parse(text)
        self.assertEqual(result["headline_monthly_percent"], 0.4)

    def test_mixed_context(self):
        result = self.parse(BLS.replace("rose 0.4 percent", "declined 0.1 percent"))
        self.assertIn("mixed", result["thesis"])

    def test_previous_month_comparison_is_not_current_change(self):
        text = BLS.replace("increased\n0.4 percent", "was unchanged").replace(
            "after rising 0.3 percent in January", "after increased 0.3 percent in January")
        text = text.replace("rose 0.4 percent", "was unchanged").replace(
            "as it did in January", "after the index rose 0.4 percent in January")
        result = self.parse(text)
        self.assertEqual(result["headline_monthly_percent"], 0)
        self.assertEqual(result["core_monthly_percent"], 0)

    def test_unknown_primary_action_cannot_borrow_comparison_rate(self):
        text = BLS.replace("rose 0.4 percent in February,\nas it did in January",
                           "moved in February after it rose 0.4 percent in January")
        with self.assertRaisesRegex(cpi.ReplayError, "Unrecognized BLS monthly change"):
            self.parse(text)

    def test_missing_or_wrong_embargo(self):
        for text in (BLS.replace("embargoed until", "published at"),
                     BLS.replace("8:30", "8:31"),
                     BLS.replace("March 12", "March 13"),
                     BLS.replace("Tuesday", "Monday"), BLS + BLS):
            with self.subTest(text=text[:100]):
                with self.assertRaises(cpi.ReplayError):
                    self.parse(text)

    def test_dst_labels_and_utc_equivalence(self):
        self.parse(BLS.replace("(ET)", "(EDT)"), "2024-03-12T12:30:00+00:00")
        with self.assertRaisesRegex(cpi.ReplayError, "DST"):
            self.parse(BLS.replace("(ET)", "(EST)"))
        winter = BLS.replace("Tuesday, March 12", "Tuesday, February 13").replace("(ET)", "(EST)")
        self.parse(winter, "2024-02-13T13:30:00+00:00")
        with self.assertRaisesRegex(cpi.ReplayError, "DST"):
            self.parse(winter.replace("(EST)", "(EDT)"), "2024-02-13T13:30:00+00:00")

    def test_nonexistent_and_ambiguous_dst_wall_times(self):
        for day, time, instant in (
            ("Sunday, March 10", "2:30", "2024-03-10T02:30:00-05:00"),
            ("Sunday, November 3", "1:30", "2024-11-03T01:30:00-04:00"),
        ):
            text = BLS.replace("Tuesday, March 12", day).replace("8:30", time)
            with self.assertRaisesRegex(cpi.ReplayError, "Ambiguous or nonexistent"):
                self.parse(text, instant)

    def test_fail_closed_on_unrecognized_or_annual_only_prose(self):
        for text in (
            BLS.replace("The Consumer Price Index for All Urban Consumers (CPI-U)", "CPI"),
            BLS.replace("The index for all items less food and energy", "Another index"),
            BLS.replace("increased\n0.4 percent", "surprised markets"),
            BLS.replace("seasonally adjusted basis", "unadjusted basis"),
            BLS.replace("rose 0.4 percent in February", "rose 3.8 percent over the last 12 months"),
            BLS.replace("rose 0.4 percent in February", "rose 0.4 percent"),
        ):
            with self.subTest(text=text[-200:]):
                with self.assertRaises(cpi.ReplayError):
                    self.parse(text)


if __name__ == "__main__":
    unittest.main()
