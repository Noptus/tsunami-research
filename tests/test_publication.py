"""Publication attestation tests; the GitHub HTTP boundary is fully mocked.

Run with: python -m unittest discover -s tests -p 'test_publication.py'

No socket is opened: ``publication._fetch`` is replaced so the GitHub API and
asset downloads are served from in-memory fixtures. All timestamps are fixed
historical dates. The local issue on disk is authored to match the asset bytes
exactly, so byte-equality and the SHA256 asset digest both hold.
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tsunami import ledger, publication


WEEK = "2024-03-15"
REPO = "Noptus/tsunami-research"
TAG = f"forecast-{WEEK}"


def issue_records():
    return [{
        "id": "nvidia-guidance-raise",
        "week_ending": WEEK,
        "as_of_at": "2024-03-11T12:00:00+00:00",
        "issued_at": "2024-03-11T13:00:00+00:00",
        "deadline_at": "2024-03-15T20:00:00+00:00",
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
    }]


class AttestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.issue = issue_records()
        # The local issue bytes are the canonical serialization; the GitHub asset
        # download must equal these exact bytes.
        self.asset_bytes = ledger.json_bytes(self.issue)
        local = self.root / "forecasts" / f"{WEEK}.json"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(self.asset_bytes)
        self.asset_digest = hashlib.sha256(self.asset_bytes).hexdigest()
        self.html_url = f"https://github.com/{REPO}/releases/tag/{TAG}"
        self.download_url = f"https://github.com/{REPO}/releases/download/{TAG}/{WEEK}.json"
        self.release = {
            "tag_name": TAG,
            "draft": False,
            "prerelease": False,
            "immutable": True,
            "html_url": self.html_url,
            "id": 555,
            "published_at": "2024-03-11T13:30:00+00:00",
            "assets": [{
                "name": f"{WEEK}.json",
                "browser_download_url": self.download_url,
                "id": 777,
                "state": "uploaded",
                "digest": f"sha256:{self.asset_digest}",
            }],
        }

    def _fetch(self, release=None, asset_bytes=None):
        """Return a fake _fetch that serves the API JSON then the asset bytes."""
        release = self.release if release is None else release
        asset_bytes = self.asset_bytes if asset_bytes is None else asset_bytes

        def fake(url, accept="application/vnd.github+json"):
            if url.startswith("https://api.github.com/"):
                return json.dumps(release).encode()
            if url == self.download_url:
                return asset_bytes
            raise AssertionError(f"Unexpected fetch URL: {url}")

        return fake

    def test_attest_builds_receipt(self):
        with patch.object(publication, "_fetch", side_effect=self._fetch()):
            receipt = publication.attest(self.root, REPO, WEEK)
        self.assertEqual(receipt["url"], self.html_url)
        self.assertTrue(receipt["immutable"])
        self.assertEqual(receipt["release_id"], 555)
        self.assertEqual(receipt["asset_id"], 777)
        self.assertEqual(receipt["asset_sha256"], self.asset_digest)
        self.assertEqual(receipt["issue_sha256"], ledger.digest(self.issue))
        # The receipt references the dated release tag.
        self.assertTrue(receipt["url"].endswith(TAG))

    def test_matching_released_tag_required(self):
        release = dict(self.release, tag_name="forecast-2024-03-08")
        with patch.object(publication, "_fetch", side_effect=self._fetch(release=release)):
            with self.assertRaisesRegex(ValueError, "published and immutable"):
                publication.attest(self.root, REPO, WEEK)

    def test_mutable_release_rejected(self):
        release = dict(self.release, immutable=False)
        with patch.object(publication, "_fetch", side_effect=self._fetch(release=release)):
            with self.assertRaisesRegex(ValueError, "published and immutable"):
                publication.attest(self.root, REPO, WEEK)

    def test_draft_release_rejected(self):
        release = dict(self.release, draft=True)
        with patch.object(publication, "_fetch", side_effect=self._fetch(release=release)):
            with self.assertRaisesRegex(ValueError, "published and immutable"):
                publication.attest(self.root, REPO, WEEK)

    def test_exact_file_bytes_must_match_asset(self):
        tampered = self.asset_bytes + b" "
        with patch.object(publication, "_fetch", side_effect=self._fetch(asset_bytes=tampered)):
            # The digest fails before byte-equality because the asset digest is
            # computed over the downloaded payload.
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                publication.attest(self.root, REPO, WEEK)

    def test_asset_digest_mismatch_rejected(self):
        release = dict(self.release)
        release["assets"] = [dict(self.release["assets"][0], digest="sha256:" + "0" * 64)]
        with patch.object(publication, "_fetch", side_effect=self._fetch(release=release)):
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                publication.attest(self.root, REPO, WEEK)

    def test_downloaded_bytes_differ_from_local(self):
        # Asset download and its advertised digest agree with each other, but
        # differ from the bytes committed locally.
        other = self.asset_bytes.replace(b"0.6", b"0.7")  # a different payload
        release = dict(self.release)
        release["assets"] = [dict(self.release["assets"][0],
                                  digest="sha256:" + hashlib.sha256(other).hexdigest())]
        with patch.object(publication, "_fetch", side_effect=self._fetch(release=release, asset_bytes=other)):
            with self.assertRaisesRegex(ValueError, "differs from local bytes"):
                publication.attest(self.root, REPO, WEEK)

    def test_exactly_one_asset_required(self):
        release = dict(self.release)
        release["assets"] = release["assets"] + [dict(release["assets"][0])]
        with patch.object(publication, "_fetch", side_effect=self._fetch(release=release)):
            with self.assertRaisesRegex(ValueError, "exactly one public issue asset"):
                publication.attest(self.root, REPO, WEEK)

    def test_asset_must_be_uploaded(self):
        release = dict(self.release)
        release["assets"] = [dict(self.release["assets"][0], state="starter")]
        with patch.object(publication, "_fetch", side_effect=self._fetch(release=release)):
            with self.assertRaisesRegex(ValueError, "Invalid issue asset metadata"):
                publication.attest(self.root, REPO, WEEK)

    def test_only_prospective_issues_attested(self):
        archived = issue_records()
        archived[0]["mode"] = "archived_replay"
        (self.root / "forecasts" / f"{WEEK}.json").write_bytes(ledger.json_bytes(archived))
        with patch.object(publication, "_fetch", side_effect=self._fetch()):
            with self.assertRaisesRegex(ValueError, "Only prospective issues"):
                publication.attest(self.root, REPO, WEEK)

    def test_invalid_repository_rejected(self):
        with self.assertRaisesRegex(ValueError, "owner/name"):
            publication.attest(self.root, "not a repo", WEEK)

    def test_invalid_week_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid release week"):
            publication.attest(self.root, REPO, "2024/03/15")


if __name__ == "__main__":
    unittest.main()
