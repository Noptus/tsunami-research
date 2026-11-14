"""Capture CPI evidence and freeze a rates projection before the outcome exists."""

from __future__ import annotations

from datetime import datetime, time, timedelta
import gzip
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .cpi import ReplayError, _instant, _parse_bls, _parse_series, _predict
from .ledger import utcnow, write_new

EASTERN = ZoneInfo("America/New_York")
BLS = "https://www.bls.gov/news.release/cpi.nr0.htm"
ALFRED = "https://alfred.stlouisfed.org/graph/alfredgraph.csv"


def _get(url: str, limit: int) -> bytes:
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,text/csv,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urlopen(request, timeout=15) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ReplayError("Source response exceeds expected size")
    return raw


def _source(raw: bytes, url: str, available_at: datetime, name: str, root: Path, retrieved_at: datetime) -> dict:
    # Stage payloads with content-addressed filenames, and seal them with the manifest last.
    hashed = sha256(raw).hexdigest()
    path = root / f"{hashed}-{name}"
    payload = gzip.compress(raw, mtime=0) if name.endswith(".gz") else raw
    try:
        with path.open("xb") as stream:
            stream.write(payload)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise ReplayError(f"Existing capture data differs: {path}")
    return {"url": url, "path": path.name, "source_sha256": hashed, "sha256": sha256(payload).hexdigest(),
            "available_at": available_at.isoformat(), "retrieved_at": retrieved_at.isoformat()}


def capture_cpi(embargo_at: str, output: Path) -> dict:
    """Freeze a 15-minute capture. Official archive embargo must match the caller's expected date/time."""
    embargo = _instant(embargo_at)
    eastern = embargo.astimezone(EASTERN)
    day = eastern.date()
    cutoff = embargo + timedelta(minutes=15)
    if (output / "forecast.json").exists():
        raise ReplayError("This output already holds a sealed CPI capture")
    now = utcnow()
    if not embargo <= now <= cutoff:
        raise ReplayError("CPI capture may run only from the official release time through its 15-minute cutoff")
    vintage = day - timedelta(days=1)
    vintage_bound = datetime.combine(vintage, time(23, 59, 59), EASTERN)
    start = day - timedelta(days=500)
    market_url = f"{ALFRED}?" + urlencode({"id": "DGS2", "cosd": start, "coed": vintage, "vintage_date": vintage})
    bls = _get(BLS, 3_000_000)
    bls_retrieved = utcnow()
    cpi = _parse_bls(bls.decode("utf-8-sig"), embargo)
    rates = _get(market_url, 131_072)
    rates_retrieved = utcnow()
    market, summary = _parse_series(rates.decode("utf-8-sig"), {"record": {"url": market_url}}, vintage, before=day)
    if summary["observed"] < 253:
        raise ReplayError("Insufficient observations for the frozen 252-change baseline")
    forecast = _predict(market)
    finished = utcnow()
    if not embargo <= bls_retrieved <= rates_retrieved <= finished <= cutoff:
        raise ReplayError("CPI capture crossed the evidence cutoff")
    output.mkdir(parents=True, exist_ok=True)
    result = {
        "mode": "contemporaneous local capture",
        "release_date": day.isoformat(),
        "embargo_at": embargo.isoformat(),
        "cutoff_at": cutoff.isoformat(),
        "finished_at": finished.isoformat(),
        "target": "Next observed session's DGS2 exceeds release-session DGS2.",
        "publication": "Local file timestamps are not a public proof of issuance. Publish the capture digest before the outcome.",
        "predictions": {name: {**prediction, "mode": "contemporaneous local capture"}
                        for name, prediction in forecast.items()},
        "cpi": cpi,
        "training": summary,
        "sources": {
            "bls": _source(bls, BLS, embargo, "bls.html.gz", output, bls_retrieved),
            "training": _source(rates, market_url, vintage_bound, "alfred.csv", output, rates_retrieved),
        },
    }
    write_new(output / "forecast.json", result)
    return result
