"""Verify a public GitHub release receipt against the exact downloadable issue."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen

from .ledger import _require, digest, read_json, validate_issue, validate_publication


REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def _fetch(url: str, accept: str = "application/vnd.github+json") -> bytes:
    request = Request(url, headers={
        "User-Agent": "Tsunami Research/0.1",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urlopen(request, timeout=30) as response:
        data = response.read(1_000_001)
    _require(len(data) <= 1_000_000, "GitHub response exceeds expected asset size")
    return data


def attest(root: Path, repository: str, week: str) -> dict:
    _require(REPOSITORY.fullmatch(repository) is not None, "Repository must be an owner/name")
    _require(re.fullmatch(r"\d{4}-\d{2}-\d{2}", week) is not None, "Invalid release week")
    local = root / "forecasts" / f"{week}.json"
    issue = validate_issue(read_json(local), week)
    _require(issue[0]["mode"] == "prospective", "Only prospective issues can be attested")
    tag = f"forecast-{week}"
    api_release = f"https://api.github.com/repos/{repository}/releases/tags/{tag}"
    release = json.loads(_fetch(api_release))
    html_url = f"https://github.com/{repository}/releases/tag/{tag}"
    _require(release.get("tag_name") == tag and release.get("draft") is False
             and release.get("prerelease") is False and release.get("immutable") is True
             and release.get("html_url") == html_url, "Expected a published and immutable GitHub release")
    _require(type(release.get("id")) is int, "Invalid release ID")
    asset_name = f"{week}.json"
    assets = [item for item in release.get("assets", []) if item.get("name") == asset_name]
    _require(len(assets) == 1, "Expected exactly one public issue asset")
    asset = assets[0]
    download_url = f"https://github.com/{repository}/releases/download/{tag}/{asset_name}"
    _require(asset.get("browser_download_url") == download_url and type(asset.get("id")) is int
             and asset.get("state") == "uploaded", "Invalid issue asset metadata")
    payload = _fetch(download_url, "application/octet-stream")
    actual = hashlib.sha256(payload).hexdigest()
    _require(asset.get("digest") == f"sha256:{actual}", "GitHub asset digest mismatch")
    _require(payload == local.read_bytes(), "Downloaded issue asset differs from local bytes")
    receipt = {
        "url": html_url,
        "published_at": release["published_at"],
        "immutable": True,
        "issue_sha256": digest(issue),
        "release_id": release["id"],
        "asset_id": asset["id"],
        "asset_url": download_url,
        "asset_sha256": actual,
    }
    validate_publication(receipt, issue, week)
    return receipt
