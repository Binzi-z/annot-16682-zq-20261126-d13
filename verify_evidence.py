#!/usr/bin/env python3
"""Independent standard-library recomputer for evidence/evidence.json.

This file intentionally does not import duty_audit.py.  It verifies:
  * SHA-256 binding to the synthetic source input and minute CSV;
  * half-open endpoint arithmetic;
  * least-fixed-point role closure from the facts embedded per decision;
  * every reported delegation path;
  * approve/reject verdicts and late/action-time alternatives;
  * the post-patch full count/state hash used by the incremental claim.

Usage:
    py -3.12 verify_evidence.py evidence\\evidence.json
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
MINUTE = timedelta(minutes=1)
ROLES = ("intake", "approval")


def parse_ts(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"naive timestamp: {value}")
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def covers(record: dict[str, Any], minute: datetime) -> bool:
    return parse_ts(record["start"]) <= minute < parse_ts(record["end"])


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def minute_span(world: dict[str, Any]) -> list[datetime]:
    starts = [parse_ts(r["start"]) for c in ("shifts", "delegations") for r in world[c]]
    ends = [parse_ts(r["end"]) for c in ("shifts", "delegations") for r in world[c]]
    current, end = min(starts), max(ends)
    result = []
    while current < end:
        result.append(current)
        current += MINUTE
    return result


def closure(
    shifts: list[dict[str, Any]],
    delegations: list[dict[str, Any]],
    minute: datetime | None = None,
) -> set[tuple[str, str, str]]:
    """Independent least fixed point. If minute is given, filter active facts."""
    if minute is not None:
        shifts = [s for s in shifts if covers(s, minute)]
        delegations = [d for d in delegations if covers(d, minute)]

    effective: set[tuple[str, str, str]] = set()
    queue: deque[tuple[str, str, str]] = deque()
    for shift in shifts:
        key = (shift["org"], shift["role"], shift["account"])
        if key not in effective:
            effective.add(key)
            queue.append(key)

    edges: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for delegation in delegations:
        edges[(delegation["org"], delegation["role"], delegation["delegator"])].append(delegation)

    while queue:
        key = queue.popleft()
        for delegation in edges.get(key, []):
            nxt = (delegation["org"], delegation["role"], delegation["delegate"])
            if nxt not in effective:
                effective.add(nxt)
                queue.append(nxt)
    return effective


def holders_by_role(effective: set[tuple[str, str, str]], org: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    for item_org, role, account in effective:
        if item_org == org:
            result[role].append(account)
    return {role: sorted(accounts) for role, accounts in result.items()}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def source_record_map(world: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {record["id"]: record for collection in ("accounts", "shifts", "delegations") for record in world[collection]}


def verify_path(case: dict[str, Any], path: dict[str, Any], active_shifts: dict[str, dict[str, Any]], active_delegations: dict[str, dict[str, Any]], minute: datetime, errors: list[str]) -> None:
    case_id = case["case_id"]
    accounts = path.get("accounts", [])
    edge_ids = path.get("delegation_ids", [])
    if len(accounts) != len(edge_ids) + 1:
        errors.append(f"{case_id}: malformed path length {path}")
        return
    shift = active_shifts.get(path.get("shift_id"))
    if not shift:
        errors.append(f"{case_id}: path shift {path.get('shift_id')} is not active/embedded")
        return
    if shift["account"] != accounts[0] or shift["org"] != case["org"]:
        errors.append(f"{case_id}: path does not start at its shift seed")
    if not covers(shift, minute):
        errors.append(f"{case_id}: path shift outside half-open interval")

    for index, edge_id in enumerate(edge_ids):
        delegation = active_delegations.get(edge_id)
        if not delegation:
            errors.append(f"{case_id}: delegation {edge_id} not active/embedded")
            continue
        expected = (case["org"], delegation["role"], accounts[index])
        actual_source = (delegation["org"], delegation["role"], delegation["delegator"])
        if expected != actual_source:
            errors.append(f"{case_id}: edge {edge_id} source mismatch")
        if delegation["delegate"] != accounts[index + 1]:
            errors.append(f"{case_id}: edge {edge_id} target mismatch")
        if not covers(delegation, minute):
            errors.append(f"{case_id}: edge {edge_id} outside half-open interval")


def verify_decision(case: dict[str, Any], world: dict[str, Any] | None, errors: list[str]) -> None:
    case_id = case["case_id"]
    graph_evidence = case["minute_graph"]
    minute = parse_ts(graph_evidence["minute"])
    org = case["org"]

    embedded_shifts = graph_evidence["active_shifts"]
    embedded_delegations = graph_evidence["active_delegations"]
    active_shifts = {s["id"]: s for s in embedded_shifts}
    active_delegations = {d["id"]: d for d in embedded_delegations}

    for record in embedded_shifts + embedded_delegations:
        if not covers(record, minute):
            errors.append(f"{case_id}: embedded fact {record['id']} not active at evidence minute")

    effective = closure(embedded_shifts, embedded_delegations)
    expected_keys = set(graph_evidence["effective_keys"])
    actual_keys = {"|".join(key) for key in effective}
    if actual_keys != expected_keys:
        errors.append(f"{case_id}: embedded closure mismatch: missing={sorted(actual_keys - expected_keys)} extra={sorted(expected_keys - actual_keys)}")

    # If source input is present, do not trust the embedded active subset alone.
    if world is not None:
        source_effective = closure(world["shifts"], world["delegations"], minute)
        source_org_keys = {"|".join(k) for k in source_effective if k[0] == org}
        embedded_org_keys = {k for k in actual_keys if k.startswith(org + "|")}
        if source_org_keys != embedded_org_keys:
            errors.append(f"{case_id}: source recomputation differs from embedded minute graph")

    claimed = {
        "intake": case["claim"]["intake_actor"],
        "approval": case["claim"]["approval_actor"],
    }
    recomputed_reasons: list[str] = []
    for role in ROLES:
        holder_accounts = holders_by_role(effective, org).get(role, [])
        evidence_role = case["role_evidence"][role]
        if evidence_role["effective_accounts"] != holder_accounts:
            errors.append(f"{case_id}: {role} holder list mismatch")
        actor_key = (org, role, claimed[role])
        actor_effective = actor_key in effective
        if evidence_role["actor_effective"] is not actor_effective:
            errors.append(f"{case_id}: {role} actor_effective mismatch")
        if not actor_effective:
            code = "CLAIMED_ACTOR_NOT_EFFECTIVE" if holder_accounts else "NO_EFFECTIVE_HOLDER"
            recomputed_reasons.append(f"{role.upper()}_{code}")
        for path in evidence_role.get("all_simple_proof_paths", []):
            verify_path(case, path, active_shifts, active_delegations, minute, errors)

    expected_verdict = "APPROVED" if not recomputed_reasons else "REJECTED"
    if case["verdict"] != expected_verdict:
        errors.append(f"{case_id}: verdict {case['verdict']}, independent closure says {expected_verdict}")
    if case["reasons"] != recomputed_reasons:
        errors.append(f"{case_id}: reasons {case['reasons']} != {recomputed_reasons}")
    if not case.get("matches_expected", False):
        errors.append(f"{case_id}: checker did not meet its declared expected outcome")

    action_check = case.get("action_time_check")
    if action_check and world is not None:
        acted_minute = parse_ts(action_check["acted_minute_utc"])
        acted_effective = closure(world["shifts"], world["delegations"], acted_minute)
        acted_ok = all((org, role, claimed[role]) in acted_effective for role in ROLES)
        if action_check["both_roles_effective_when_acted"] is not acted_ok:
            errors.append(f"{case_id}: action-time effectiveness mismatch")
        alternative = "APPROVED" if acted_ok else "REJECTED"
        if action_check["event_time_anchor_alternative_verdict"] != alternative:
            errors.append(f"{case_id}: action-time alternative verdict mismatch")


def verify_endpoint_and_occupancy(evidence: dict[str, Any], world: dict[str, Any], errors: list[str]) -> None:
    records = source_record_map(world)
    for check in evidence["endpoint_checks"]:
        record = records.get(check["record_id"])
        if not record:
            errors.append(f"endpoint: missing source record {check['record_id']}")
            continue
        minute = parse_ts(check["minute_utc"])
        actual = covers(record, minute)
        if actual != check["actual_covered"] or check["passed"] is not (actual == check["expected_covered"]):
            errors.append(f"endpoint check {check['check_id']} failed independent recomputation")

    for check in evidence["occupancy_checks"]:
        minute = parse_ts(check["minute"])
        effective = closure(world["shifts"], world["delegations"], minute)
        actual = (check["org"], check["role"], check["account"]) in effective
        if actual != check["actual_effective"] or check["passed"] is not (actual == check["expected_effective"]):
            errors.append(f"occupancy check {check['id']} failed independent recomputation")


def verify_cycles(evidence: dict[str, Any], world: dict[str, Any], errors: list[str]) -> None:
    for report in evidence["cycle_checks"]:
        minute = parse_ts(report["minute_utc"])
        effective = closure(world["shifts"], world["delegations"], minute)
        accounts = sorted(a for o, r, a in effective if o == report["org"] and r == report["role"])
        if accounts != report["effective_accounts"]:
            errors.append(f"cycle {report['check_id']}: effective accounts mismatch")
        seat = bool(accounts)
        if seat is not report["seat_produced"] or report["passed"] is not (seat == report["expected_rooted_cycle"]):
            errors.append(f"cycle {report['check_id']}: seat/rooted expectation mismatch")


def full_state(world: dict[str, Any], minutes: list[datetime]) -> tuple[dict[str, dict[str, list[str]]], int]:
    orgs = sorted({r["org"] for c in ("accounts", "shifts", "delegations") for r in world[c] if "org" in r})
    state: dict[str, dict[str, list[str]]] = {}
    total = 0
    for org in orgs:
        org_state: dict[str, list[str]] = {}
        for minute in minutes:
            effective = closure(world["shifts"], world["delegations"], minute)
            keys = sorted(f"{role}:{account}" for o, role, account in effective if o == org)
            org_state[iso(minute)] = keys
            total += len(keys)
        state[org] = org_state
    return state, total


def verify_incremental(evidence: dict[str, Any], world: dict[str, Any], errors: list[str]) -> None:
    inc = evidence["incremental"]
    if not inc.get("passed"):
        errors.append("incremental evidence reports passed=false")
    if not inc["counts_equal"] or not inc["exact_minute_holder_state_equal"] or not inc["state_hashes_equal"]:
        errors.append("incremental equality flags are false")

    patched = json.loads(json.dumps(world))
    patch = inc["patch_shift"]
    patched["shifts"] = [s for s in patched["shifts"] if s["id"] != patch["id"]]
    patched["shifts"].append(patch)

    minutes = minute_span(patched)
    state, total = full_state(patched, minutes)
    if total != inc["full_recomputed_count_after_patch"]:
        errors.append(f"incremental: independent full count {total} != evidence {inc['full_recomputed_count_after_patch']}")
    if total != inc["actual_count_after_incremental_patch"]:
        errors.append("incremental: maintained incremental count differs from independent full count")
    state_hash = sha256_json(state)
    if state_hash != inc["full_state_sha256"]:
        errors.append("incremental: independent full state hash differs")
    if state_hash != inc["incremental_state_sha256"]:
        errors.append("incremental: incremental state hash differs from independently recomputed hash")

    downstream: dict[tuple[str, str, str], set[tuple[str, str, str]]] = defaultdict(set)
    for d in world["delegations"]:
        downstream[(d["org"], d["role"], d["delegator"])].add((d["org"], d["role"], d["delegate"]))
    start = (patch["org"], patch["role"], patch["account"])
    seen = {start}
    queue = deque([start])
    while queue:
        cur = queue.popleft()
        for nxt in downstream.get(cur, set()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    affected_orgs = sorted({k[0] for k in seen})
    if affected_orgs != inc["affected_orgs"] or affected_orgs != inc["post_patch_recomputed_orgs"]:
        errors.append("incremental: independently derived affected orgs do not match evidence")
    if inc["post_patch_recomputed_orgs"] != ["org.increment"]:
        errors.append("incremental: expected only org.increment to be recomputed after the one change")


def main(argv: list[str]) -> int:
    evidence_path = Path(argv[1]) if len(argv) > 1 else ROOT / "evidence" / "evidence.json"
    if not evidence_path.is_absolute():
        evidence_path = (Path.cwd() / evidence_path).resolve()
    evidence = load_json(evidence_path)
    errors: list[str] = []

    input_path = ROOT / evidence["input_file"]
    world: dict[str, Any] | None = None
    if input_path.exists():
        if sha256_file(input_path) != evidence["input_sha256"]:
            errors.append("input SHA-256 mismatch")
        world = load_json(input_path)
    else:
        errors.append(f"source input not found for full verification: {input_path}")

    csv_path = evidence_path.parent / evidence["minute_enumeration"]["csv_file"]
    if csv_path.exists():
        if sha256_file(csv_path) != evidence["minute_enumeration"]["csv_sha256"]:
            errors.append("minute CSV SHA-256 mismatch")
        line_count = sum(1 for _ in csv_path.open("r", encoding="utf-8")) - 1
        if line_count != evidence["minute_enumeration"]["fill_count"]:
            errors.append(f"minute CSV rows {line_count} != fill_count")
    else:
        errors.append(f"minute CSV not found: {csv_path}")

    for case in evidence["decisions"]:
        verify_decision(case, world, errors)

    if world is not None:
        verify_endpoint_and_occupancy(evidence, world, errors)
        verify_cycles(evidence, world, errors)
        verify_incremental(evidence, world, errors)

    if errors:
        print("EVIDENCE INVALID")
        for error in errors:
            print(f"  - {error}")
        return 1

    print(f"VERIFIED: {evidence_path}")
    print(f"  decisions={len(evidence['decisions'])} endpoints={len(evidence['endpoint_checks'])} fills={evidence['minute_enumeration']['fill_count']}")
    print("  embedded closures, proof paths, source recomputation, CSV hash, and incremental counts all match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
