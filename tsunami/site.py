"""Static, dependency-free publication. No fetching, scripts, or score inference.

Forecast artifacts are content-addressed: a changed record gets a new URL and
existing artifacts are never overwritten. Publish the output directory intact
between builds to retain previously published links. Output must stay in this
repository; symlink destinations are rejected.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


_ROOT = Path(__file__).resolve().parents[1]


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def _number(value: object) -> bool:
    return (type(value) in (int, float) and math.isfinite(value)
            and 0 <= value <= 1)


def _score(value: object) -> str:
    return f"{value:.3f}" if _number(value) else "Not available"


def _probability(value: object) -> str:
    if not _number(value):
        return '<span class="muted">Not available</span>'
    return (f'<span class="probability">{value:.0%}</span>'
            f'<span class="bar" aria-hidden="true" style="--probability: {value * 100:.2f}%">'
            '<span></span></span>')


def _instant(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def _time(value: object) -> str:
    if value is None or value == "":
        return "Not supplied"
    if _instant(value) is not None:
        return f'<time datetime="{_e(value)}">{_e(value)}</time>'
    # Date-only week endings are still valid HTML dates, unlike arbitrary text.
    try:
        if len(str(value)) == 10:
            datetime.strptime(str(value), "%Y-%m-%d")
            return f'<time datetime="{_e(value)}">{_e(value)}</time>'
    except ValueError:
        pass
    return _e(value)


def _link(url: object, title: object) -> str:
    text = _e(title)
    if not isinstance(url, str):
        return text + ' <span class="muted">(link unavailable)</span>'
    try:
        parsed = urlsplit(url)
        safe = (url == url.strip() and not any(ord(c) < 32 or ord(c) == 127 for c in url)
                and "\\" not in url and parsed.scheme.lower() in {"http", "https"}
                and bool(parsed.hostname) and not parsed.username and not parsed.password)
        # Accessing port also rejects malformed port declarations.
        parsed.port
    except ValueError:
        safe = False
    if safe:
        return f'<a href="{_e(url)}" rel="noreferrer noopener">{text}</a>'
    return text + ' <span class="muted">(link unavailable)</span>'


def _result(forecast: dict, results: dict) -> dict:
    result = results.get(forecast.get("id"), {})
    return result if isinstance(result, dict) and result.get("forecast_id") == forecast.get("id") else {}


def _resolved(result: dict) -> bool:
    return (result.get("status") == "resolved" and type(result.get("outcome")) is int
            and result["outcome"] in (0, 1))


def _status(forecast: dict, result: dict, now: datetime) -> str:
    if result.get("status") == "void":
        return "Void · not scored"
    if _resolved(result):
        return "Resolved · " + ("Yes" if result["outcome"] else "No")
    deadline = _instant(forecast.get("deadline_at"))
    if deadline is not None and deadline <= now:
        return "Deadline passed · awaiting resolution"
    return "Unresolved · deadline ahead" if deadline else "Unresolved · deadline unverified"


def _summary(forecasts: list[dict], results: dict) -> dict:
    live = [(f, _result(f, results)) for f in forecasts if f.get("mode") == "prospective"]
    resolved = [(f, r) for f, r in live if _resolved(r)]
    scored = [r["brier"] for _, r in resolved if _number(r.get("brier"))]
    paired = [r for f, r in resolved if _number(f.get("baseline_probability"))
              and _number(r.get("baseline_brier")) and _number(r.get("brier"))]
    return {
        "scope": "Resolved prospective forecasts only; archived replay and void results excluded.",
        "resolved_count": len(resolved), "scored_count": len(scored),
        "mean_brier": sum(scored) / len(scored) if scored else None,
        "baseline_comparison_count": len(paired),
        "paired_mean_brier": sum(r["brier"] for r in paired) / len(paired) if paired else None,
        "mean_baseline_brier": sum(r["baseline_brier"] for r in paired) / len(paired) if paired else None,
    }


def _ledger(forecasts: list[dict], results: dict, artifacts: dict, now: datetime, label: str) -> str:
    if not forecasts:
        return '<p class="empty">No forecasts published in this section yet.</p>'
    rows, articles = [], []
    for f in forecasts:
        name = artifacts[f["id"]]
        anchor = "f-" + name.removesuffix(".json")
        result = _result(f, results)
        status = _status(f, result, now)
        question = _e(f.get("question", "Untitled forecast"))
        mode = "Prospective · live record" if f.get("mode") == "prospective" else "Archived replay · not live"
        rows.append(f'<tr><th scope="row"><a href="#{anchor}">{question}</a>'
                    f'<small>{_e(f.get("id", ""))}</small></th>'
                    f'<td>{_time(f.get("week_ending"))}</td><td>{_probability(f.get("probability"))}</td>'
                    f'<td>{_probability(f.get("baseline_probability"))}</td><td>{_e(status)}</td></tr>')
        timing = "".join(f'<div><dt>{title}</dt><dd>{_time(f.get(key))}</dd></div>' for key, title in (
            ("issued_at", "Issued"), ("as_of_at", "Evidence as of"),
            ("week_ending", "Week ending"), ("deadline_at", "Resolution deadline")))
        risks = "".join(f'<li>{_e(risk)}</li>' for risk in f.get("risks", [])) or '<li>No risks supplied.</li>'
        evidence = "".join('<li>' + _link(e.get("url"), e.get("title", "Source"))
                           + f'<small>Available {_time(e.get("available_at"))}</small></li>'
                           for e in f.get("evidence", []) if isinstance(e, dict)) or '<li>No evidence supplied.</li>'
        resolution = ""
        if result:
            outcome = ("Yes (1)" if result["outcome"] else "No (0)") if _resolved(result) else "Not scored"
            sources = "".join(f'<li>{_link(url, url)}</li>' for url in result.get("source_urls", []))
            resolution = (f'<section class="resolution"><h4>Resolution record</h4><p>{_e(status)} · {outcome}</p>'
                          f'<p>Recorded {_time(result.get("resolved_at"))}</p>'
                          f'<p>{_e(result.get("resolution_note", "No resolution note supplied."))}</p>'
                          f'<p>Brier: {_score(result.get("brier")) if _resolved(result) else "Not scored"}'
                          f' · Baseline Brier: {_score(result.get("baseline_brier")) if _resolved(result) else "Not scored"}</p>'
                          f'<ul class="sources">{sources}</ul></section>')
        publication = _mapping(f.get("publication"))
        receipt = ""
        if publication.get("url"):
            receipt = (f' · {_link(publication["url"], "Immutable GitHub release")}'
                       f' · Published {_time(publication.get("published_at"))}')
        method = f'<p><strong>Method.</strong> {_e(f["method"])}</p>' if f.get("method") else ""
        articles.append(
            f'<article class="forecast" id="{anchor}"><header><p class="eyebrow">{mode}</p>'
            f'<h3>{question}</h3><p class="muted">{_e(f.get("kind", "").replace("_", " "))}'
            f' · {_e(status)}</p></header><dl class="timing">{timing}</dl>'
            f'<p class="rationale">{_e(f.get("rationale", ""))}</p>{method}'
            '<details><summary>Read risks, resolution rule &amp; evidence</summary>'
            f'<div class="detail-grid"><section><h4>What could change the view</h4><ul>{risks}</ul>'
            f'<h4>Resolution rule</h4><p>{_e(f.get("resolution_rule", ""))}</p></section>'
            f'<section><h4>Evidence available at issue</h4><ul class="sources">{evidence}</ul></section></div>'
            f'{resolution}</details><p class="artifact"><a href="forecasts/{name}">Forecast JSON</a>'
            f'{receipt}</p></article>')
    return (f'<div class="table-scroll" role="region" aria-label="{label} forecast ledger" tabindex="0">'
            f'<table><caption>{label} forecast ledger · probabilities of Yes</caption><thead><tr>'
            '<th scope="col">Question / record</th><th scope="col">Week ending</th>'
            '<th scope="col">Forecast</th><th scope="col">Baseline</th><th scope="col">Status at build</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>' + "".join(articles))


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _first(*values: object) -> object:
    return next((v for v in values if v is not None), None)


def _replay(replay: dict) -> str:
    predictions = _mapping(replay.get("predictions"))
    baseline = _mapping(_first(replay.get("baseline"), predictions.get("baseline")))
    event = _mapping(replay.get("event"))
    probability = _first(replay.get("baseline_probability"), baseline.get("probability"), baseline.get("probability_up"))
    score = _first(replay.get("baseline_brier"), replay.get("baseline_score"), baseline.get("brier"),
                   baseline.get("score"), _mapping(event.get("brier")).get("baseline"))
    cutoff = _first(replay.get("cutoff_at"), replay.get("cutoff"), replay.get("as_of_at"))
    title = _first(replay.get("title"), replay.get("question"), "Historical case study")
    outcomes = replay.get("outcomes")
    if not isinstance(outcomes, list):
        outcomes = [dict(value, observation=key) for key in ("event", "next", "outcome")
                    if isinstance(value := replay.get(key), dict)]
    outcomes = [o for o in outcomes if isinstance(o, dict)]
    if not outcomes and replay.get("outcome") is not None and not isinstance(replay.get("outcome"), (dict, list)):
        outcomes = [{"observation": "Outcome", "outcome": replay.get("outcome"),
                     "resolved_at": _first(replay.get("resolved_at"), replay.get("outcome_at"),
                                              replay.get("outcome_date"))}]

    def when(o: dict) -> object:
        return _first(o.get("date"), o.get("resolved_at"), o.get("at"))

    def order(o: dict) -> tuple:
        value = when(o)
        instant = _instant(value)
        if instant is None and isinstance(value, str) and len(value) == 10:
            instant = _instant(value + "T00:00:00Z")
        return (instant is None, instant or datetime.max.replace(tzinfo=timezone.utc), str(value))

    rows = []
    for observation in sorted(outcomes, key=order):
        role = observation.get("observation", "Observation")
        labels = {"event": "Release session", "next": "Next observed session"}
        raw_level = observation.get("DGS2")
        level = f"{raw_level:.2f}%" if type(raw_level) in (int, float) else "—"
        raw_change = observation.get("change_basis_points")
        change = f"{raw_change:+g} bp" if type(raw_change) in (int, float) else "—"
        if observation.get("brier") is not None:
            score_text = "Scored target"
        elif "outcome" in observation:
            score_text = f"Outcome: {observation['outcome']}"
        else:
            score_text = "Context"
        rows.append(f'<tr><th scope="row">{_e(labels.get(role, role))}</th>'
                    f'<td>{_time(when(observation))}</td><td>{level}</td><td>{change}</td><td>{_e(score_text)}</td></tr>')
    observations = (
        '<div class="table-scroll" role="region" aria-label="Chronological Treasury observations" tabindex="0">'
        '<table><caption>Chronological observations</caption><thead><tr><th scope="col">Session</th>'
        '<th scope="col">Date</th><th scope="col">2-year yield</th><th scope="col">Change</th>'
        '<th scope="col">Use</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'
    ) if rows else ""
    cpi = _mapping(replay.get("cpi"))
    cpi_line = ""
    if type(cpi.get("headline_monthly_percent")) in (int, float) and type(cpi.get("core_monthly_percent")) in (int, float):
        source = _mapping(_mapping(replay.get("sources")).get("bls"))
        cpi_line = (f'<p>Monthly headline CPI was <strong>{cpi["headline_monthly_percent"]:+g}%</strong>, '
                    f'with core CPI at <strong>{cpi["core_monthly_percent"]:+g}%</strong>. '
                    f'{_link(source.get("url"), "BLS release")}</p>')
    return (f'<h3>{_e(title)}</h3><dl class="timing"><div><dt>Replay cutoff</dt><dd>{_time(cutoff)}</dd></div>'
            f'<div><dt>Baseline probability</dt><dd>{_probability(probability)}</dd></div>'
            f'<div><dt>Baseline Brier score</dt><dd>{_score(score)}</dd></div></dl>'
            + cpi_line + observations
            + '<p>The scored event is the change from the release session to the next observed session. '
            'This archival reconstruction supplies one Brier trial.</p>'
            '<p><a href="replay.json">Detailed replay JSON · source hashes, timing and scores</a></p>')


def _study(report: dict | None) -> str:
    if report is None:
        return ""
    summary = report["summary"]
    counts = report["counts"]
    range_ = report["range"]
    metrics = "".join(
        f'<div><dt>{label}</dt><dd>{_score(summary.get(key))}</dd></div>'
        for label, key in (
            ("Event-rate Brier", "mean_baseline_brier"),
            ("Lag-only logistic Brier", "mean_market_only_brier"),
            ("CPI + lag logistic Brier", "mean_challenger_brier"),
        )
    )
    delta = summary.get("mean_cpi_ablation_delta")
    if type(delta) in (int, float) and math.isfinite(delta):
        comparison = f" Adding CPI changed mean Brier by {delta:+.3f} against the matched lag-only model."
    else:
        comparison = ""
    interval = _mapping(summary.get("cpi_ablation_interval"))
    interval_line = (
        f' The 95% paired block-bootstrap interval spanned {interval["lower_95"]:+.3f} to {interval["upper_95"]:+.3f}.'
        if interval else ""
    )
    return (
        '<article class="replay study"><h3>Do released CPI changes add signal?</h3>'
        f'<p>Expanding-window comparison over {_e(counts["scored"])} scored releases'
        f' ({_time(range_["first_scored"])} to {_time(range_["last_scored"])}).'
        f'{comparison}{interval_line}</p><dl class="timing">{metrics}</dl>'
        '<p>Each release uses its own ALFRED yield snapshots from before and after the event. '
        'This archival study uses published CPI readings. Market-consensus surprises are outside its scope.</p>'
        '<p><a href="study.json">Walk-forward results · per-release sources, probabilities and scores</a></p></article>'
    )


def _destination(output: Path) -> Path:
    candidate = output if output.is_absolute() else Path.cwd() / output
    # Inspect every component before resolving; no writes through symlinks.
    for part in (candidate, *candidate.parents):
        if part.is_symlink():
            raise ValueError("Output may not traverse a symlink")
    candidate = candidate.resolve()
    if not candidate.is_relative_to(_ROOT) or candidate == _ROOT:
        raise ValueError("Output must be a directory inside this repository")
    return candidate


def render(output: Path, replay: dict, forecasts: list[dict], results: dict[str, dict],
           *, study: dict | None = None) -> None:
    """Publish a static edition, preserving immutable forecast JSON artifacts.

    Scores are reported as supplied, never recomputed. Summary denominators
    explicitly distinguish resolved records, scored records, and baseline pairs.
    Only http(s) evidence URLs become links; all authored text is escaped.
    """
    output = _destination(Path(output))
    ids = [f["id"] for f in forecasts]
    if any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("Forecast IDs must be unique strings")
    if any(f.get("mode") not in {"prospective", "archived_replay"} for f in forecasts):
        raise ValueError("Unknown forecast mode")
    now = datetime.now(timezone.utc)
    built = now.isoformat(timespec="seconds")
    summary = _summary(forecasts, results)
    artifacts, files = {}, {}
    for forecast in forecasts:
        payload = _json(forecast)
        name = hashlib.sha256(payload).hexdigest() + ".json"
        artifacts[forecast["id"]] = name
        files["forecasts/" + name] = payload
    live = sorted((f for f in forecasts if f.get("mode") == "prospective"),
                  key=lambda f: (str(f.get("issued_at", "")), f["id"]), reverse=True)
    archived = [f for f in forecasts if f.get("mode") == "archived_replay"]
    as_of = max((_instant(f.get("as_of_at")) for f in live if _instant(f.get("as_of_at")) is not None), default=None)
    as_of_label = _time(as_of.isoformat()) if as_of else "No live as-of timestamp available"
    stats = (f'<div><dt>Resolved live forecasts</dt><dd>{summary["resolved_count"]}</dd></div>'
             f'<div><dt>Mean Brier · {summary["scored_count"]} scored</dt><dd>{_score(summary["mean_brier"])}</dd></div>'
             f'<div><dt>Baseline Brier · {summary["baseline_comparison_count"]} paired</dt>'
             f'<dd>{_score(summary["mean_baseline_brier"])}</dd></div>')
    page = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Tsunami Research: a public forecast ledger with frozen probabilities, evidence and accountable resolution.">
<title>Tsunami Research — The forecast ledger</title><link rel="stylesheet" href="styles.css"></head>
<body><a class="skip" href="#main">Skip to content</a><div class="publication">
<header class="masthead"><a class="wordmark" href="index.html">Tsunami <span>Research</span></a>
<p>Independent market research</p></header>
<nav aria-label="Primary"><a href="#live">Live ledger</a><a href="#archive">Archived research</a><a href="#reading">Reading the ledger</a><a href="scores.json">Scores JSON</a></nav>
<main id="main"><section class="hero" aria-labelledby="title"><div><p class="eyebrow">The public forecast ledger</p>
<h1 id="title">Weekly market forecasts</h1>
<p class="dek">Each forecast has a probability, a dated evidence trail and a rule for scoring the outcome.</p></div>
<aside class="edition" aria-label="Edition timing"><span class="edition-mark" aria-hidden="true">TR /</span>
<p><strong>Latest live evidence as of</strong><br>{as_of_label}</p><p><strong>Static edition built</strong><br>{_time(built)}</p>
<p>Not a real-time feed. Each record has its own evidence cutoff and resolution deadline. Timestamps retain their stated offsets; +00:00 is UTC.</p></aside></section>
<section class="scoreboard" aria-label="Resolved live score summary"><dl>{stats}</dl>
<p>Resolved prospective forecasts only. Replays, voids and unresolved forecasts never enter these totals.
Lower Brier is better (0–1). Baseline comparison uses matched records only; matched forecast Brier: {_score(summary['paired_mean_brier'])}.</p></section>
<section id="live" aria-labelledby="live-title"><div class="section-heading"><p class="eyebrow">01 / Prospective</p><h2 id="live-title">The live ledger</h2>
<p>Published before resolution. Probabilities are snapshots, not continuously updated prices.</p></div>
{_ledger(live, results, artifacts, now, 'Prospective')}</section>
<section id="archive" class="archive" aria-labelledby="archive-title"><div class="section-heading"><p class="eyebrow">02 / Archived · not live</p>
<h2 id="archive-title">The research archive</h2><p>Historical replay is a separate exercise. It does not count toward live performance.</p></div>
<div class="replay">{_replay(replay)}</div>{_study(study)}{_ledger(archived, results, artifacts, now, 'Archived replay')}</section>
<section id="reading" class="reading" aria-labelledby="reading-title"><p class="eyebrow">03 / Notes to the reader</p><h2 id="reading-title">How to read this ledger</h2>
<div class="detail-grid"><div><h3>A probability, not a promise</h3><p>Each percentage is the probability of Yes under the stated resolution rule.
A missing baseline is not zero. Brier scores measure squared probability error; small samples are not evidence of durable skill.</p></div>
<div><h3>A deadline, not a result</h3><p>Week ending identifies the forecast period, not a promised publication schedule.
Passing a deadline does not resolve a question. Status labels reflect this build only; resolution requires a recorded outcome.
Void records are not scored.</p></div></div><p>Forecast JSON links are content-addressed snapshots. Changes receive a new URL.
<a href="scores.json">Scores and resolution records</a> and <a href="replay.json">replay data</a> describe this edition.</p></section>
</main><footer><strong>Tsunami Research</strong><p>A public research notebook. Not investment advice.</p><a href="#main">Back to the ledger ↑</a></footer>
</div></body></html>
'''
    files.update({"index.html": page.encode("utf-8"),
                  "styles.css": (Path(__file__).parent / "static" / "styles.css").read_bytes(),
                  "replay.json": _json(replay),
                  "scores.json": _json({"built_at": built, "summary": summary, "results": results,
                                        "forecast_files": {i: "forecasts/" + name for i, name in artifacts.items()}})})
    if study is not None:
        files["study.json"] = _json(study)
    # Preflight every target before modifying anything. Never follow a planted
    # destination symlink, including an empty artifact directory.
    artifact_directory = output / "forecasts"
    if artifact_directory.is_symlink():
        raise ValueError("Publication targets may not be symlinks")
    if artifact_directory.exists() and not artifact_directory.is_dir():
        raise ValueError("Forecast artifact target is not a directory")
    for relative, data in files.items():
        target = output / relative
        for part in (target, *target.parents):
            if part.is_symlink():
                raise ValueError("Publication targets may not be symlinks")
        if target.exists():
            if not target.is_file():
                raise ValueError("Publication target is not a file")
            existing = target.read_bytes()
            if relative.startswith("forecasts/") and existing != data:
                raise ValueError("Immutable artifact content mismatch")
    output.mkdir(parents=True, exist_ok=True)
    (output / "forecasts").mkdir(exist_ok=True)
    for relative, data in files.items():
        target = output / relative
        if relative.startswith("forecasts/"):
            if not target.exists():
                with target.open("xb") as stream:
                    stream.write(data)
        else:
            target.write_bytes(data)
