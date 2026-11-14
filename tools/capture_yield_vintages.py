"""Capture tiny date-specific ALFRED CSVs at a bounded request rate."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
import time as timer
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from tsunami.cpi import _parse_series

EASTERN = ZoneInfo("America/New_York")
ALFRED = "https://alfred.stlouisfed.org/graph/alfredgraph.csv"


def capture(vintage: date, start: date, end: date) -> dict:
    url = f"{ALFRED}?" + urlencode({"id": "DGS2", "cosd": start, "coed": end, "vintage_date": vintage})
    request = Request(url, headers={"User-Agent": "Tsunami Research/0.1 (+https://github.com/Noptus/tsunami-research)"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=18) as response:
                data = response.read(32_769)
            break
        except OSError:
            if attempt == 2:
                raise
            timer.sleep(2 ** attempt)
    if len(data) > 32_768:
        raise ValueError("Unexpected ALFRED CSV size")
    text = data.decode("utf-8-sig")
    _parse_series(text, {"record": {"url": url}}, vintage)
    available = datetime.combine(vintage, time(23, 59, 59), EASTERN)
    captured = datetime.now(timezone.utc)
    if available > captured:
        raise ValueError(f"Cannot treat an unfinished vintage as final: {vintage}")
    timer.sleep(0.4)
    return {
        "url": url,
        "vintage_date": vintage.isoformat(),
        "available_at": available.isoformat(),
        "retrieved_at": captured.isoformat(timespec="seconds"),
        "sha256": sha256(data).hexdigest(),
        "csv": text,
    }


def main(root: Path) -> None:
    study = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    output = root / "vintages.json"
    snapshots = json.loads(output.read_text()).get("events", {}) if output.exists() else {}
    for event in study["events"]:
        event_day = date.fromisoformat(event["release_date"])
        day = event_day.isoformat()
        if day in snapshots:
            continue
        prior = event_day - timedelta(days=1)
        # The final release's short post-vintage is explicitly recorded. Earlier
        # releases use +7, with an observation window bounded at +5.
        after = min(event_day + timedelta(days=7), date(2026, 11, 13))
        snapshots[day] = {
            "before": capture(prior, event_day - timedelta(days=14), prior),
            "after": capture(after, event_day, min(event_day + timedelta(days=5), after)),
        }
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps({"events": snapshots}, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output)
        print(day, flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/cpi-study"))
