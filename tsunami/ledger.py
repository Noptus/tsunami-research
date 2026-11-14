"""Validate weekly questions, compute outcomes, and audit append-only publications."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .cpi import _date, _instant, _unique_object

EASTERN = ZoneInfo("America/New_York")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
GITHUB_RELEASE = re.compile(r"https://github\.com/[^/]+/[^/]+/releases/tag/forecast-\d{4}-\d{2}-\d{2}")
CHECKSUM = re.compile(r"[a-f0-9]{64}")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()


def digest(value: object) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def write_new(path: Path, value: object) -> None:
    """An identical retry is safe. A conflicting write fails without updating the ledger."""
    content = json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / f".{path.name}.lock").open("a+b") as lock:
        import fcntl

        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            if path.read_bytes() != content:
                raise ValueError(f"Ledger record is already sealed: {path}")
            return
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_bytes(content)
        temporary.replace(path)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _probability(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def _host(url: object) -> str:
    _require(isinstance(url, str), "Source URL must be a string")
    _require(url.isascii() and "\\" not in url and not any(ord(c) <= 32 or ord(c) == 127 for c in url),
             "Unsafe source URL")
    parts = urlsplit(url)
    _require(parts.scheme == "https" and bool(parts.hostname) and parts.username is None and parts.password is None
             and parts.port in (80, 443, None), f"Invalid HTTPS evidence URL: {url}")
    return parts.hostname


def _market_lock(market: dict) -> datetime:
    anchor = _date(market["start_date"])
    return datetime.combine(anchor + timedelta(days=3), time(9, 30), EASTERN)


def validate_issue(issue: object, week: str) -> list[dict]:
    week_date = _date(week)
    _require(week_date.weekday() == 4, "Week ending must fall on Friday")
    _require(isinstance(issue, list) and bool(issue), "Issue must contain forecasts")
    seen = set()
    issued = None
    for forecast in issue:
        _require(isinstance(forecast, dict), "Invalid forecast")
        forecast_id = forecast.get("id")
        _require(isinstance(forecast_id, str) and len(forecast_id) <= 100 and SLUG.fullmatch(forecast_id) is not None,
                 "Forecast ID must be a slug")
        _require(forecast_id not in seen, f"Duplicate forecast: {forecast_id}")
        seen.add(forecast_id)
        _require(forecast.get("week_ending") == week, f"Wrong week for {forecast_id}")
        as_of, issue_time, deadline = (_instant(forecast.get(key)) for key in ("as_of_at", "issued_at", "deadline_at"))
        _require(as_of <= issue_time < deadline, f"Invalid issue timing: {forecast_id}")
        _require(issued is None or issue_time == issued, "One issue must share an issuance time")
        issued = issue_time
        _require(deadline.astimezone(EASTERN).date() == week_date, "Deadline must fall on local week ending")
        _require(issue_time.astimezone(EASTERN).date() >= week_date - timedelta(days=7),
                 "Weekly issue was locked more than one week before its deadline")
        _require(forecast.get("mode") in ("prospective", "archived_replay"), "Invalid forecast mode")
        for key in ("question", "rationale", "resolution_rule"):
            _require(_text(forecast.get(key)), f"Missing forecast {key}: {forecast_id}")
        barrier_times = {key: _instant(forecast[key]) for key in (
            "resolve_no_not_before", "void_not_before"
        ) if key in forecast}
        _require(all(value >= deadline for value in barrier_times.values()), "Resolution barrier precedes deadline")
        if len(barrier_times) == 2:
            _require(barrier_times["void_not_before"] >= barrier_times["resolve_no_not_before"],
                     "Void barrier precedes negative-resolution barrier")
        for key in ("probability", "baseline_probability"):
            _require(key in forecast and (_probability(forecast[key]) or key == "baseline_probability" and forecast[key] is None),
                     f"Invalid {key}: {forecast_id}")
        risks = forecast.get("risks")
        _require(isinstance(risks, list) and bool(risks) and all(_text(risk) for risk in risks), "Missing risks")
        evidence = forecast.get("evidence")
        _require(isinstance(evidence, list) and bool(evidence), "Missing forecast evidence")
        for source in evidence:
            _require(isinstance(source, dict) and _text(source.get("title")), "Invalid evidence title")
            _host(source.get("url"))
            _require(_instant(source.get("available_at")) <= as_of, "Lookahead: source is newer than as-of time")
        if forecast.get("kind") == "market_threshold":
            market = forecast.get("market")
            _require(isinstance(market, dict) and market.get("series") == "SP500", "Unsupported market series")
            start, end = _date(market.get("start_date")), _date(market.get("end_date"))
            _require(start < end <= week_date and start <= as_of.astimezone(EASTERN).date(),
                     "Invalid market observation range")
            _require(start.weekday() == 4 and end == start + timedelta(days=7),
                     "Market forecast requires Friday closes exactly one week apart")
            _require(datetime.combine(start, time(16), EASTERN) <= as_of and issue_time < _market_lock(market),
                     "Market forecast must lock after the anchor close and before Monday at 09:30 ET")
            _require(market.get("direction") in ("gte", "lte"), "Invalid market threshold direction")
            threshold = market.get("threshold_pct")
            _require(type(threshold) in (int, float) and math.isfinite(threshold), "Invalid market threshold")
        elif forecast.get("kind") == "binary_event":
            hosts = forecast.get("resolution_hosts")
            _require(isinstance(hosts, list) and bool(hosts), "Named event must list resolution authorities")
            _require(all(isinstance(host, str) and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host) for host in hosts),
                     "Invalid named event authority")
        else:
            raise ValueError(f"Unknown forecast type: {forecast_id}")
    _require(len({f["mode"] for f in issue}) == 1, "An issue cannot mix archived and prospective records")
    return issue


def validate_publication(publication: dict, issue: list[dict], week: str) -> None:
    url = publication.get("url")
    _require(isinstance(url, str) and GITHUB_RELEASE.fullmatch(url) is not None and url.endswith(f"forecast-{week}"),
             "Publication must reference the dated GitHub release")
    _require(publication.get("issue_sha256") == digest(issue), "Published issue digest does not match")
    _require(publication.get("immutable") is True and type(publication.get("release_id")) is int
             and type(publication.get("asset_id")) is int, "Publication must reference an immutable GitHub release and its issue asset")
    published_at = _instant(publication.get("published_at"))
    _require(all(_instant(record["issued_at"]) <= published_at < _instant(record["deadline_at"]) for record in issue),
             "Public release did not precede all issue deadlines")
    _require(all(
        record["kind"] != "market_threshold" or published_at < _market_lock(record["market"])
        for record in issue
    ), "Market question was published at or after Monday 09:30 ET")


def _fred_url(series: str, start: date, end: date) -> str:
    return "https://fred.stlouisfed.org/graph/fredgraph.csv?" + urlencode(
        {"id": series, "cosd": start.isoformat(), "coed": end.isoformat()}
    )


def fetch_sp500(start: date, end: date) -> tuple[dict[date, Decimal], str, str]:
    """Fetch transient values. Repository artifacts record digests and hits, never raw SP500 levels."""
    url = _fred_url("SP500", start, end)
    request = Request(url, headers={"User-Agent": "Tsunami Research/0.1 (+https://github.com/Noptus/tsunami-research)"})
    with urlopen(request, timeout=25) as response:
        raw = response.read(2_000_001)
    _require(len(raw) <= 2_000_000, "FRED response exceeds expected CSV size")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), strict=True)
    _require(reader.fieldnames in (["observation_date", "SP500"], ["DATE", "SP500"]), "Unexpected SP500 CSV schema")
    date_field = reader.fieldnames[0]
    values = {}
    seen = set()
    for row in reader:
        day = _date(row[date_field])
        _require(day not in seen and start <= day <= end, "Unexpected or repeated SP500 date")
        seen.add(day)
        raw_price = row["SP500"]
        if raw_price in ("", "."):
            continue
        try:
            price = Decimal(raw_price)
        except (InvalidOperation, TypeError) as exc:
            raise ValueError(f"Invalid SP500 observation for {day}") from exc
        _require(price.is_finite() and price > 0, f"Invalid SP500 observation for {day}")
        values[day] = price
    return values, url, hashlib.sha256(raw).hexdigest()


def _hits(value: Decimal, market: dict) -> bool:
    threshold = Decimal(str(market["threshold_pct"]))
    return value >= threshold if market["direction"] == "gte" else value <= threshold


def market_baseline(forecast: dict) -> dict:
    """Count nonoverlapping Friday close-to-close returns strictly before the target window."""
    market = forecast["market"]
    anchor = _date(market["start_date"])
    _require(anchor.weekday() == 4, "Friday-only baseline requires a Friday anchor")
    # FRED exposes a trailing ten-year SP500 history. Keep the fixed request
    # window bounded by the anchor; the first observation can be later.
    start = anchor - timedelta(days=3650)
    values, url, raw_digest = fetch_sp500(start, anchor)
    _require(anchor in values, "FRED has no published close for the anchor Friday")
    pairs = [
        (day, day + timedelta(days=7))
        for day in sorted(values)
        if day.weekday() == 4 and day + timedelta(days=7) in values and day + timedelta(days=7) <= anchor
    ]
    _require(len(pairs) >= 450, "Insufficient Friday close pairs")
    count = sum(_hits((values[right] / values[left] - 1) * 100, market) for left, right in pairs)
    return {
        "probability": (count + 1) / (len(pairs) + 2),
        "hits": count,
        "samples": len(pairs),
        "range_start": start.isoformat(),
        "range_end": anchor.isoformat(),
        "method": "Laplace smoothing of matching Friday-to-Friday S&P 500 close returns over a trailing ten-year request window. Skip missing endpoints. The anchor is eligible history. Zero market levels stored.",
        "source_url": url,
        "sha256": raw_digest,
    }


def market_observation(forecast: dict, *, now: datetime | None = None) -> dict | None:
    observed_at = now or utcnow()
    if observed_at < _instant(forecast["deadline_at"]) + timedelta(hours=8):
        return None
    market = forecast["market"]
    start, end = _date(market["start_date"]), _date(market["end_date"])
    values, url, raw_digest = fetch_sp500(start, end)
    observation = {
        "forecast_id": forecast["id"],
        "forecast_sha256": digest(forecast),
        "resolved_at": observed_at.isoformat(),
        "source_urls": [url],
        "source_sha256": raw_digest,
    }
    if start not in values or end not in values:
        void_at = _instant(forecast["void_not_before"]) if "void_not_before" in forecast else (
            datetime.combine(end + timedelta(days=11), time(17), EASTERN)
        )
        if observed_at < void_at:
            return None
        missing = ", ".join(day.isoformat() for day in (start, end) if day not in values)
        return {**observation, "status": "void",
                "resolution_note": f"FRED did not provide closing observations for {missing} at the predeclared void cutoff."}
    outcome = int(_hits((values[end] / values[start] - 1) * 100, market))
    return {
        **observation,
        "status": "resolved",
        "outcome": outcome,
        "resolution_note": (
            f"FRED SP500 closes for {start} and {end}. The percentage change "
            f"{'met' if outcome else 'did not meet'} the predeclared threshold. "
            "Raw index levels are not republished."
        ),
    }


def score(forecast: dict, observation: dict) -> dict:
    _require(observation.get("forecast_id") == forecast["id"], "Resolution has wrong record ID")
    _require(observation.get("forecast_sha256") == digest(forecast), "Resolution has wrong forecast digest")
    resolved_at = _instant(observation.get("resolved_at"))
    _require(_instant(forecast["deadline_at"]) <= resolved_at <= utcnow(), "Resolution timestamp is out of bounds")
    _require(_text(observation.get("resolution_note")), "Resolution needs a rationale")
    urls = observation.get("source_urls")
    _require(isinstance(urls, list) and bool(urls), "Resolution requires sources")
    hosts = {_host(url) for url in urls}
    if forecast["kind"] == "binary_event":
        authorities = set(forecast["resolution_hosts"])
        _require(hosts <= authorities, "Resolution source is outside predeclared authority")
        if observation.get("outcome") == 0 and "resolve_no_not_before" in forecast:
            _require(resolved_at >= _instant(forecast["resolve_no_not_before"]), "Negative-resolution review is too early")
            if {"www.sec.gov", "investor.nvidia.com"} <= authorities:
                _require("www.sec.gov" in hosts and ("investor.nvidia.com" in hosts or "nvidianews.nvidia.com" in hosts),
                         "Negative resolution requires SEC and issuer archive reviews")
    else:
        market = forecast["market"]
        market_url = _fred_url(market["series"], _date(market["start_date"]), _date(market["end_date"]))
        _require(urls == [market_url], "Market observation requires the exact declared FRED series and date range")
        sha = observation.get("source_sha256")
        _require(isinstance(sha, str) and CHECKSUM.fullmatch(sha) is not None, "Missing market source checksum")
    status = observation.get("status")
    _require(status in ("resolved", "void"), "Invalid resolution status")
    output = dict(observation)
    if status == "void":
        _require("outcome" not in observation, "Void observations cannot carry outcomes")
        if "void_not_before" in forecast:
            _require(resolved_at >= _instant(forecast["void_not_before"]), "Void review is too early")
        elif forecast["kind"] == "market_threshold":
            end = _date(forecast["market"]["end_date"])
            _require(resolved_at >= datetime.combine(end + timedelta(days=11), time(17), EASTERN),
                     "Market void review is too early")
        return {**output, "brier": None, "baseline_brier": None}
    outcome = observation.get("outcome")
    _require(type(outcome) is int and outcome in (0, 1), "Outcome must be integer 0 or 1")
    baseline = forecast["baseline_probability"]
    return {**output, "brier": (forecast["probability"] - outcome) ** 2,
            "baseline_brier": (baseline - outcome) ** 2 if baseline is not None else None}


def load_ledger(root: Path, *, allow_unpublished: bool = False) -> tuple[list[dict], dict[str, dict]]:
    records, results, known = [], {}, set()
    issue_paths = {path.stem: path for path in (root / "forecasts").glob("*.json")}
    publication_paths = {path.stem: path for path in (root / "publications").glob("*.json")}
    _require(publication_paths.keys() <= issue_paths.keys(), "Orphan publication record")
    for week, path in sorted(issue_paths.items()):
        issue = validate_issue(read_json(path), week)
        ids = {record["id"] for record in issue}
        _require(not known & ids, "Forecast ID is duplicated across issues")
        known.update(ids)
        prospective = issue[0]["mode"] == "prospective"
        publication = None
        if prospective:
            if week in publication_paths:
                publication = read_json(publication_paths[week])
                validate_publication(publication, issue, week)
            elif not allow_unpublished:
                continue
        else:
            _require(week not in publication_paths, "Archived issues cannot carry prospective publication receipts")
        for forecast in issue:
            result_path = root / "results" / f"{forecast['id']}.json"
            if result_path.exists():
                _require(not prospective or publication is not None, "Cannot score an unverified prospective issue")
                results[forecast["id"]] = score(forecast, read_json(result_path))
            records.append({**forecast, "publication": publication} if publication else forecast)
    result_ids = {path.stem for path in (root / "results").glob("*.json")}
    _require(result_ids <= known, "Orphan resolution record")
    return records, results
