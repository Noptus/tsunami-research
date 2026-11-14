"""Command line for replay, weekly publication, settlement, and site rendering."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import editorial
from .capture import capture_cpi
from .cpi import replay
from .draft import propose
from .ledger import (
    digest,
    json_bytes,
    load_ledger,
    market_baseline,
    market_observation,
    read_json,
    score,
    utcnow,
    validate_issue,
    write_new,
)
from .publication import attest
from .site import render
from .study import study


ROOT = Path.cwd()
DEMO = ROOT / "data" / "cpi-2026-11-10"
STUDY = ROOT / "data" / "cpi-study"


def _write(value: object, path: Path | None = None) -> None:
    if path is None:
        sys.stdout.buffer.write(json_bytes(value))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(value))


def _baseline(args: argparse.Namespace) -> None:
    issue_path = ROOT / "forecasts" / f"{args.week}.json"
    issue = validate_issue(read_json(issue_path), args.week)
    if (ROOT / "publications" / f"{args.week}.json").exists() and args.apply:
        raise ValueError("A published issue cannot be edited")
    baselines = {}
    for forecast in issue:
        if forecast["kind"] == "market_threshold":
            calculated = market_baseline(forecast)
            baselines[forecast["id"]] = calculated
            if args.apply:
                forecast["baseline_probability"] = calculated["probability"]
                forecast["baseline"] = calculated
    if args.apply:
        _write(issue, issue_path)
    _write(baselines)


def _attest(args: argparse.Namespace) -> None:
    receipt = attest(ROOT, args.repository, args.week)
    write_new(ROOT / "publications" / f"{args.week}.json", receipt)
    _write(receipt)


def _settle() -> None:
    records, existing = load_ledger(ROOT)
    settled = []
    for forecast in records:
        if forecast["kind"] != "market_threshold" or forecast["id"] in existing:
            continue
        unwrapped = {key: value for key, value in forecast.items() if key != "publication"}
        observed = market_observation(unwrapped)
        if observed is not None:
            score(unwrapped, observed)
            write_new(ROOT / "results" / f"{forecast['id']}.json", observed)
            settled.append(observed["forecast_id"])
    _write({"settled": settled})


def _adjudicate(args: argparse.Namespace) -> None:
    records, _ = load_ledger(ROOT)
    forecast = next((item for item in records if item["id"] == args.id), None)
    if forecast is None or forecast["kind"] != "binary_event":
        raise ValueError("Adjudication requires a registered binary event")
    unwrapped = {key: value for key, value in forecast.items() if key != "publication"}
    observed = {
        "forecast_id": forecast["id"],
        "forecast_sha256": digest(unwrapped),
        "resolved_at": utcnow().isoformat(),
        "status": "void" if args.outcome == "void" else "resolved",
        "source_urls": args.source,
        "resolution_note": args.note,
    }
    if args.outcome != "void":
        observed["outcome"] = int(args.outcome == "yes")
    scored = score(unwrapped, observed)
    write_new(ROOT / "results" / f"{forecast['id']}.json", observed)
    _write(scored)


def _editorial_prepare(args: argparse.Namespace) -> None:
    proposal = read_json(args.proposal) if args.proposal else None
    sources = read_json(args.sources) if args.sources else None
    manifest_dir = args.sources.resolve().parent if args.sources else None
    packet = editorial.prepare(
        args.week, proposal=proposal, sources=sources,
        manifest_dir=manifest_dir, root=ROOT,
    )
    write_new(args.output, packet)
    _write(packet)


def _editorial_review(args: argparse.Namespace) -> None:
    packet = read_json(args.packet)
    issue = read_json(args.candidate)
    report = editorial.review(packet, issue, args.week)
    if args.output is not None:
        # Content-addressed by issue digest so a changed candidate never
        # overwrites a prior review.
        path = args.output if args.output.suffix == ".json" else (
            args.output / f"review-{report['issue_sha256']}-{report['packet_digest']}.json"
        )
        write_new(path, report)
    _write(report)


def _editorial_stage(args: argparse.Namespace) -> None:
    issue = read_json(args.candidate)
    report = read_json(args.report)
    result = editorial.stage(ROOT, issue, args.week, read_json(args.packet), report)
    _write(result)


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(prog="python3 -m tsunami", description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    run_replay = commands.add_parser("replay", help="Run an offline historical CPI replay")
    run_replay.add_argument("--fixture", type=Path, default=DEMO)
    run_replay.add_argument("--output", type=Path)
    run_study = commands.add_parser("study", help="Evaluate the archived CPI feature ablation")
    run_study.add_argument("--fixture", type=Path, default=STUDY)
    run_study.add_argument("--output", type=Path)
    run_study.add_argument("--single-vintage", action="store_true", help="Reproduce the initial single-vintage analysis")
    capture = commands.add_parser("capture-cpi", help="Freeze an upcoming CPI release within 15 minutes of embargo lift")
    capture.add_argument("--embargo", required=True, help="Expected BLS release time in ISO 8601 with offset")
    capture.add_argument("--output", type=Path, required=True)
    build = commands.add_parser("build", help="Render the GitHub Pages site")
    build.add_argument("--output", type=Path, default=ROOT / "_site")
    draft = commands.add_parser("propose-week", help="Build a reviewable weekly market issue draft")
    draft.add_argument("--week", required=True)
    baseline = commands.add_parser("baseline", help="Compute FRED SP500 base rates for a weekly issue")
    baseline.add_argument("--week", required=True)
    baseline.add_argument("--apply", action="store_true", help="Record the base rates in an unpublished draft")
    validate = commands.add_parser("validate", help="Check source, ledger, and scoring invariants")
    validate.add_argument("--repository", help="Re-verify public immutable release receipts online")
    publication = commands.add_parser("attest", help="Record an immutable public release as publication evidence")
    publication.add_argument("--repository", required=True)
    publication.add_argument("--week", required=True)
    commands.add_parser("settle-markets", help="Settle eligible verified market questions from FRED")
    adjudicate = commands.add_parser("adjudicate", help="Record a human-reviewed named-event outcome")
    adjudicate.add_argument("--id", required=True)
    adjudicate.add_argument("--outcome", choices=("yes", "no", "void"), required=True)
    adjudicate.add_argument("--note", required=True)
    adjudicate.add_argument("--source", nargs="+", required=True)
    editorial_cli = commands.add_parser("editorial", help="Offline editorial intake and reproducible issue review")
    editorial_sub = editorial_cli.add_subparsers(dest="stage", required=True)
    prepare = editorial_sub.add_parser("prepare", help="Freeze an immutable local packet of baselines and source receipts")
    prepare.add_argument("--week", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--proposal", type=Path, help="Existing proposal JSON; otherwise propose and source baselines")
    prepare.add_argument("--sources", type=Path, help="Optional JSON manifest of supplemental primary-source receipts")
    review = editorial_sub.add_parser("review", help="Validate a candidate issue against a saved packet")
    review.add_argument("--week", required=True)
    review.add_argument("--packet", type=Path, required=True)
    review.add_argument("--candidate", type=Path, required=True)
    review.add_argument("--output", type=Path, help="Write the review (content-addressed by issue digest)")
    stage_cmd = editorial_sub.add_parser("stage", help="Copy a validated candidate into forecasts/ (no publish)")
    stage_cmd.add_argument("--week", required=True)
    stage_cmd.add_argument("--candidate", type=Path, required=True)
    stage_cmd.add_argument("--report", type=Path, required=True)
    stage_cmd.add_argument("--packet", type=Path, required=True)
    args = cli.parse_args(argv)
    try:
        if args.command == "replay":
            _write(replay(args.fixture), args.output)
        elif args.command == "study":
            _write(study(args.fixture, vintaged=not args.single_vintage), args.output)
        elif args.command == "capture-cpi":
            _write(capture_cpi(args.embargo, args.output))
        elif args.command == "build":
            forecasts, results = load_ledger(ROOT)
            render(args.output, replay(DEMO), forecasts, results, study=study(STUDY, vintaged=True))
        elif args.command == "propose-week":
            _write(propose(args.week))
        elif args.command == "baseline":
            _baseline(args)
        elif args.command == "attest":
            _attest(args)
        elif args.command == "settle-markets":
            _settle()
        elif args.command == "adjudicate":
            _adjudicate(args)
        elif args.command == "editorial":
            if args.stage == "prepare":
                _editorial_prepare(args)
            elif args.stage == "review":
                _editorial_review(args)
            elif args.stage == "stage":
                _editorial_stage(args)
        elif args.command == "validate":
            replay(DEMO)
            checked, results = load_ledger(ROOT, allow_unpublished=True)
            published = {record["week_ending"] for record in checked if "publication" in record}
            if args.repository:
                for week in sorted(published):
                    if attest(ROOT, args.repository, week) != read_json(ROOT / "publications" / f"{week}.json"):
                        raise ValueError(f"Remote receipt changed: {week}")
            _write({"validated_forecasts": len(checked), "published_issues": len(published), "resolved": len(results)})
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        cli.exit(2, f"tsunami: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
