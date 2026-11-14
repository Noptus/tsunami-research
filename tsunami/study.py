"""Chronological ablation: do CPI headline/core changes add information over a
market-only Treasury lag sign for the next observed session's direction?

Python 3.11 standard library only. No network, shell, or writes.

The study is an archival, honest ablation, not a tradeable signal or a claim of
skill. Two matched model families are compared under one walk-forward protocol:

  * baseline  - an event-type Laplace constant keyed on the sign of the last
                observed pre-release Treasury change (market-only information);
  * challenger - an L2-regularized logistic model on that same lag sign plus the
                released headline and core monthly CPI changes (the "advanced"
                features), standardized with training statistics only.

Both see identical expanding-window cutoffs and the same regularization (the
intercept is never penalized). Constants are predeclared below, never tuned on
future events. Each event is predicted strictly before its label is scored, and
no event is trained on a label that archival rates could not yet have revealed.

The paired Brier delta (challenger minus baseline) summarizes whether the CPI
text features helped, on the matched set of scored events only. A positive delta
means the challenger was worse. One small study is descriptive, not inferential.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import random
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .cpi import (
    ReplayError,
    _contained,
    _date,
    _instant,
    _parse_bls,
    _parse_series,
    _sign,
    _unique_object,
)

EASTERN = ZoneInfo("America/New_York")

# --- Predeclared constants (never selected on future data) -------------------
MIN_TRAINING_EVENTS = 24       # minimum labeled prior events before a prediction
L2_LAMBDA = 1.0                # ridge penalty on standardized features (not intercept)
LEARNING_RATE = 0.2            # full-batch gradient-descent step
MAX_ITERATIONS = 2000          # deterministic optimizer budget
CONVERGENCE = 1e-9             # mean-squared-gradient stopping threshold
LAPLACE_ALPHA = 1.0            # additive smoothing for the event-type base rate
LABEL_GAP_CALENDAR_DAYS = 5    # conservative calendar bound for label availability

FEATURES = ("lag_sign", "headline_monthly_percent", "core_monthly_percent")


def _sigmoid(z: float) -> float:
    """Overflow-safe logistic function."""
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    exp_z = math.exp(z)
    return exp_z / (1.0 + exp_z)


def _check_bls_url(url: str, release: date, name: str) -> None:
    """The BLS archive link must be an HTTPS bls.gov URL indexing the exact date.

    Real archive paths look like /news.release/archives/cpi_MMDDYYYY.htm.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host != "www.bls.gov" or parts.username or parts.password:
        raise ReplayError(f"BLS source URL must be an HTTPS www.bls.gov link: {name}")
    if parts.path.lower() != f"/news.release/archives/cpi_{release:%m%d%Y}.htm":
        raise ReplayError(f"BLS source URL does not index the release date: {name}")


def _gzip_text(data: bytes, name: str, source_sha256: str) -> str:
    """Decompress a gzip evidence excerpt and anchor it to the original bytes."""
    try:
        original = gzip.decompress(data)
    except (OSError, EOFError) as exc:
        raise ReplayError(f"Invalid gzip evidence: {name}") from exc
    if hashlib.sha256(original).hexdigest() != source_sha256.lower():
        raise ReplayError(f"Original source SHA256 mismatch: {name}")
    try:
        return original.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ReplayError(f"Invalid UTF-8 evidence: {name}") from exc


def _read_excerpt(root: Path, record: dict, name: str) -> tuple[date, object, dict]:
    """Validate an event record, verify its gzip artifact, re-parse the excerpt.

    Returns the release date, the embargo instant, and parsed CPI features.
    """
    required = {"release_date", "embargo_at", "source_url", "path",
                "sha256", "source_sha256", "retrieved_at"}
    if not isinstance(record, dict) or not required <= record.keys():
        raise ReplayError(f"Incomplete event record: {name}")
    for key in ("source_url", "path"):
        if not isinstance(record[key], str) or not record[key]:
            raise ReplayError(f"Invalid {key}: {name}")
    for key in ("sha256", "source_sha256"):
        value = record[key]
        if not isinstance(value, str) or len(value) != 64 or not set(value.lower()) <= set("0123456789abcdef"):
            raise ReplayError(f"Invalid {key}: {name}")
    release = _date(record["release_date"])
    embargo = _instant(record["embargo_at"])
    _instant(record["retrieved_at"])  # must be a timezone-aware instant
    data = _contained(root, record["path"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != record["sha256"].lower():
        raise ReplayError(f"SHA256 mismatch: {name}")
    text = _gzip_text(data, name, record.get("excerpt_sha256", record["source_sha256"]))
    cpi = _parse_bls(text, embargo)  # reuses BLS embargo + verb aliases + proof
    return release, embargo, cpi


def _logistic_fit(rows: list[tuple[list[float], int]], dim: int) -> list[float]:
    """Deterministic ridge-penalized logistic regression; intercept unpenalized.

    Weights layout: index 0 is the intercept, 1..dim the standardized features.
    """
    weights = [0.0] * (dim + 1)
    n = len(rows)
    for _ in range(MAX_ITERATIONS):
        gradient = [0.0] * (dim + 1)
        for features, label in rows:
            z = weights[0] + sum(weights[j + 1] * features[j] for j in range(dim))
            error = _sigmoid(z) - label
            gradient[0] += error
            for j in range(dim):
                gradient[j + 1] += error * features[j]
        gradient[0] /= n
        for j in range(dim):
            gradient[j + 1] = gradient[j + 1] / n + L2_LAMBDA * weights[j + 1] / n
        weights = [w - LEARNING_RATE * g for w, g in zip(weights, gradient)]
        if sum(g * g for g in gradient) / len(gradient) < CONVERGENCE:
            break
    return weights


def _standardize(train: list[list[float]], dim: int) -> tuple[list[float], list[float]]:
    """Training-only mean and (population) standard deviation per feature."""
    n = len(train)
    means = [sum(row[j] for row in train) / n for j in range(dim)]
    stds = []
    for j in range(dim):
        variance = sum((row[j] - means[j]) ** 2 for row in train) / n
        stds.append(math.sqrt(variance) if variance > 0 else 1.0)
    return means, stds


def _logistic_probability(features: list[float], means: list[float],
                          stds: list[float], weights: list[float]) -> float:
    z = weights[0]
    for j in range(len(features)):
        z += weights[j + 1] * ((features[j] - means[j]) / stds[j])
    return _sigmoid(z)


def _baseline_probability(train: list[dict], lag_sign: int) -> dict:
    """Event-type Laplace base rate keyed on the pre-event Treasury lag sign."""
    matched = [event for event in train if event["lag_sign"] == lag_sign]
    up = sum(event["up"] for event in matched)
    probability = (up + LAPLACE_ALPHA) / (len(matched) + 2 * LAPLACE_ALPHA)
    return {"probability_up": probability, "samples": len(matched), "up": up,
            "event_type": lag_sign}


def _challenger_probability(train: list[dict], current: dict, features: tuple = FEATURES) -> dict:
    dim = len(features)
    design = [[float(event[name]) for name in features] for event in train]
    means, stds = _standardize(design, dim)
    rows = [([(row[j] - means[j]) / stds[j] for j in range(dim)], event["up"])
            for row, event in zip(design, train)]
    weights = _logistic_fit(rows, dim)
    current_values = [float(current[name]) for name in features]
    probability = _logistic_probability(current_values, means, stds, weights)
    return {"probability_up": probability, "samples": len(train)}


def _load_events(root: Path, manifest: dict) -> list[dict]:
    """Verify each event excerpt and return chronologically sorted CPI features."""
    events_raw = manifest["events"]
    if not isinstance(events_raw, list) or not events_raw:
        raise ReplayError("Manifest events must be a nonempty list")
    events = []
    seen_dates = set()
    for index, record in enumerate(events_raw):
        name = f"events[{index}]"
        release, embargo, cpi = _read_excerpt(root, record, name)
        _check_bls_url(record["source_url"], release, name)
        if embargo.astimezone(EASTERN).date() != release:
            raise ReplayError(f"Embargo Eastern date differs from release date: {name}")
        if release in seen_dates:
            raise ReplayError(f"Duplicate event release date: {release}")
        seen_dates.add(release)
        events.append({
            "release_date": release,
            "embargo_at": record["embargo_at"],
            "source_url": record["source_url"],
            "path": record["path"],
            "sha256": record["sha256"],
            "source_sha256": record["source_sha256"],
            "excerpt_sha256": record.get("excerpt_sha256", record["source_sha256"]),
            "retrieved_at": record["retrieved_at"],
            "headline_monthly_percent": cpi["headline_monthly_percent"],
            "core_monthly_percent": cpi["core_monthly_percent"],
            "cpi": cpi,
        })
    events.sort(key=lambda event: event["release_date"])
    return events


def _load_yields(root: Path, manifest: dict, last_release: date) -> tuple[dict, dict]:
    record = manifest["yields"]
    required = {"path", "sha256", "url", "vintage_date", "retrieved_at"}
    if not isinstance(record, dict) or not required <= record.keys():
        raise ReplayError("Incomplete yields record")
    vintage = _date(record["vintage_date"])
    if vintage < last_release:
        raise ReplayError("Yields vintage must be frozen at or after the last event")
    _instant(record["retrieved_at"])
    data = _contained(root, record["path"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != str(record["sha256"]).lower():
        raise ReplayError("SHA256 mismatch: yields")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ReplayError("Invalid UTF-8: yields") from exc
    source = {"name": "yields", "record": {"url": record["url"]}}
    observed, info = _parse_series(text, source, vintage)
    return observed, info


def _label_event(event: dict, yields: dict[date, Decimal]) -> dict | None:
    """Attach the pre-event lag sign and the realized next-session direction.

    Returns None (skip, never imputed) if the release session is unobserved or
    the next observed session is implausibly far past the release, which under a
    conservative calendar bound could not have been labeled contemporaneously.
    """
    release = event["release_date"]
    prior = [day for day in yields if day < release]
    on_or_after = [day for day in yields if day >= release]
    if release not in yields or len(prior) < 2:
        return {**event, "skipped": "missing release-session or pre-event observations"}
    prior.sort()
    lag_change = yields[prior[-1]] - yields[prior[-2]]
    following = [day for day in on_or_after if day > release]
    if not following:
        return {**event, "skipped": "no next observed session after release"}
    next_day = min(following)
    if (next_day - release).days > LABEL_GAP_CALENDAR_DAYS:
        return {**event, "skipped": "next observation beyond the conservative label-availability bound"}
    change = yields[next_day] - yields[release]
    return {**event, "skipped": None, "lag_sign": _sign(lag_change),
            "reference_date": prior[-1], "release_change": float(yields[release] - yields[prior[-1]]),
            "next_date": next_day, "up": int(change > 0),
            "next_change_basis_points": float(change * 100)}


def _vintage_record(record: dict, expected: date, *, before: date | None = None) -> tuple[dict, dict]:
    url = urlsplit(record["url"])
    if (url.scheme, url.hostname, url.path) != ("https", "alfred.stlouisfed.org", "/graph/alfredgraph.csv"):
        raise ReplayError("Unexpected event ALFRED URL")
    day = _date(record["vintage_date"])
    bound = _instant(record["available_at"])
    if day != expected or bound != datetime.combine(day, time(23, 59, 59), EASTERN):
        raise ReplayError("Vintage date or conservative availability bound is inconsistent")
    if _instant(record["retrieved_at"]) < bound:
        raise ReplayError("Yield retrieval precedes its eligible vintage bound")
    text = record["csv"]
    if hashlib.sha256(text.encode()).hexdigest() != record["sha256"]:
        raise ReplayError("Event ALFRED source hash mismatch")
    values, info = _parse_series(text, {"record": {"url": record["url"]}}, day, before=before)
    info.update({key: record[key] for key in ("url", "available_at", "retrieved_at", "sha256")})
    return values, info


def _vintage_labels(root: Path, events: list[dict]) -> list[dict]:
    snapshots = json.loads(_contained(root, "vintages.json").read_text(encoding="utf-8"),
                           object_pairs_hook=_unique_object)["events"]
    if set(snapshots) != {event["release_date"].isoformat() for event in events}:
        raise ReplayError("Per-event vintages must cover every retained release exactly once")
    labeled = []
    for event in events:
        release = event["release_date"]
        pair = snapshots[release.isoformat()]
        post_vintage = min(release + timedelta(days=7), events[-1]["release_date"] + timedelta(days=3))
        prior, prior_info = _vintage_record(pair["before"], release - timedelta(days=1), before=release)
        follow, follow_info = _vintage_record(pair["after"], post_vintage)
        if any(day < release or day > post_vintage for day in follow):
            raise ReplayError("Per-event outcome is outside its archive window")
        label = _label_event(event, prior | follow)
        labeled.append({**label, "label_available_at": follow_info["available_at"],
                        "yield_sources": {"before": prior_info, "after": follow_info}})
    return labeled


def _paired_interval(values: list[float], block: int, repetitions: int = 4000) -> dict | None:
    # Resample contiguous paired score differences and retain each block's order.
    count = len(values)
    if count < 2 * block:
        return None
    rng = random.Random(731_2026)
    means = []
    for _ in range(repetitions):
        drawn = []
        while len(drawn) < count:
            first = rng.randrange(count)
            drawn.extend(values[(first + offset) % count] for offset in range(block))
        means.append(sum(drawn[:count]) / count)
    means.sort()
    low, high = means[int(0.025 * repetitions)], means[int(0.975 * repetitions) - 1]
    return {"lower_95": low, "upper_95": high, "block_length_releases": block, "resamples": repetitions}


def _event_links(event: dict) -> dict:
    return {"release_date": event["release_date"].isoformat(),
            "source_url": event["source_url"], "path": event["path"],
            "sha256": event["sha256"], "source_sha256": event["source_sha256"],
            "excerpt_sha256": event["excerpt_sha256"], "retrieved_at": event["retrieved_at"],
            "embargo_at": event["embargo_at"],
            "headline_monthly_percent": event["headline_monthly_percent"],
            "core_monthly_percent": event["core_monthly_percent"]}


def study(directory: Path, *, vintaged: bool = False) -> dict:
    """Run the offline walk-forward ablation over a local immutable fixture.

    All prediction probabilities are computed from strictly prior labeled events
    before any realized outcome is scored. Raises ReplayError on invalid or
    temporally ineligible evidence; filesystem errors may also propagate.
    """
    root = Path(directory).resolve(strict=True)
    manifest_path = _contained(root, "manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"),
                              object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReplayError("Invalid manifest.json") from exc
    if not isinstance(manifest, dict) or not {"events", "yields"} <= manifest.keys():
        raise ReplayError("Manifest must contain events and yields")

    events = _load_events(root, manifest)
    if vintaged:
        labeled = _vintage_labels(root, events)
        yields_info = {"design": "Each release has separate pre-cutoff and post-outcome ALFRED archives."}
    else:
        yields, yields_info = _load_yields(root, manifest, events[-1]["release_date"])
        yields_info = {**manifest["yields"], **yields_info}
        labeled = [_label_event(event, yields) for event in events]

    # Stage 1: predict every eligible event from strictly prior labeled events.
    # No realized outcome influences any probability (predict-before-score).
    usable = [event for event in labeled if event["skipped"] is None]
    skipped = [{"release_date": event["release_date"].isoformat(), "reason": event["skipped"]}
               for event in labeled if event["skipped"] is not None]
    staged = []
    for index, event in enumerate(usable):
        if vintaged:
            cutoff = _instant(event["embargo_at"]) + timedelta(minutes=15)
            history = [prior for prior in usable[:index] if _instant(prior["label_available_at"]) <= cutoff]
        else:
            history = [prior for prior in usable[:index]
                       if prior["next_date"] + timedelta(days=2) < event["release_date"]]
        if len(history) < MIN_TRAINING_EVENTS:
            continue
        baseline = _baseline_probability(history, event["lag_sign"])
        inputs = {name: event[name] for name in FEATURES}
        market_only = _challenger_probability(history, inputs, ("lag_sign",))
        challenger = _challenger_probability(history, inputs)
        staged.append({
            "event": event,
            "predictions": {"baseline": baseline, "market_only": market_only, "challenger": challenger},
            "training_events": len(history),
        })

    # Stage 2: reveal realized outcomes and score the staged predictions only.
    scored = []
    baseline_briers, market_briers, challenger_briers = [], [], []
    for item in staged:
        event = item["event"]
        up = event["up"]
        baseline_brier = (item["predictions"]["baseline"]["probability_up"] - up) ** 2
        market_brier = (item["predictions"]["market_only"]["probability_up"] - up) ** 2
        challenger_brier = (item["predictions"]["challenger"]["probability_up"] - up) ** 2
        baseline_briers.append(baseline_brier)
        market_briers.append(market_brier)
        challenger_briers.append(challenger_brier)
        scored.append({
            "release_date": event["release_date"].isoformat(),
            "lag_sign": event["lag_sign"],
            "reference_date": event["reference_date"].isoformat(),
            "next_date": event["next_date"].isoformat(),
            "realized_up": up,
            "next_change_basis_points": event["next_change_basis_points"],
            "release_change_basis_points": event["release_change"] * 100,
            "training_events": item["training_events"],
            "baseline_probability_up": item["predictions"]["baseline"]["probability_up"],
            "market_only_probability_up": item["predictions"]["market_only"]["probability_up"],
            "market_only_brier": market_brier,
            "challenger_probability_up": item["predictions"]["challenger"]["probability_up"],
            "baseline_brier": baseline_brier,
            "challenger_brier": challenger_brier,
            "brier_delta": challenger_brier - baseline_brier,
            "source": _event_links(event),
            **({"yield_sources": event["yield_sources"]} if vintaged else {}),
        })

    n = len(scored)
    mean_baseline = sum(baseline_briers) / n if n else None
    mean_market = sum(market_briers) / n if n else None
    mean_challenger = sum(challenger_briers) / n if n else None
    return {
        "mode": "per-event vintaged archived ablation" if vintaged else "archived ablation",
        "title": "CPI text vs. market-only Treasury lag ablation",
        "target": ("Direction of the next observed session's DGS2 minus the release-session "
                   "DGS2; up means strictly positive."),
        "yields": yields_info,
        "counts": {"events": len(events), "labeled": len(usable), "skipped": len(skipped),
                   "scored": n, "minimum_training_events": MIN_TRAINING_EVENTS},
        "range": {"first_release": events[0]["release_date"].isoformat(),
                  "last_release": events[-1]["release_date"].isoformat(),
                  "first_scored": scored[0]["release_date"] if scored else None,
                  "last_scored": scored[-1]["release_date"] if scored else None},
        "skipped_events": skipped,
        "excluded_archives": manifest.get("capture_failures", []),
        "scored_events": scored,
        "summary": {
            "scored": n,
            "mean_baseline_brier": mean_baseline,
            "mean_market_only_brier": mean_market,
            "mean_challenger_brier": mean_challenger,
            "mean_cpi_ablation_delta": (mean_challenger - mean_market) if n else None,
            "cpi_ablation_interval": _paired_interval(
                [expanded - market for expanded, market in zip(challenger_briers, market_briers)], 6
            ),
            "mean_brier_delta": (mean_challenger - mean_baseline) if n else None,
        },
        "methodology": {
            "protocol": ("Expanding-window walk-forward. Each eligible event is predicted only "
                         "from strictly earlier labeled events, after a predeclared minimum of "
                         f"{MIN_TRAINING_EVENTS} such events. Current-event model inputs contain only named pre-cutoff "
                         "features. Predictions are staged before evaluation."),
            "baseline": ("Event-type Laplace constant: the smoothed base rate of upward next "
                         "sessions among prior events sharing the same pre-release Treasury lag "
                         f"sign (alpha={LAPLACE_ALPHA}). Market information only."),
            "market_only": "Ridge logistic regression using the lag sign only, on the same paired sample.",
            "challenger": (f"L2-regularized logistic regression (lambda={L2_LAMBDA}, intercept "
                           "unpenalized) on the lag sign plus released headline and core monthly "
                           "CPI changes, standardized with training-only mean and standard "
                           "deviation. Same walk-forward cutoffs as the baseline."),
            "features": list(FEATURES),
            "label_availability": (
                "The per-event post-vintage is eligible only after its stated Eastern day ends. Training uses "
                "only prior labels eligible by the current embargo plus 15 minutes. Missing observations are skipped."
                if vintaged else "Conservative bound: an event is labeled only when its next "
                                   f"observed session is within {LABEL_GAP_CALENDAR_DAYS} calendar "
                                   "days of the release. Training waits two additional calendar days beyond "
                                   "the next observation. These are analytical buffers. "
                                   "Missing release sessions and longer gaps are skipped."),
            "optimizer": (f"Deterministic full-batch gradient descent (rate={LEARNING_RATE}, "
                          f"max {MAX_ITERATIONS} iterations, stop at mean-squared-gradient "
                          f"< {CONVERGENCE})."),
        },
        "limitations": [
            "A single small ablation is descriptive; the paired Brier delta is not a significance test.",
            "CPI levels do not measure surprise relative to market expectations.",
            (
                "Each release uses an ALFRED vintage dated the day before the embargo for lag inputs. The outcome is "
                "from a bounded later vintage, and ALFRED does not establish first-print or intraday receipt in this dataset."
                if vintaged else
                "Lag values and labels use one frozen post-study vintage. A fully point-in-time study needs each pre-event "
                "market vintage and first published outcomes."
            ),
            "The calendar label bound is conservative and may skip legitimately labeled events.",
            "Observed sessions skip missing values; no exchange-calendar inference or imputation.",
            "The gzip and normalized excerpt hashes are verified offline. Original HTML hashes were recorded at download; re-fetch the original pages to verify those hashes separately.",
        ],
    }
