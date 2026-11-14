"""Prepare an editorial market issue from a current, bounded FRED snapshot."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from .cpi import _date
from .ledger import EASTERN, _market_lock, market_baseline, utcnow, validate_issue

# Only Friday closures or early closes affect the endpoint convention. Maintain
# this list against https://www.nyse.com/markets/hours-calendars.
FRIDAY_EXCEPTIONS = {
    date(2026, 4, 3), date(2026, 6, 19), date(2026, 7, 3),
    date(2026, 11, 27), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 3, 26), date(2027, 6, 18),
    date(2027, 11, 26), date(2027, 12, 24), date(2027, 12, 31),
}


def _abstain(week: str, reason: str) -> dict:
    return {"status": "abstained", "week_ending": week, "records": [], "reason": reason}


def _record(ending: date, anchor: date, direction: str, observed: datetime, baseline: dict) -> dict:
    week = ending.isoformat()
    down = direction == "lte"
    sign, verb = ("down", "lower") if down else ("up", "higher")
    deadline = datetime.combine(ending, time(16), EASTERN)
    void_at = datetime.combine(ending + timedelta(days=11), time(17), EASTERN)
    formula = "<= -0.02" if down else ">= +0.02"
    return {
        "id": f"sp500-{sign}-at-least-two-percent-{week}",
        "week_ending": week,
        "deadline_at": deadline.astimezone(timezone.utc).isoformat(),
        "void_not_before": void_at.astimezone(timezone.utc).isoformat(),
        "kind": "market_threshold",
        "question": f"Will the S&P 500 close at least 2% {verb} over the week ending {ending:%B %d}?",
        "probability": baseline["probability"],
        "baseline_probability": baseline["probability"],
        "method": "Laplace-smoothed Friday-to-Friday S&P 500 close frequency.",
        "rationale": (
            f"The prior FRED sample met the threshold in {baseline['hits']} of {baseline['samples']} "
            "eligible Friday pairs. This draft uses the resulting base rate pending editorial research."
        ),
        "risks": [
            "Both tails resolve on the same weekly return, making their outcomes dependent.",
            "The baseline omits weeks with a missing Friday endpoint and does not condition on current catalysts.",
        ],
        "resolution_rule": (
            f"YES if the FRED SP500 close on {ending} divided by the close on {anchor}, minus one, is {formula}. "
            f"Preserve the first eligible observation. Void if an endpoint is still missing at {void_at.isoformat()}."
        ),
        "evidence": [{
            "title": f"FRED SP500 baseline snapshot through {anchor}",
            "url": baseline["source_url"],
            "available_at": observed.isoformat(),
            "sha256": baseline["sha256"],
        }],
        "mode": "prospective",
        "market": {
            "series": "SP500",
            "start_date": anchor.isoformat(),
            "end_date": week,
            "direction": direction,
            "threshold_pct": -2.0 if down else 2.0,
        },
        "baseline": baseline,
        "issued_at": observed.isoformat(),
        "as_of_at": observed.isoformat(),
    }


def propose(week: str, *, now: datetime | None = None) -> dict:
    """Return a draft or documented abstention. Nothing is written or published."""
    ending = _date(week)
    if ending.weekday() != 4:
        return _abstain(week, "Week ending must fall on a Friday.")
    anchor = ending - timedelta(days=7)
    if anchor.year < 2026 or ending.year > 2027 or {anchor, ending} & FRIDAY_EXCEPTIONS:
        return _abstain(week, "The endpoint calendar is unsupported or has a holiday or early close.")
    anchor_close = datetime.combine(anchor, time(16), EASTERN)
    market_lock = _market_lock({"start_date": anchor.isoformat()})
    clock = now if now is not None else utcnow()
    if clock.tzinfo is None or clock.utcoffset() is None:
        return _abstain(week, "Clock must be timezone-aware.")
    if clock <= anchor_close:
        return _abstain(week, "Too early: wait for the anchor Friday close and FRED publication.")
    if clock >= market_lock:
        return _abstain(week, "Deadline passed: the Monday market lock has elapsed.")
    try:
        baselines = [
            market_baseline({"market": {"start_date": anchor.isoformat(), "direction": direction, "threshold_pct": value}})
            for direction, value in (("gte", 2.0), ("lte", -2.0))
        ]
    except (ValueError, OSError) as exc:
        return _abstain(week, f"Baseline unavailable: {exc}")
    snapshot = {(baseline["sha256"], baseline["source_url"]) for baseline in baselines}
    if len(snapshot) != 1:
        return _abstain(week, "Baseline source digest or URL differs between tails. Retry the draft.")
    observed = (now if now is not None else utcnow()).astimezone(timezone.utc)
    if observed >= market_lock:
        return _abstain(week, "Deadline passed while retrieving the source.")
    records = [
        _record(ending, anchor, direction, observed, baseline)
        for direction, baseline in zip(("gte", "lte"), baselines)
    ]
    validate_issue(records, week)
    sha256, url = snapshot.pop()
    return {"status": "proposed", "week_ending": week, "records": records,
            "baseline_source": {"sha256": sha256, "source_url": url}}
