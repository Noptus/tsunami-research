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


def _probability(value: object, *, places: int = 0) -> str:
    if not _number(value):
        return '<span class="muted">Not available</span>'
    return f'<span class="probability">{value:.{places}%}</span>'


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


def _outcome_state(forecasts: list[dict], results: dict, now: datetime) -> str:
    """One-line outcome state for an issue, distinct from the origin projections."""
    live = [f for f in forecasts if f.get("mode") == "prospective"]
    if not live:
        return "Archived replay · not a live issue"
    paired = [(f, _result(f, results)) for f in live]
    resolved = [r for _, r in paired if _resolved(r)]
    void = [r for _, r in paired if r.get("status") == "void"]
    if resolved and len(resolved) + len(void) == len(live):
        yes = sum(1 for r in resolved if r["outcome"])
        tail = f", {len(void)} void" if void else ""
        return f"Resolved · {yes} of {len(resolved)} scored Yes{tail}"
    if len(void) == len(live):
        return "Void · no forecasts scored"
    if resolved or void:
        return f"Partly resolved · {len(resolved)} scored, {len(live) - len(resolved) - len(void)} open"
    deadlines = [_instant(f.get("deadline_at")) for f in live]
    if deadlines and all(d is not None and d <= now for d in deadlines):
        return "Deadline passed · awaiting resolution"
    return "Open · published before resolution"


def _row(f: dict, result: dict, status: str, anchor: str, href: str) -> str:
    question = _e(f.get("question", "Untitled forecast"))
    row_id = f' id="{anchor}"' if href else ""
    outcome = _e(status)
    if _resolved(result) and _number(result.get("brier")):
        outcome += f'<small>Brier {_score(result["brier"])}</small>'
    return (f'<tr{row_id}><th scope="row"><a href="{href}#{anchor}">{question}</a></th>'
            f'<td>{_probability(f.get("probability"))}</td>'
            f'<td>{_probability(f.get("baseline_probability"), places=1)}</td><td>{outcome}</td></tr>')


def _table(rows: list[str], label: str) -> str:
    return (f'<div class="table-scroll forecast-scroll" role="region" aria-label="{label}" tabindex="0">'
            f'<table class="forecast-table"><caption class="screen-reader">{_e(label)}. '
            'Probabilities are for Yes.</caption><thead><tr>'
            '<th scope="col">Question</th><th scope="col">Forecast</th>'
            '<th scope="col">Baseline</th><th scope="col">Outcome</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>')


def _article(f: dict, result: dict, status: str, anchor: str, artifact: str) -> str:
    question = _e(f.get("question", "Untitled forecast"))
    timing = "".join(f'<div><dt>{title}</dt><dd>{_time(f.get(key))}</dd></div>' for key, title in (
        ("issued_at", "Issued"), ("as_of_at", "Evidence cutoff"),
        ("deadline_at", "Deadline")))
    risks = "".join(f'<li>{_e(risk)}</li>' for risk in f.get("risks", [])) or '<li>No risks supplied.</li>'
    evidence = "".join('<li>' + _link(e.get("url"), e.get("title", "Source"))
                       + f'<small>Available {_time(e.get("available_at"))}</small></li>'
                       for e in f.get("evidence", []) if isinstance(e, dict)) or '<li>No evidence supplied.</li>'
    resolution = ""
    if result:
        outcome = ("Yes (1)" if result["outcome"] else "No (0)") if _resolved(result) else "Not scored"
        sources = "".join(f'<li>{_link(url, url)}</li>' for url in result.get("source_urls", []))
        resolution = (f'<section class="resolution"><h4>Outcome</h4><p>{_e(status)} · {outcome}</p>'
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
    return (
        f'<article class="forecast" id="{anchor}"><header><h3>{question}</h3>'
        f'<p class="muted">{_e(status)}</p></header><dl class="timing">{timing}</dl>'
        f'<p class="rationale">{_e(f.get("rationale", ""))}</p>{method}'
        '<details><summary>Sources and resolution</summary>'
        f'<div class="detail-grid"><section><h4>What could change the view</h4><ul>{risks}</ul>'
        f'<h4>Resolution rule</h4><p>{_e(f.get("resolution_rule", ""))}</p></section>'
        f'<section><h4>Evidence available at issue</h4><ul class="sources">{evidence}</ul></section></div>'
        f'{resolution}</details><p class="artifact"><a href="{artifact}">Forecast JSON</a>'
        f'{receipt}</p></article>')


def _ledger(forecasts: list[dict], results: dict, artifacts: dict, now: datetime, label: str,
            *, issue: str = "", artifact_base: str = "") -> str:
    if not forecasts:
        return '<p class="empty">No forecasts published in this section yet.</p>'
    rows, bodies = [], []
    for f in forecasts:
        name = artifacts[f["id"]]
        anchor = "f-" + name.removesuffix(".json")
        result = _result(f, results)
        status = _status(f, result, now)
        rows.append(_row(f, result, status, anchor, issue))
        if not issue:
            bodies.append(_article(f, result, status, anchor, artifact_base + "forecasts/" + name))
    return _table(rows, label) + "".join(bodies)


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


_EDITORIAL = Path(__file__).parent / "static" / "editorial.json"


def _editorial(overrides: dict | None) -> dict:
    """Authored dated copy keyed by week, from the tracked file and any override."""
    copy = {}
    try:
        loaded = json.loads(_EDITORIAL.read_text(encoding="utf-8"))
        copy = {k: v for k, v in loaded.items() if isinstance(v, dict) and not k.startswith("_")}
    except (OSError, ValueError):
        copy = {}
    for week, entry in _mapping(overrides).items():
        if isinstance(entry, dict):
            copy[week] = {**copy.get(week, {}), **entry}
    return copy


def _group_issues(forecasts: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group prospective forecasts into dated issues, newest week first.

    Records that share a ``week_ending`` form one issue. Anything without a
    usable week falls into its own singleton issue keyed by the empty string so
    it is never silently dropped.
    """
    issues: dict[str, list[dict]] = {}
    for f in forecasts:
        week = str(f.get("week_ending")) if f.get("week_ending") not in (None, "") else ""
        issues.setdefault(week, []).append(f)
    ordered = sorted(issues.items(), key=lambda kv: (kv[0] != "", kv[0]), reverse=True)
    return ordered


def _permalink(week: str) -> str | None:
    """Stable per-issue URL, only for plain ISO week-ending dates."""
    if len(week) == 10:
        try:
            datetime.strptime(week, "%Y-%m-%d")
            return f"issues/{week}.html"
        except ValueError:
            return None
    return None


def _week_label(week: str) -> str:
    if _permalink(week):
        day = datetime.strptime(week, "%Y-%m-%d")
        return f"{day:%B} {day.day}, {day.year}"
    return "Undated issue"


def _issue_title(week: str, copy: dict) -> str:
    entry = _mapping(copy.get(week))
    fallback = "Week ending " + _week_label(week) if _permalink(week) else "Undated issue"
    return str(entry.get("title") or fallback)


def _issue_lead(week: str, copy: dict) -> str:
    text = _mapping(copy.get(week)).get("synthesis")
    return f'<p class="synthesis">{_e(text)}</p>' if isinstance(text, str) and text.strip() else ""


def _issue_note(week: str, copy: dict) -> str:
    note = _mapping(copy.get(week)).get("note")
    return f'<p class="muted audited-note">{_e(note)}</p>' if isinstance(note, str) and note.strip() else ""


def _page(title: str, description: str, body: str, *, base: str = "") -> bytes:
    return (f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{_e(description)}">
<title>{_e(title)}</title><link rel="stylesheet" href="{base}styles.css"></head>
<body><a class="skip" href="#main">Skip to content</a><div class="publication">
<header class="masthead"><a class="wordmark" href="{base}index.html">Tsunami <span>Research</span></a>
<p>Weekly market forecasts</p></header>
<nav aria-label="Primary"><a href="{base + 'index.html' if base else ''}#live">Latest issue</a>
<a href="{base + 'index.html' if base else ''}#archive">Archive</a>
<a href="{base + 'index.html' if base else ''}#performance">Performance</a>
<a href="{base + 'index.html' if base else ''}#reading">Method</a></nav>
<main id="main">{body}</main>
<footer><a href="{base}index.html">Tsunami Research</a><a href="#main">Back to top</a></footer>
</div></body></html>
''').encode("utf-8")


def _issue_header(week: str, copy: dict, *, latest: bool) -> str:
    title = _e(_issue_title(week, copy))
    label = _week_label(week)
    prefix = "Latest issue" if latest else "Weekly issue"
    date_label = (
        f'<p class="eyebrow">{prefix} · week ending <time datetime="{_e(week)}">{_e(label)}</time></p>'
        if _permalink(week) else f'<p class="eyebrow">{prefix}</p>'
    )
    return f'{date_label}<h1 id="live-title">{title}</h1>{_issue_lead(week, copy)}'


def _issue_page(week: str, forecasts: list[dict], results: dict, artifacts: dict,
                now: datetime, copy: dict) -> bytes:
    title = _issue_title(week, copy)
    state = _outcome_state(forecasts, results, now)
    body = (
        f'<section class="issue-lead" id="live" aria-labelledby="live-title">{_issue_header(week, copy, latest=False)}'
        f'<p class="issue-meta">{_e(state)}.</p></section>'
        f'<section aria-labelledby="forecasts-title"><h2 id="forecasts-title" class="screen-reader">Forecasts</h2>'
        f'{_ledger(forecasts, results, artifacts, now, "Issue forecasts", artifact_base="../")}</section>'
        f'{_issue_note(week, copy)}<p class="back"><a href="../index.html#archive">All issues</a></p>')
    return _page(f"{title} | Tsunami Research",
                 f"Forecasts and evidence for the week ending {_week_label(week)}.", body, base="../")


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
           *, study: dict | None = None, editorial: dict | None = None) -> None:
    """Publish a static edition, preserving immutable forecast JSON artifacts.

    Scores are reported as supplied, never recomputed. Summary denominators
    explicitly distinguish resolved records, scored records, and baseline pairs.
    Only http(s) evidence URLs become links; all authored text is escaped.

    Prospective forecasts are grouped into dated weekly issues. The newest issue
    leads the home page; all dated issues get stable permalinks under ``issues/``.
    ``editorial`` optionally overrides authored copy keyed by ``week_ending``.
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
    copy = _editorial(editorial)
    artifacts, files = {}, {}
    for forecast in forecasts:
        payload = _json(forecast)
        name = hashlib.sha256(payload).hexdigest() + ".json"
        artifacts[forecast["id"]] = name
        files["forecasts/" + name] = payload
    prospective = [f for f in forecasts if f.get("mode") == "prospective"]
    archived = [f for f in forecasts if f.get("mode") == "archived_replay"]
    issues = _group_issues(prospective)
    stats = (f'<div><dt>Resolved forecasts</dt><dd>{summary["resolved_count"]}</dd></div>'
             f'<div><dt>Mean Brier</dt><dd>{_score(summary["mean_brier"])}</dd></div>'
             f'<div><dt>Baseline Brier</dt><dd>{_score(summary["mean_baseline_brier"])}</dd></div>')

    if issues:
        lead_week, lead = issues[0]
        issue_path = _permalink(lead_week) or ""
        release = _mapping(lead[0].get("publication"))
        publication = (
            f'{_link(release["url"], "Original release")} · Published {_time(release.get("published_at"))}'
            if release.get("url") else ""
        )
        see_issue = f'<a class="issue-link" href="{issue_path}">Read the issue</a>' if issue_path else ""
        lead_body = (f'{_issue_header(lead_week, copy, latest=True)}'
                     f'{_ledger(lead, results, artifacts, now, "Latest forecasts", issue=issue_path)}'
                     f'<p class="issue-meta">{see_issue} {publication}</p>')
    else:
        lead_body = '<h1 id="live-title">Weekly market forecasts</h1><p>No forecasts published in this section yet.</p>'

    archive = []
    for week, members in issues:
        href = _permalink(week)
        label = _e(_week_label(week))
        title = _e(_issue_title(week, copy))
        if href:
            meta = _e(_outcome_state(members, results, now))
            anchors = "" if week == issues[0][0] else "".join(
                f'<span class="legacy-anchor" id="f-{artifacts[f["id"]].removesuffix(".json")}"></span>'
                for f in members)
            archive.append(f'<li><span>{anchors}<time datetime="{_e(week)}">{label}</time></span>'
                           f'<a href="{href}">{title}</a><small>{meta}</small></li>')
        elif week != (issues[0][0] if issues else None):
            archive.append(f'<li>{_ledger(members, results, artifacts, now, "Undated forecasts")}</li>')
    issue_index = (f'<ul class="issue-index">{"".join(archive)}</ul>' if archive else
                   '<p>No dated issues yet.</p>')
    archival_forecasts = _ledger(archived, results, artifacts, now, "Historical forecasts") if archived else ""
    performance = (
        f'<dl>{stats}</dl><p>Lower Brier is better. The average uses {summary["scored_count"]} resolved '
        f'forecasts, with {summary["baseline_comparison_count"]} baseline pairs. Matched forecast '
        f'Brier is {_score(summary["paired_mean_brier"])}. '
        '<a href="scores.json">Download scores and outcomes</a>.</p>'
    ) if summary["resolved_count"] else (
        '<p>Awaiting the first forecast outcomes. <a href="scores.json">Download scores and outcomes</a>.</p>'
    )
    body = f'''<section id="live" class="issue-lead" aria-labelledby="live-title">{lead_body}</section>
<section id="archive" class="archive" aria-labelledby="archive-title">
<h2 id="archive-title">Archive</h2>{issue_index}
<div class="research-notes">
<div class="replay">{_replay(replay)}</div>{_study(study)}{archival_forecasts}
</div></section>
<section id="performance" class="scoreboard" aria-labelledby="performance-title">
<h2 id="performance-title">Performance</h2>{performance}</section>
<section id="reading" class="reading" aria-labelledby="reading-title">
<h2 id="reading-title">Method</h2>
<p>An issue gives the probability of a Yes outcome. The release records the original estimate, its sources and the rule for scoring it.
The forecast page adds the result when the rule can be applied. Passing a deadline does not resolve a question.</p>
<p>Brier measures the squared difference between the probability and the result.
Performance uses resolved prospective forecasts. The CPI replay and historical study are reported separately.</p>
<p><a href="https://github.com/Noptus/tsunami-research/blob/main/docs/weekly-editorial.md">How an issue is made</a>
 · <a href="https://github.com/Noptus/tsunami-research">Source code</a></p></section>
'''
    page = _page("Tsunami Research", "Weekly market forecasts and their results.", body)
    files.update({"index.html": page,
                  "styles.css": (Path(__file__).parent / "static" / "styles.css").read_bytes(),
                  "replay.json": _json(replay),
                  "scores.json": _json({"built_at": built, "summary": summary, "results": results,
                                        "forecast_files": {i: "forecasts/" + name for i, name in artifacts.items()}})})
    for week, members in issues:
        permalink = _permalink(week)
        if permalink:
            files[permalink] = _issue_page(week, members, results, artifacts, now, copy)
    if study is not None:
        files["study.json"] = _json(study)
    # Preflight every target before modifying anything. Never follow a planted
    # destination symlink, including an empty artifact directory.
    artifact_directory = output / "forecasts"
    if artifact_directory.is_symlink():
        raise ValueError("Publication targets may not be symlinks")
    if artifact_directory.exists() and not artifact_directory.is_dir():
        raise ValueError("Forecast artifact target is not a directory")
    issue_directory = output / "issues"
    if issue_directory.is_symlink():
        raise ValueError("Publication targets may not be symlinks")
    if issue_directory.exists() and not issue_directory.is_dir():
        raise ValueError("Issue permalink target is not a directory")
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
    (output / "issues").mkdir(exist_ok=True)
    for relative, data in files.items():
        target = output / relative
        if relative.startswith("forecasts/"):
            if not target.exists():
                with target.open("xb") as stream:
                    stream.write(data)
        else:
            target.write_bytes(data)
