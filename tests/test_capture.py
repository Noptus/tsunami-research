"""Offline checks for the live-capture timing and evidence contract."""

from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from tsunami import capture
from tsunami.cpi import ReplayError
from test_cpi import BLS


EMBARGO = datetime.fromisoformat("2024-03-12T12:30:00Z")


def rates() -> bytes:
    day = EMBARGO.date() - timedelta(days=400)
    records = ["observation_date,DGS2_20240311"]
    while day < EMBARGO.date():
        if day.weekday() < 5:
            value = 4 + (day.day % 7) / 100
            records.append(f"{day},{value:.2f}")
        day += timedelta(days=1)
    return ("\n".join(records) + "\n").encode()


class CaptureTests(TestCase):
    def test_persists_timestamped_inputs_without_outcome(self):
        with TemporaryDirectory() as temp:
            root = Path(temp) / "capture"
            with patch.object(capture, "utcnow", return_value=EMBARGO + timedelta(minutes=1)), \
                 patch.object(capture, "_get", side_effect=(BLS.encode(), rates())) as fetch:
                result = capture.capture_cpi(EMBARGO.isoformat(), root)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(json.loads((root / "forecast.json").read_text()), result)
            self.assertEqual(result["predictions"]["baseline"]["samples"], 252)
            self.assertEqual(set(result["sources"]), {"bls", "training"})
            for source in result["sources"].values():
                self.assertEqual(sha256((root / source["path"]).read_bytes()).hexdigest(), source["sha256"])

    def test_clock_outside_cutoff_never_fetches(self):
        with patch.object(capture, "utcnow", return_value=EMBARGO - timedelta(seconds=1)), \
             patch.object(capture, "_get") as fetch:
            with self.assertRaisesRegex(ReplayError, "15-minute cutoff"):
                capture.capture_cpi(EMBARGO.isoformat(), Path("unused"))
            fetch.assert_not_called()

    def test_fetch_crosses_cutoff_and_never_seals(self):
        times = [EMBARGO, EMBARGO, EMBARGO + timedelta(minutes=16), EMBARGO + timedelta(minutes=16)]
        with TemporaryDirectory() as temp:
            root = Path(temp) / "capture"
            with patch.object(capture, "utcnow", side_effect=times), \
                 patch.object(capture, "_get", side_effect=(BLS.encode(), rates())):
                with self.assertRaisesRegex(ReplayError, "crossed the evidence cutoff"):
                    capture.capture_cpi(EMBARGO.isoformat(), root)
            self.assertFalse(root.exists())
