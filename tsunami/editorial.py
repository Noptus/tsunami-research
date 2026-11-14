"""Prepare an evidence packet, review a candidate, and write it to the ledger."""

from __future__ import annotations

import fcntl
import hashlib
from copy import deepcopy
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

from .cpi import _contained, _date, _instant
from .draft import propose
from .ledger import _host, _require, CHECKSUM, digest, read_json, utcnow, validate_issue, write_new


PACKET_VERSION = 1
MAX_SNAPSHOT_BYTES = 2_000_000


def _stamp(value: object) -> str:
    return _instant(value).astimezone(timezone.utc).isoformat()


def _source_key(source: dict) -> tuple[str, str]:
    _host(source.get("url"))
    return source["url"], _stamp(source.get("available_at"))


def _source_receipt(entry: object, clock: datetime, base: Path | None = None) -> dict:
    _require(isinstance(entry, dict), "Source entry must be an object")
    _require(isinstance(entry.get("title"), str) and bool(entry["title"].strip()),
             "Source needs a title")
    url, available = _source_key(entry)
    _require(_instant(available) <= clock, "Source availability is after the packet time")
    receipt = {"title": entry["title"].strip(), "url": url, "available_at": available,
               "payload_verified": False}
    if entry.get("retrieved_at") is not None:
        retrieved = _stamp(entry["retrieved_at"])
        _require(_instant(available) <= _instant(retrieved) <= clock,
                 "Source retrieval must fall between availability and packet time")
        receipt["retrieved_at"] = retrieved
    sha = entry.get("sha256")
    if sha is not None:
        _require(isinstance(sha, str) and CHECKSUM.fullmatch(sha) is not None,
                 "Source SHA-256 must be 64 lowercase hexadecimal characters")
        receipt.update(sha256=sha, sha256_declared_only=True)
    if "path" in entry:
        _require(base is not None and "retrieved_at" in receipt,
                 "Snapshot files require a manifest directory and retrieval time")
        path = _contained(base.resolve(), entry["path"])
        with path.open("rb") as source:
            data = source.read(MAX_SNAPSHOT_BYTES + 1)
        _require(len(data) <= MAX_SNAPSHOT_BYTES, f"Snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes")
        actual = hashlib.sha256(data).hexdigest()
        _require(sha is None or sha == actual, f"Snapshot hash does not match: {url}")
        receipt.update(sha256=actual, payload_verified=True, snapshot_bytes=len(data))
        receipt.pop("sha256_declared_only", None)
    return receipt


def _group_sources(receipts: list[dict]) -> list[dict]:
    groups = {}
    for receipt in receipts:
        key = _source_key(receipt)
        existing = groups.get(key)
        if existing is not None:
            old, new = existing.get("sha256"), receipt.get("sha256")
            _require(not old or not new or old == new, f"Conflicting digests for {receipt['url']}")
            if existing.get("payload_verified") or (
                not receipt.get("payload_verified") and old and not new
            ):
                continue
        groups[key] = receipt
    return [groups[key] for key in sorted(groups)]


def _proposal_records(proposal: object, week: str, clock: datetime) -> list[dict]:
    _require(isinstance(proposal, dict) and proposal.get("week_ending") == week,
             "Proposal must be an object for the requested week")
    records = proposal.get("records")
    _require(isinstance(records, list), "Proposal records must be a list")
    if proposal.get("status") == "abstained":
        _require(not records and isinstance(proposal.get("reason"), str) and bool(proposal["reason"].strip()),
                 "An abstention must have a reason and no records")
        return records
    _require(proposal.get("status") == "proposed", "Invalid proposal status")
    validate_issue(records, week)
    _require(all(_instant(record["issued_at"]) <= clock for record in records),
             "Proposal issuance is after the packet time")
    return records


def _packet_digest(packet: dict) -> str:
    return digest({key: value for key, value in packet.items() if key != "packet_digest"})


def prepare(week: str, *, proposal: dict | None = None, sources: object | None = None,
            manifest_dir: Path | None = None, now: datetime | None = None,
            root: Path | None = None) -> dict:
    """Pin the proposal and the source receipts. Snapshot bytes stay local."""
    _require(_date(week).weekday() == 4, "Week ending must fall on a Friday")
    if proposal is None:
        proposal = propose(week, now=now)
    clock = now if now is not None else utcnow()
    _stamp(clock.isoformat())
    proposal = deepcopy(proposal)
    records = _proposal_records(proposal, week, clock)
    _require(sources is None or isinstance(sources, list), "Sources manifest must be an array")
    receipts = _group_sources([
        _source_receipt(entry, clock, manifest_dir or root or Path.cwd())
        for entry in sources or []
    ])
    # Supplemental receipts may attest a proposal citation but cannot disagree with it.
    _group_sources([
        *(_source_receipt(entry, clock) for r in records for entry in r["evidence"]),
        *receipts,
    ])
    packet = {
        "schema": "tsunami.editorial.packet",
        "version": PACKET_VERSION,
        "week_ending": week,
        "prepared_at": _stamp(clock.isoformat()),
        "proposal_status": proposal["status"],
        "proposal": proposal,
        "baseline_digest": digest(records) if records else None,
        "sources": receipts,
        "source_count": len(receipts),
    }
    packet["packet_digest"] = _packet_digest(packet)
    return packet


def verify_packet(packet: object) -> dict:
    _require(isinstance(packet, dict) and packet.get("schema") == "tsunami.editorial.packet",
             "Not an editorial packet")
    _require(packet.get("version") == PACKET_VERSION, "Unsupported packet version")
    _require(packet.get("packet_digest") == _packet_digest(packet), "Packet digest does not match its content")
    week = packet.get("week_ending")
    _require(_date(week).weekday() == 4, "Week ending must fall on a Friday")
    clock = _instant(packet.get("prepared_at"))
    records = _proposal_records(packet.get("proposal"), week, clock)
    _require(packet.get("proposal_status") == packet["proposal"]["status"], "Proposal status differs in packet")
    _require(packet.get("baseline_digest") == (digest(records) if records else None), "Baseline digest differs in packet")
    sources = packet.get("sources")
    _require(isinstance(sources, list) and len(sources) == packet.get("source_count"), "Invalid source count")
    for receipt in sources:
        normalized = _source_receipt(receipt, clock)
        if receipt.get("payload_verified") is True:
            _require(type(receipt.get("snapshot_bytes")) is int
                     and 0 <= receipt["snapshot_bytes"] <= MAX_SNAPSHOT_BYTES
                     and "sha256" in normalized and "retrieved_at" in normalized,
                     "Invalid snapshot receipt")
            normalized.update(payload_verified=True, snapshot_bytes=receipt["snapshot_bytes"])
            normalized.pop("sha256_declared_only")
        _require(receipt == normalized, "Source receipt does not match its schema")
    _require(sources == _group_sources(sources), "Source receipts must be canonical and unique")
    return packet


def _tail_probability_bound(issue: list[dict]) -> None:
    markets = [f for f in issue if f["kind"] == "market_threshold"]
    for one, two in combinations(markets, 2):
        left, right = one["market"], two["market"]
        if any(left[k] != right[k] for k in ("series", "start_date", "end_date")):
            continue
        if left["direction"] == right["direction"]:
            continue
        lower, upper = (left, right) if left["direction"] == "lte" else (right, left)
        if lower["threshold_pct"] < upper["threshold_pct"]:
            _require(one["probability"] + two["probability"] <= 1 + 1e-12,
                     "Mutually exclusive market tails exceed total probability one")


def review(packet: object, issue: object, week: str, *, now: datetime | None = None) -> dict:
    """Report schema, evidence, timestamp and baseline checks for one exact issue."""
    clock = now if now is not None else utcnow()
    _stamp(clock.isoformat())
    packet = verify_packet(packet)
    _require(packet["week_ending"] == week, "Packet is for a different week")
    prepared = _instant(packet["prepared_at"])
    _require(prepared <= clock, "Packet time is after review")
    validated = validate_issue(issue, week)
    proposal = packet["proposal"]["records"]
    pinned_sources = _group_sources([
        *(_source_receipt(entry, prepared) for r in proposal for entry in r["evidence"]),
        *packet["sources"],
    ])
    sources = {_source_key(s): s for s in pinned_sources}
    verified, cited, market_checks = set(), set(), []
    evidence_count = 0
    for forecast in validated:
        as_of = _instant(forecast["as_of_at"])
        _require(_instant(forecast["issued_at"]) <= clock, "Issue time is after review")
        for citation in forecast["evidence"]:
            evidence_count += 1
            source = sources.get(_source_key(citation))
            _require(source is not None, f"Evidence is missing from packet or its time changed: {citation['url']}")
            _require(source.get("sha256") == citation.get("sha256"),
                     f"Evidence digest differs from its receipt: {citation['url']}")
            if source.get("retrieved_at"):
                _require(_instant(source["retrieved_at"]) <= as_of,
                         f"Snapshot was retrieved after the forecast cutoff: {citation['url']}")
            (verified if source["payload_verified"] else cited).add(source["url"])
        if forecast["kind"] == "market_threshold":
            matches = [r for r in proposal if r.get("market") == forecast["market"]]
            _require(len(matches) == 1 and all(
                matches[0].get(key) == forecast.get(key) for key in ("baseline_probability", "baseline")
            ), f"Market forecast differs from the frozen proposal or baseline: {forecast['id']}")
            market_checks.append(forecast["id"])
    _tail_probability_bound(validated)
    return {
        "schema": "tsunami.editorial.review",
        "version": PACKET_VERSION,
        "week_ending": week,
        "packet_digest": packet["packet_digest"],
        "issue_sha256": digest(validated),
        "forecast_count": len(validated),
        "forecast_ids": [f["id"] for f in validated],
        "evidence_checked": evidence_count,
        "snapshot_verified_at_intake_urls": sorted(verified),
        "cited_source_urls": sorted(cited),
        "market_baselines_anchored": sorted(market_checks),
        "checked": [
            "schema: validate_issue",
            "evidence and retrieval cutoffs",
            "packet and proposal digests",
            "cited evidence bound to packet or proposal",
            "market terms and baseline match the proposal",
            "mutually exclusive market tail probabilities",
        ],
        "not_checked": [
            "factual truth of any cited claim",
            "accuracy of declared availability or retrieval timestamps",
            "probability calibration",
        ],
    }


def stage(root: Path, issue: object, week: str, packet: object, report: object,
          *, now: datetime | None = None) -> dict:
    """Recheck the reviewed candidate and seal a local forecast file."""
    checked = review(packet, issue, week, now=now)
    _require(report == checked, "Review report differs from this packet or issue")
    folder = root / "forecasts"
    target = folder / f"{week}.json"
    _require(not folder.is_symlink() and not target.is_symlink(), "Forecast path may not traverse a symlink")
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".editorial-stage.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ids = {record["id"] for record in issue}
        for path in folder.glob("*.json"):
            if path != target:
                _require(not ids.intersection(record["id"] for record in read_json(path)),
                         "A forecast ID is already registered in another issue")
        if target.exists():
            _require(read_json(target) == issue, f"An issue already exists for {week}")
        else:
            write_new(target, issue)
    return {"staged": str(target), "week_ending": week, "issue_sha256": checked["issue_sha256"],
            "forecast_ids": sorted(ids)}
