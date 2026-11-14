"""Capture official BLS opening text and one frozen ALFRED yield vintage."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from gzip import compress
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import re
import sys
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from tsunami.cpi import _HTMLText, _MONTHS, _parse_bls

BLS = "https://www.bls.gov"
ARCHIVE = BLS + "/bls/news-release/cpi.htm"
FIRST_RELEASE = date(2020, 1, 1)
LAST_RELEASE = date(2026, 11, 10)
VINTAGE = "2026-11-13"
ALFRED = ("https://alfred.stlouisfed.org/graph/alfredgraph.csv"
          f"?id=DGS2&cosd=2019-10-01&coed={VINTAGE}&vintage_date={VINTAGE}")
EASTERN = ZoneInfo("America/New_York")


def fetch(url: str) -> tuple[bytes, str]:
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urlopen(request, timeout=20) as response:
            return response.read(), datetime.now(timezone.utc).isoformat(timespec="seconds")
    except OSError:
        with urlopen(request, timeout=35) as response:
            return response.read(), datetime.now(timezone.utc).isoformat(timespec="seconds")


def decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("windows-1252", errors="replace")


def event_day(link: str) -> date:
    return datetime.strptime(link.rsplit("_", 1)[1][:8], "%m%d%Y").date()


def capture(link: str) -> tuple[dict, bytes]:
    day = event_day(link)
    url = BLS + link
    raw, retrieved_at = fetch(url)
    parser = _HTMLText()
    parser.feed(decode(raw))
    visible = " ".join("".join(parser.parts).split())
    match = re.search(
        r"(?:Transmission of material in this release is embargoed until|This release is embargoed until)\s+"
        r"(\d{1,2}):(\d{2})\s*([ap])\.?m\.?\s*\((?:ET|EST|EDT)\)\s*"
        r"(?:[A-Za-z]+,?\s+)?([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})",
        visible,
        re.I,
    )
    if match is None:
        raise ValueError("Missing embargo timestamp")
    hour, minute, period, month, dom, year = match.groups()
    embargo = datetime(int(year), _MONTHS[month.title()], int(dom), int(hour) % 12 + (12 if period.lower() == "p" else 0),
                       int(minute), tzinfo=EASTERN)
    if embargo.date() != day:
        raise ValueError("Archive filename and embargo date disagree")
    try:
        parsed = _parse_bls(decode(raw), embargo)
    except ValueError as error:
        if "over the 2 months from September 2025 to November 2025" in visible:
            raise ValueError("Shutdown release reports a two-month change, not a comparable monthly release") from error
        raise
    excerpt = "\n".join(
        f"<p>{escape(parsed[key])}</p>" for key in ("embargo_evidence", "headline_evidence", "core_evidence")
    )
    payload = compress(excerpt.encode(), mtime=0)
    return {
        "release_date": day.isoformat(),
        "embargo_at": embargo.isoformat(),
        "source_url": url,
        "path": f"{day.isoformat()}.html.gz",
        "sha256": sha256(payload).hexdigest(),
        "source_sha256": sha256(raw).hexdigest(),
        "excerpt_sha256": sha256(excerpt.encode()).hexdigest(),
        "retrieved_at": retrieved_at,
    }, payload


def main(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    raw_index, index_capture = fetch(ARCHIVE)
    links = sorted(
        {link for link in re.findall(r"/news\.release/archives/cpi_[0-9]{8}\.htm", decode(raw_index), re.I)
         if FIRST_RELEASE <= event_day(link) <= LAST_RELEASE},
        key=event_day,
    )
    final_release = f"/news.release/archives/cpi_{LAST_RELEASE.strftime('%m%d%Y')}.htm"
    if final_release not in links:
        links.append(final_release)
    events, failures = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [(link, pool.submit(capture, link)) for link in links]
        for link, task in jobs:
            try:
                event, payload = task.result()
            except (OSError, ValueError) as error:
                failures.append({"release_date": event_day(link).isoformat(), "source_url": BLS + link,
                                 "reason": str(error)})
                continue
            events.append(event)
            (output / event["path"]).write_bytes(payload)
    raw_yields, yield_capture = fetch(ALFRED)
    (output / "alfred-dgs2.csv").write_bytes(raw_yields)
    manifest = {
        "archive_index": {"url": ARCHIVE, "retrieved_at": index_capture, "sha256": sha256(raw_index).hexdigest()},
        "capture_failures": failures,
        "events": events,
        "yields": {"path": "alfred-dgs2.csv", "sha256": sha256(raw_yields).hexdigest(), "url": ALFRED,
                   "vintage_date": VINTAGE, "retrieved_at": yield_capture},
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Captured {len(events)} events with {len(failures)} excluded.")
    for failure in failures:
        print(failure)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/cpi-study"))
