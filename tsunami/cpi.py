"""Auditable CPI/Treasury archived replay; Python 3.11 standard library only.

The two probabilities use Treasury history only. CPI supplies a deterministic,
non-learned narrative, not an estimated text signal. Brier scores describe one
archived event, not a backtest or a claim of predictive skill.

Vintage evidence must occur in the CSV series header or URL (vintage_date,
vintage_dates, revision_date, realtime_start/end, or a DGS2_YYYYMMDD series ID).
A hash verifies fixture integrity, not the authenticity of the publisher or URL.
The scored target is the next observed session minus the release session, both
from the outcome vintage. The release-session change is descriptive only.
Eastern timezone validation requires the host's IANA timezone database.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import math
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo


class ReplayError(ValueError):
    """Invalid, unverifiable, or temporally ineligible replay evidence."""


def _date(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ReplayError(f"Expected YYYY-MM-DD date: {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ReplayError(f"Invalid date: {value!r}") from exc


def _instant(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ReplayError(f"Invalid ISO timestamp: {value!r}") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ReplayError(f"Timestamp must be timezone-aware: {value!r}")
    return result


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReplayError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _contained(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ReplayError("Source path must be a nonempty relative path")
    if ".." in Path(relative).parts:
        raise ReplayError("Source path traversal is forbidden")
    try:
        resolved = (root / relative).resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ReplayError(f"Source path is not contained: {relative!r}") from exc
    if not resolved.is_file():
        raise ReplayError(f"Source is not a file: {relative!r}")
    return resolved


def _source(root: Path, name: str, record: dict) -> dict:
    required = {"path", "url", "sha256", "available_at", "retrieved_at", "evidence_type"}
    if not isinstance(record, dict) or not required <= record.keys():
        raise ReplayError(f"Incomplete source record: {name}")
    if not all(isinstance(record[key], str) and record[key] for key in required):
        raise ReplayError(f"Source fields must be nonempty strings: {name}")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", record["sha256"]):
        raise ReplayError(f"Invalid SHA256: {name}")
    url = urlsplit(record["url"])
    if url.scheme not in {"https", "http"} or not url.netloc:
        raise ReplayError(f"Invalid source URL: {name}")
    available = _instant(record["available_at"])
    retrieved = _instant(record["retrieved_at"])
    if retrieved < available:
        raise ReplayError(f"Retrieval precedes availability: {name}")
    return {"name": name, "record": dict(record),
            "file": _contained(root, record["path"]),
            "available": available, "retrieved": retrieved}


def _verified_text(source: dict) -> str:
    # Called for the outcome only after _predict has returned.
    try:
        data = source["file"].read_bytes()
    except OSError as exc:
        raise ReplayError(f"Cannot read {source['name']}") from exc
    actual = hashlib.sha256(data).hexdigest()
    if actual != source["record"]["sha256"].lower():
        raise ReplayError(f"SHA256 mismatch: {source['name']}")
    source["verified_sha256"] = actual
    encoding = source["record"].get("encoding", "identity")
    if encoding == "gzip":
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError) as exc:
            raise ReplayError(f"Invalid gzip evidence: {source['name']}") from exc
    elif encoding != "identity":
        raise ReplayError(f"Unknown evidence encoding: {encoding}")
    original = source["record"].get("source_sha256")
    if original is not None and original != hashlib.sha256(data).hexdigest():
        raise ReplayError(f"Original source SHA256 mismatch: {source['name']}")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ReplayError(f"Invalid UTF-8: {source['name']}") from exc


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"p", "div", "br", "pre", "h1", "h2", "tr", "td"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "pre", "h1", "h2", "tr", "td"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


_MONTHS = {name: i for i, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"), 1)}
_MONTH_PATTERN = "|".join(_MONTHS)


def _bls_embargo(text: str, expected: datetime) -> str:
    # Match the actual release template, not an arbitrary date elsewhere in HTML.
    pattern = (r"(?:Transmission of material in this release is embargoed until|"
               r"(?:This release is )?embargoed until)\s+"
               r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*"
               r"(?P<ampm>[ap])\.?m\.?\s*\((?P<zone>ET|EST|EDT)\)\s*"
               r"(?:(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s*)?"
               rf"(?P<month>{_MONTH_PATTERN})\s+(?P<day>\d{{1,2}}),\s+(?P<year>\d{{4}})")
    matches = list(re.finditer(pattern, text, re.IGNORECASE))
    if len(matches) != 1:
        raise ReplayError("BLS must contain one recognizable release embargo template")
    match = matches[0]
    hour = int(match["hour"])
    if not 1 <= hour <= 12:
        raise ReplayError("Invalid BLS embargo hour")
    hour = hour % 12 + (12 if match["ampm"].lower() == "p" else 0)
    try:
        naive = datetime(int(match["year"]), _MONTHS[match["month"].title()],
                         int(match["day"]), hour, int(match["minute"]))
    except ValueError as exc:
        raise ReplayError("Invalid BLS embargo date/time") from exc
    eastern = ZoneInfo("America/New_York")
    local = naive.replace(tzinfo=eastern)
    # Reject ambiguous or nonexistent wall times, rather than guessing a fold.
    if local.utcoffset() != naive.replace(tzinfo=eastern, fold=1).utcoffset():
        raise ReplayError("Ambiguous or nonexistent BLS Eastern wall time")
    if match["weekday"] and local.strftime("%A").lower() != match["weekday"].lower():
        raise ReplayError("BLS embargo weekday mismatch")
    if match["zone"].upper() != "ET" and match["zone"].upper() != local.tzname():
        raise ReplayError("BLS timezone label conflicts with Eastern DST")
    if local != expected:
        raise ReplayError("BLS embargo does not match event manifest")
    return match[0]


def _monthly_change(sentence: str) -> float:
    # Anchor the action to the index subject, not a later comparison's verb.
    subject = re.match(
        r"(?:The\s+)?(?:Consumer Price Index for All Urban Consumers\s*\(CPI-U\)|"
        r"index for all items less food and energy|all items less food and energy index)\s+",
        sentence, re.I)
    if subject is None:
        raise ReplayError(f"Unrecognized BLS index subject: {sentence}")
    action = sentence[subject.end():]
    match = re.match(
        r"(rose|increased|advanced|climbed|fell|declined|decreased|dropped)\s+"
        r"(?:by\s+)?(\d+(?:\.\d+)?)\s+percent\b", action, re.I)
    stable = re.match(r"(?:was|were|remained|held)\s+(?:unchanged|stable|flat)\b|"
                      r"(?:showed|posted)\s+no\s+change\b|did not change\b", action, re.I)
    if match:
        value = float(match[2])
        if not math.isfinite(value):
            raise ReplayError("Non-finite BLS monthly change")
        return -value if match[1].lower() in {"fell", "declined", "decreased", "dropped"} else value
    if stable:
        return 0.0
    raise ReplayError(f"Unrecognized BLS monthly change: {sentence}")


def _parse_bls(text: str, embargo: datetime) -> dict:
    parser = _HTMLText()
    parser.feed(text)
    visible = re.sub(r"\s+", " ", "".join(parser.parts)).strip()
    embargo_evidence = _bls_embargo(visible, embargo)
    opening = re.search(r"The Consumer Price Index for All Urban Consumers\s*\(CPI-U\)", visible, re.I)
    if opening is None:
        raise ReplayError("Missing BLS release opening CPI-U headline")
    # Restrict to opening prose; tables and annual summaries are not substitutes.
    prose = visible[opening.start():opening.start() + 6000]
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", prose)
    headline = sentences[0]
    core_candidates = [s for s in sentences if re.match(
        r"(?:The\s+)?(?:index for all items less food and energy|all items less food and energy index)\b",
        s, re.I)]
    if not core_candidates:
        raise ReplayError("Missing core CPI monthly opening statement")
    core = core_candidates[0]
    # Ignore comparison clauses (e.g. ', after increasing ... in January').
    # Otherwise a stable current month could accidentally inherit an old rate.
    headline_clause = headline.split(",", 1)[0]
    core_clause = core.split(",", 1)[0]
    for clause in (headline_clause, core_clause):
        if re.search(r"\b(?:12[- ]month|over the (?:last|past) year|over the last 12 months)\b", clause, re.I):
            raise ReplayError("Annual CPI statement cannot substitute for monthly change")
        if not re.search(rf"\b(?:in (?:{_MONTH_PATTERN})|for the month|on a (?:seasonally adjusted|monthly) basis)\b",
                         clause, re.I):
            raise ReplayError("CPI change lacks explicit monthly context")
    if "seasonally adjusted" not in headline_clause.lower():
        raise ReplayError("Headline CPI must identify a seasonally adjusted monthly change")
    headline_change = _monthly_change(headline_clause)
    core_change = _monthly_change(core_clause)
    if headline_change > 0 and core_change > 0:
        thesis = "Both monthly CPI measures rose: an inflation-pressure/upward-yield narrative."
    elif headline_change <= 0 and core_change <= 0:
        thesis = "Neither monthly CPI measure rose: a cooling/flat-to-downward-yield narrative."
    else:
        thesis = "Headline and core monthly changes are mixed: no directional CPI narrative."
    return {"headline_monthly_percent": headline_change, "core_monthly_percent": core_change,
            "headline_evidence": headline, "core_evidence": core,
            "embargo_evidence": embargo_evidence,
            "thesis": thesis + " This deterministic context ignores expectations and does not alter either probability; it is not a learned or tested text signal."}


def _vintage(header: str, url: str, expected: date) -> list[str]:
    evidence: list[str] = []
    values: list[date] = []
    query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    for key in ("vintage_date", "vintage_dates", "revision_date", "realtime_start", "realtime_end"):
        for value in query.get(key, []):
            values.append(_date(value))
            evidence.append(f"URL {key}={value}")
    if ("realtime_start" in query) != ("realtime_end" in query):
        raise ReplayError("ALFRED real-time interval must have both fixed endpoints")
    for label, value in [("header", header)] + [
        (f"URL {key}", v) for key in ("id", "series_id", "line_id") for v in query.get(key, [])
    ]:
        for match in re.finditer(r"\bDGS2_(\d{4})(\d{2})(\d{2})\b", value):
            values.append(_date("-".join(match.groups())))
            evidence.append(f"{label} {match[0]}")
    if not values or any(value != expected for value in values):
        raise ReplayError(f"Missing or conflicting fixed vintage; expected {expected}")
    return evidence


def _parse_series(text: str, source: dict, expected_vintage: date,
                  before: date | None = None) -> tuple[dict[date, Decimal], dict]:
    reader = csv.reader(io.StringIO(text), strict=True)
    try:
        raw_header = next(reader)
        header = [cell.strip() for cell in raw_header]
        if (len(header) != 2 or header[0] not in {"date", "observation_date"}
                or not re.fullmatch(r"DGS2(?:_\d{8})?", header[1])):
            raise ReplayError("Expected date/observation_date,DGS2[_YYYYMMDD] CSV header")
        vintage_evidence = _vintage(header[1], source["record"]["url"], expected_vintage)
        observed: dict[date, Decimal] = {}
        seen: set[date] = set()
        missing = 0
        for row in reader:
            if len(row) != 2:
                raise ReplayError(f"Malformed CSV row {reader.line_num}")
            day = _date(row[0].strip())
            if day in seen:
                raise ReplayError(f"Duplicate observation date: {day}")
            seen.add(day)
            if before is not None and day >= before:
                raise ReplayError(f"Training lookahead date: {day}")
            if day > expected_vintage:
                raise ReplayError(f"Observation date {day} is after its archival vintage")
            raw = row[1].strip()
            if raw in {"", "."}:
                missing += 1
                continue
            if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", raw):
                raise ReplayError(f"Malformed DGS2 value on {day}: {raw!r}")
            try:
                value = Decimal(raw)
                finite = value.is_finite() and math.isfinite(float(value))
            except (InvalidOperation, OverflowError) as exc:
                raise ReplayError(f"Invalid DGS2 value on {day}") from exc
            if not finite:
                raise ReplayError(f"Non-finite DGS2 value on {day}")
            observed[day] = value
    except (StopIteration, csv.Error) as exc:
        raise ReplayError("Empty or malformed CSV") from exc
    if not observed:
        raise ReplayError("CSV contains no observed DGS2 values")
    observed = dict(sorted(observed.items()))
    return observed, {"rows": len(seen), "observed": len(observed), "missing": missing,
                      "first_observed_date": min(observed).isoformat(),
                      "last_observed_date": max(observed).isoformat(),
                      "vintage_date": expected_vintage.isoformat(),
                      "vintage_evidence": vintage_evidence}


def _sign(value: Decimal) -> int:
    return (value > 0) - (value < 0)


def _predict(training: dict[date, Decimal]) -> dict:
    values = list(training.values())
    changes = [right - left for left, right in zip(values, values[1:])]
    if not changes:
        raise ReplayError("At least two observed training sessions are required")
    trailing = changes[-252:]
    upward = sum(change > 0 for change in trailing)
    baseline = (upward + 1) / (len(trailing) + 2)
    latest_sign = _sign(changes[-1])
    # Each target is paired only with its preceding change; never itself.
    pairs = list(zip(changes, changes[1:]))[-252:]
    selected = [target for lag, target in pairs if _sign(lag) == latest_sign]
    selected_up = sum(change > 0 for change in selected)
    enough = len(selected) >= 20
    challenger = (selected_up + 1) / (len(selected) + 2) if enough else baseline
    return {
        "baseline": {"mode": "archived replay", "probability_up": baseline, "samples": len(trailing),
                     "up": upward, "non_up": len(trailing) - upward,
                     "description": "Laplace (up+1)/(n+2) over the last at most 252 adjacent observed-session changes; flat is non-up."},
        "challenger": {"mode": "archived replay", "context_source": "bls",
                       "probability_up": challenger, "samples": len(selected),
                       "up": selected_up, "non_up": len(selected) - selected_up,
                       "candidate_pairs": len(pairs), "latest_lag_sign": latest_sign,
                       "minimum_samples": 20, "fallback_to_baseline": not enough,
                       "description": "Pre-specified momentum: last at most 252 lag/next-change pairs, conditioned on the latest observed change sign; Laplace smoothing, baseline fallback below 20 matches. Treasury values only; no fitting or model selection."}}


def _observed(day: date, value: Decimal, reference_day: date, reference: Decimal,
              predictions: dict, score: bool) -> dict:
    change = value - reference
    up = int(change > 0)
    return {"date": day.isoformat(), "DGS2": float(value),
            "reference_date": reference_day.isoformat(), "reference_DGS2": float(reference),
            "change_percentage_points": float(change), "change_basis_points": float(change * 100),
            "up": up,
            "brier": {name: (model["probability_up"] - up) ** 2
                      for name, model in predictions.items()} if score else None}


def replay(directory: Path) -> dict:
    """Validate a local immutable fixture and return a JSON-serializable audit.

    Outcome bytes (including their SHA256) are read only after predictions exist.
    Retrieval may be later than cutoff: that is explicitly an archived replay,
    not proof of a contemporaneously captured forecast. No network or writes.
    Raises ReplayError on invalid evidence (filesystem errors may also propagate).
    """
    root = Path(directory).resolve(strict=True)
    manifest_path = _contained(root, "event.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"),
                              object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReplayError("Invalid event.json") from exc
    if not isinstance(manifest, dict):
        raise ReplayError("Manifest must be an object")
    required = {"release_date", "embargo_at", "cutoff_at", "outcome_event_date", "outcome_next_date", "sources"}
    if not required <= manifest.keys():
        raise ReplayError("Incomplete event manifest")
    release = _date(manifest["release_date"])
    embargo = _instant(manifest["embargo_at"])
    cutoff = _instant(manifest["cutoff_at"])
    event_day = _date(manifest["outcome_event_date"])
    next_day = _date(manifest["outcome_next_date"])
    eastern = ZoneInfo("America/New_York")
    if embargo.astimezone(eastern).date() != release:
        raise ReplayError("Release date differs from Eastern embargo date")
    if event_day != release or next_day <= event_day:
        raise ReplayError("Outcome event must be release date and next date must be later")
    if cutoff < embargo or cutoff.astimezone(eastern).date() != release:
        raise ReplayError("CPI replay cutoff must be on release day at or after embargo")
    records = manifest["sources"]
    if not isinstance(records, dict) or set(records) != {"bls", "training", "outcome"}:
        raise ReplayError("Sources must be exactly bls, training, outcome")
    sources = {name: _source(root, name, record) for name, record in records.items()}
    if len({source["file"] for source in sources.values()}) != 3:
        raise ReplayError("Sources must have distinct contained paths")
    for name in ("bls", "training"):
        if sources[name]["available"] > cutoff:
            raise ReplayError(f"Forecast input available after cutoff: {name}")
    if sources["bls"]["available"] < embargo:
        raise ReplayError("BLS original release cannot be available before embargo")
    if sources["outcome"]["available"] <= cutoff:
        raise ReplayError("Outcome must become available strictly after cutoff")

    cpi = _parse_bls(_verified_text(sources["bls"]), embargo)
    cpi["source"] = "bls"
    training, training_info = _parse_series(
        _verified_text(sources["training"]), sources["training"],
        release - timedelta(days=1), before=release)
    predictions = _predict(training)

    # Deliberate information boundary: do not move outcome I/O above this line.
    outcome_vintage = sources["outcome"]["available"].astimezone(eastern).date()
    if outcome_vintage < next_day:
        raise ReplayError("Outcome availability date precedes requested next observation")
    outcome, outcome_info = _parse_series(
        _verified_text(sources["outcome"]), sources["outcome"], outcome_vintage)
    if any(day > outcome_vintage for day in outcome):
        raise ReplayError("Outcome contains observations after its archival vintage")
    if event_day not in outcome or next_day not in outcome:
        raise ReplayError("Outcome event/next observation missing")
    if any(event_day < day < next_day for day in outcome):
        raise ReplayError("outcome_next_date is not the next observed session")
    anchor_day = max(training)
    if any(anchor_day < day < event_day for day in outcome):
        raise ReplayError("Training anchor misses an observed pre-event session")
    event = _observed(event_day, outcome[event_day], anchor_day, training[anchor_day], predictions, False)
    following = _observed(next_day, outcome[next_day], event_day, outcome[event_day], predictions, True)
    provenance = {}
    for name, source in sources.items():
        provenance[name] = {**source["record"], "verified_sha256": source["verified_sha256"],
                            "contained_path_verified": True,
                            "available_by_cutoff": source["available"] <= cutoff,
                            "retrieved_by_cutoff": source["retrieved"] <= cutoff,
                            "role": "evaluation only" if name == "outcome" else "forecast input"}
    return {
        "mode": "archived replay", "release_date": release.isoformat(),
        "embargo_at": manifest["embargo_at"], "cutoff_at": manifest["cutoff_at"],
        "eligibility": {"archived_replay": True,
                        "forecast_inputs_retrieved_by_cutoff": all(sources[n]["retrieved"] <= cutoff for n in ("bls", "training")),
                        "prospective_forecast": False,
                        "explanation": "Declared availability gates replay eligibility; later retrieval is allowed and disclosed. This execution is never a prospective forecast. URL, evidence_type and availability are manifest assertions, not independently authenticated."},
        "title": f"CPI release replay · {release.isoformat()}",
        "target": "Next observed session’s DGS2 minus release-session DGS2; up means strictly positive. Both levels come from the frozen evaluation vintage. Release-session change versus the pre-event anchor is descriptive.",
        "baseline_brier": following["brier"]["baseline"],
        "predictions": predictions, "cpi": cpi,
        "training": training_info, "outcome_series": outcome_info,
        "event": event, "next": following, "sources": provenance,
        "limitations": ["One event's Brier scores are not evidence of model skill.",
                        "CPI levels alone do not measure surprise relative to expectations.",
                        "Observed sessions skip missing values; no exchange-calendar inference or imputation.",
                        "Vintage dates are checked against URL/header declarations, not authenticated by a remote archive. The outcome vintage is evaluated at its Eastern calendar date.",
                        "Event change uses the frozen training vintage anchor; subsequent revisions may differ."]}
