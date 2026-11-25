#!/usr/bin/env python3
"""Offline duty-delegation checker for the fictional 16682 scenario.

Only the JSON file in data/synthetic_duty.json is authorization input.  It
contains fictional accounts, UTC shift intervals, and delegation events.
Approval claims below are untrusted probes: they state who allegedly acted and
when, and this module recomputes whether the claimed seats were effective.

Run on native Windows with Python 3.12 standard library only:

    py -3.12 duty_audit.py
    python duty_audit.py --input data\\synthetic_duty.json --out evidence
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import sys
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "data" / "synthetic_duty.json"
DEFAULT_OUT = ROOT / "evidence"
MINUTE = timedelta(minutes=1)
ROLES = ("intake", "approval")

# This is the one handover change used by the incremental proof.  It is a new
# original shift seed for org.increment; the night delegation already exists.
PATCH_SHIFT: dict[str, Any] = {
    "id": "shift.increment.approval.night-root-PATCH",
    "account": "acct.inc.night-root",
    "org": "org.increment",
    "role": "approval",
    "start": "2026-11-27T01:00:00Z",
    "end": "2026-11-27T02:00:00Z",
}

# Claims are verification probes, not additional authorization facts.
PROBES: list[dict[str, Any]] = [
    {
        "case_id": "C1_CROSSDAY_DUAL_DUTY_SHARED_CONTROLLER",
        "org": "org.harbor",
        "at": "2026-11-27T00:07:00Z",
        "intake_actor": "acct.harbor.lin",
        "approval_actor": "acct.harbor.shen",
        "expected": "APPROVED",
    },
    {
        "case_id": "C2_START_ENDPOINT_IS_INCLUSIVE",
        "org": "org.harbor",
        "at": "2026-11-26T23:30:00Z",
        "intake_actor": "acct.harbor.lin",
        "approval_actor": "acct.harbor.shen",
        "expected": "APPROVED",
    },
    {
        "case_id": "C3_END_ENDPOINT_IS_EXCLUSIVE",
        "org": "org.harbor",
        "at": "2026-11-27T00:30:00Z",
        "intake_actor": "acct.harbor.lin",
        "approval_actor": "acct.harbor.shen",
        "expected": "REJECTED",
    },
    {
        "case_id": "C4_SOURCELESS_RING_CANNOT_FILL_SEAT",
        "org": "org.ring",
        "at": "2026-11-27T00:10:00Z",
        "intake_actor": "acct.ring.b",
        "approval_actor": "acct.ring.c",
        "expected": "REJECTED",
    },
    {
        "case_id": "C5_ROOTED_MUTUAL_BATCH_APPROVE",
        "org": "org.mutual",
        "at": "2026-11-27T00:15:00Z",
        "intake_actor": "acct.mutual.root",
        "approval_actor": "acct.mutual.y",
        "expected": "APPROVED",
    },
    {
        "case_id": "C6_OLD_APPROVAL_LATE_EXACT_HANDOVER",
        "org": "org.late",
        "received_at": "2026-11-27T01:00:00Z",
        "acted_at": "2026-11-27T00:59:00Z",
        "intake_actor": "acct.late.intake",
        "approval_actor": "acct.late.old-approver",
        "expected": "REJECTED",
    },
    {
        "case_id": "C7_OLD_APPROVAL_LATE_AFTER_HANDOVER",
        "org": "org.late",
        "received_at": "2026-11-27T01:02:00Z",
        "acted_at": "2026-11-27T00:58:00Z",
        "intake_actor": "acct.late.intake",
        "approval_actor": "acct.late.old-approver",
        "expected": "REJECTED",
    },
    {
        "case_id": "C8_NEW_HANDOVER_APPROVER_CAN_APPROVE",
        "org": "org.late",
        "at": "2026-11-27T01:05:00Z",
        "intake_actor": "acct.late.intake",
        "approval_actor": "acct.late.new-approver",
        "expected": "APPROVED",
    },
]

TARGETED_OCCUPANCY_CHECKS: list[dict[str, Any]] = [
    {"id": "O1_HARBOR_INTAKE_BEFORE_START", "org": "org.harbor", "role": "intake", "account": "acct.harbor.lin", "minute": "2026-11-26T23:29:00Z", "expected_effective": False},
    {"id": "O2_HARBOR_INTAKE_START_INCLUDED", "org": "org.harbor", "role": "intake", "account": "acct.harbor.lin", "minute": "2026-11-26T23:30:00Z", "expected_effective": True},
    {"id": "O3_HARBOR_INTAKE_LAST_INSIDE_MINUTE", "org": "org.harbor", "role": "intake", "account": "acct.harbor.lin", "minute": "2026-11-27T00:29:00Z", "expected_effective": True},
    {"id": "O4_HARBOR_INTAKE_END_EXCLUDED", "org": "org.harbor", "role": "intake", "account": "acct.harbor.lin", "minute": "2026-11-27T00:30:00Z", "expected_effective": False},
    {"id": "O5_LATE_OLD_APPROVER_LAST_INSIDE_MINUTE", "org": "org.late", "role": "approval", "account": "acct.late.old-approver", "minute": "2026-11-27T00:59:00Z", "expected_effective": True},
    {"id": "O6_LATE_OLD_APPROVER_HANDOVER_EXCLUDED", "org": "org.late", "role": "approval", "account": "acct.late.old-approver", "minute": "2026-11-27T01:00:00Z", "expected_effective": False},
    {"id": "O7_INCREMENT_NIGHT_DELEGATION_DORMANT_WITHOUT_SHIFT", "org": "org.increment", "role": "approval", "account": "acct.inc.night-approver", "minute": "2026-11-27T01:00:00Z", "expected_effective": False},
]


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp, accepting the usual trailing Z."""
    if not isinstance(value, str):
        raise ValueError(f"timestamp must be a string, got {value!r}")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def covers_half_open(record: dict[str, Any], minute: datetime) -> bool:
    """True iff the minute label is in [start, end)."""
    start = parse_ts(record["start"])
    end = parse_ts(record["end"])
    return start <= minute < end


def input_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_world(path: Path) -> dict[str, Any]:
    world = json.loads(path.read_text(encoding="utf-8"))
    if set(world) - {"format", "accounts", "shifts", "delegations"}:
        raise ValueError("unexpected top-level authorization input fields")
    for collection in ("accounts", "shifts", "delegations"):
        if not isinstance(world.get(collection), list):
            raise ValueError(f"missing list: {collection}")

    ids: dict[str, str] = {}
    for collection in ("accounts", "shifts", "delegations"):
        for item in world[collection]:
            item_id = item.get("id")
            if not item_id:
                raise ValueError(f"{collection} record without id")
            if item_id in ids:
                raise ValueError(f"duplicate id {item_id!r} in {ids[item_id]} and {collection}")
            ids[item_id] = collection
            if "start" in item and "end" in item:
                start, end = parse_ts(item["start"]), parse_ts(item["end"])
                if start >= end:
                    raise ValueError(f"interval empty or reversed: {item_id}")
                if start.second or start.microsecond or end.second or end.microsecond:
                    raise ValueError(f"interval endpoints must be minute aligned: {item_id}")

    account_ids = {a["id"] for a in world["accounts"]}
    for shift in world["shifts"]:
        if shift["account"] not in account_ids:
            raise ValueError(f"shift {shift['id']} references unknown account")
    for delegation in world["delegations"]:
        if delegation["delegator"] not in account_ids or delegation["delegate"] not in account_ids:
            raise ValueError(f"delegation {delegation['id']} references unknown account")
    return world


def account_index(world: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {account["id"]: account for account in world["accounts"]}


def all_orgs(world: dict[str, Any]) -> list[str]:
    orgs = {a["org"] for a in world["accounts"]}
    orgs.update(s["org"] for s in world["shifts"])
    orgs.update(d["org"] for d in world["delegations"])
    return sorted(orgs)


def minute_span(world: dict[str, Any]) -> list[datetime]:
    starts: list[datetime] = []
    ends: list[datetime] = []
    for collection in ("shifts", "delegations"):
        for record in world[collection]:
            starts.append(parse_ts(record["start"]))
            ends.append(parse_ts(record["end"]))
    if not starts:
        return []
    first, last = min(starts), max(ends)
    if first.second or first.microsecond:
        raise ValueError("span start is not minute aligned")
    minutes: list[datetime] = []
    current = first
    while current < last:
        minutes.append(current)
        current += MINUTE
    return minutes


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def recompute_minute(world: dict[str, Any], minute: datetime, org: str | None = None) -> dict[str, Any]:
    """Least-fixed-point closure from active shifts through active delegations.

    Starting with every active original shift is what prevents a sourceless
    delegation cycle from creating a node.  Processing a whole batch as one
    edge set means mutually dependent edges need no insertion order.
    """
    shifts = sorted(
        (s for s in world["shifts"] if covers_half_open(s, minute) and (org is None or s["org"] == org)),
        key=lambda s: s["id"],
    )
    delegations = sorted(
        (d for d in world["delegations"] if covers_half_open(d, minute) and (org is None or d["org"] == org)),
        key=lambda d: d["id"],
    )

    proofs: dict[tuple[str, str, str], dict[str, Any]] = {}
    for shift in shifts:
        key = (shift["org"], shift["role"], shift["account"])
        proof = {
            "shift_id": shift["id"],
            "org": shift["org"],
            "role": shift["role"],
            "accounts": [shift["account"]],
            "delegation_ids": [],
            "batch_ids": [],
        }
        # Deterministic handling if synthetic data ever has duplicate seeds.
        old = proofs.get(key)
        if old is None or proof["shift_id"] < old["shift_id"]:
            proofs[key] = proof

    adjacency: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for delegation in delegations:
        source = (delegation["org"], delegation["role"], delegation["delegator"])
        adjacency[source].append(delegation)

    queue = deque(proofs.keys())
    while queue:
        key = queue.popleft()
        parent = proofs[key]
        for delegation in adjacency.get(key, []):
            target = (delegation["org"], delegation["role"], delegation["delegate"])
            if target in proofs:
                continue
            proofs[target] = {
                "shift_id": parent["shift_id"],
                "org": delegation["org"],
                "role": delegation["role"],
                "accounts": [*parent["accounts"], delegation["delegate"]],
                "delegation_ids": [*parent["delegation_ids"], delegation["id"]],
                "batch_ids": _dedupe([*parent["batch_ids"], delegation.get("batch", "")]),
            }
            queue.append(target)

    holders: dict[tuple[str, str], list[str]] = defaultdict(list)
    for item_org, role, account in proofs:
        holders[(item_org, role)].append(account)
    for key in holders:
        holders[key] = sorted(holders[key])

    return {
        "minute": minute,
        "org_filter": org,
        "shifts": shifts,
        "delegations": delegations,
        "proofs": proofs,
        "holders": dict(holders),
        "adjacency": dict(adjacency),
    }


def key_string(key: tuple[str, str, str]) -> str:
    return "|".join(key)


def serialize_graph(graph: dict[str, Any]) -> dict[str, Any]:
    proofs = {key_string(k): v for k, v in sorted(graph["proofs"].items())}
    holders = {f"{org}|{role}": accounts for (org, role), accounts in sorted(graph["holders"].items())}
    return {
        "minute": iso(graph["minute"]),
        "org_filter": graph["org_filter"],
        "active_shifts": graph["shifts"],
        "active_delegations": graph["delegations"],
        "effective_keys": sorted(key_string(k) for k in graph["proofs"]),
        "holders": holders,
        "proofs": proofs,
    }


def enumerate_proof_paths(graph: dict[str, Any], org: str, role: str, actor: str) -> list[dict[str, Any]]:
    """Enumerate simple root-to-actor paths; cycles are not unfolded forever."""
    target = (org, role, actor)
    if target not in graph["proofs"]:
        return []

    roots = [key for key, proof in graph["proofs"].items() if not proof["delegation_ids"] and key[0] == org and key[1] == role]
    results: list[dict[str, Any]] = []
    adjacency = graph["adjacency"]

    if target in roots:
        results.append(
            {
                "shift_id": graph["proofs"][target]["shift_id"],
                "accounts": [actor],
                "delegation_ids": [],
                "length": 0,
            }
        )

    def walk(
        current: tuple[str, str, str],
        nodes: set[tuple[str, str, str]],
        accounts: list[str],
        edge_ids: list[str],
        shift_id: str,
    ) -> None:
        for delegation in adjacency.get(current, []):
            nxt = (delegation["org"], delegation["role"], delegation["delegate"])
            if nxt in nodes:
                continue
            next_accounts = [*accounts, delegation["delegate"]]
            next_edges = [*edge_ids, delegation["id"]]
            if nxt == target:
                results.append(
                    {
                        "shift_id": shift_id,
                        "accounts": next_accounts,
                        "delegation_ids": next_edges,
                        "length": len(next_edges),
                    }
                )
            else:
                walk(nxt, nodes | {nxt}, next_accounts, next_edges, shift_id)

    for root in roots:
        if root == target:
            continue
        walk(root, {root}, [root[2]], [], graph["proofs"][root]["shift_id"])

    deduped: dict[tuple[str, ...], dict[str, Any]] = {}
    for path in results:
        deduped[tuple(path["delegation_ids"])] = path
    return sorted(deduped.values(), key=lambda p: (p["length"], p["delegation_ids"]))


def role_evidence(
    world: dict[str, Any],
    accounts: dict[str, dict[str, Any]],
    graph: dict[str, Any],
    org: str,
    role: str,
    actor: str,
) -> dict[str, Any]:
    key = (org, role, actor)
    paths = enumerate_proof_paths(graph, org, role, actor)
    root_accounts = sorted({path["accounts"][0] for path in paths if path["accounts"]})
    controllers = sorted({accounts.get(root, {}).get("controller", root) for root in root_accounts})
    return {
        "claimed_actor": actor,
        "actor_effective": key in graph["proofs"],
        "effective_accounts": graph["holders"].get((org, role), []),
        "shortest_proof": graph["proofs"].get(key),
        "all_simple_proof_paths": paths,
        "root_accounts": root_accounts,
        "actual_controllers": controllers,
    }


def evaluate_claim(world: dict[str, Any], accounts: dict[str, dict[str, Any]], claim: dict[str, Any]) -> dict[str, Any]:
    anchor_text = claim.get("received_at") or claim.get("at")
    if not anchor_text:
        raise ValueError(f"claim {claim.get('case_id')} has no at/received_at")
    anchor = parse_ts(anchor_text)
    org = claim["org"]
    graph = recompute_minute(world, anchor, org)

    roles = {
        "intake": role_evidence(world, accounts, graph, org, "intake", claim["intake_actor"]),
        "approval": role_evidence(world, accounts, graph, org, "approval", claim["approval_actor"]),
    }
    reasons: list[str] = []
    for role in ROLES:
        evidence = roles[role]
        if not evidence["actor_effective"]:
            if evidence["effective_accounts"]:
                reasons.append(f"{role.upper()}_CLAIMED_ACTOR_NOT_EFFECTIVE")
            else:
                reasons.append(f"{role.upper()}_NO_EFFECTIVE_HOLDER")

    approved = not reasons
    verdict = "APPROVED" if approved else "REJECTED"
    controller_sets = [set(roles[r]["actual_controllers"]) for r in ROLES]
    shared_controllers = sorted(set.intersection(*controller_sets)) if all(controller_sets) else []

    result: dict[str, Any] = {
        "case_id": claim["case_id"],
        "org": org,
        "verdict": verdict,
        "expected_verdict": claim.get("expected"),
        "matches_expected": verdict == claim.get("expected"),
        "anchor": "received_at" if claim.get("received_at") else "at",
        "decision_minute_utc": iso(anchor),
        "claim": claim,
        "reasons": reasons,
        "distinct_acting_accounts": claim["intake_actor"] != claim["approval_actor"],
        "shared_actual_controllers": shared_controllers,
        "shared_controller_is_allowed_and_disclosed": bool(shared_controllers),
        "role_evidence": roles,
        "minute_graph": serialize_graph(graph),
        "policy": (
            "At the UTC minute when the claim is received by the reference "
            "monitor, each claimed actor must be reachable from an active "
            "shift through active delegations. Intervals are [start, end). "
            "A shared root/controller is evidence of convergence, not a veto."
        ),
    }

    if claim.get("acted_at"):
        acted = parse_ts(claim["acted_at"])
        acted_graph = recompute_minute(world, acted, org)
        acted_roles = {
            role: role_evidence(world, accounts, acted_graph, org, role, claim[f"{role}_actor"])
            for role in ROLES
        }
        acted_both = all(item["actor_effective"] for item in acted_roles.values())
        result["action_time_check"] = {
            "acted_minute_utc": iso(acted),
            "both_roles_effective_when_acted": acted_both,
            "event_time_anchor_alternative_verdict": "APPROVED" if acted_both else "REJECTED",
            "note": (
                "The default verdict uses receipt/reference-monitor time. "
                "This block independently shows what an event-time policy "
                "would conclude from the same input."
            ),
            "role_effective_at_action": {role: acted_roles[role]["actor_effective"] for role in ROLES},
        }
    return result


def endpoint_checks(world: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for collection in ("shifts", "delegations"):
        for record in world[collection]:
            start = parse_ts(record["start"])
            end = parse_ts(record["end"])
            for boundary, minute, expected in (("start_inclusive", start, True), ("end_exclusive", end, False)):
                actual = covers_half_open(record, minute)
                checks.append(
                    {
                        "check_id": f"{record['id']}:{boundary}",
                        "collection": collection,
                        "record_id": record["id"],
                        "boundary": boundary,
                        "minute_utc": iso(minute),
                        "expected_covered": expected,
                        "actual_covered": actual,
                        "passed": actual is expected,
                    }
                )
    return checks


def targeted_occupancy_checks(world: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for check in TARGETED_OCCUPANCY_CHECKS:
        minute = parse_ts(check["minute"])
        graph = recompute_minute(world, minute, check["org"])
        key = (check["org"], check["role"], check["account"])
        actual = key in graph["proofs"]
        item = dict(check)
        item["actual_effective"] = actual
        item["passed"] = actual is check["expected_effective"]
        item["minute_utc"] = iso(minute)
        results.append(item)
    return results


def strongly_connected_components(nodes: set[tuple[str, str, str]], adjacency: dict[tuple[str, str, str], list[dict[str, Any]]]) -> list[list[tuple[str, str, str]]]:
    """Iterative-safe Tarjan SCC; synthetic graphs are tiny and recursive is fine."""
    index = 0
    indices: dict[tuple[str, str, str], int] = {}
    low: dict[tuple[str, str, str], int] = {}
    stack: list[tuple[str, str, str]] = []
    on_stack: set[tuple[str, str, str]] = set()
    components: list[list[tuple[str, str, str]]] = []

    def connect(v: tuple[str, str, str]) -> None:
        nonlocal index
        indices[v] = low[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)
        for delegation in adjacency.get(v, []):
            w = (delegation["org"], delegation["role"], delegation["delegate"])
            if w not in indices:
                connect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], indices[w])
        if low[v] == indices[v]:
            component: list[tuple[str, str, str]] = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                component.append(w)
                if w == v:
                    break
            components.append(sorted(component))

    for node in sorted(nodes):
        if node not in indices:
            connect(node)
    return components


def cycle_reports(world: dict[str, Any]) -> list[dict[str, Any]]:
    specs = [
        ("CYCLE1_SOURCELESS_RING", "org.ring", "intake", "2026-11-27T00:10:00Z", False),
        ("CYCLE2_ROOTED_MUTUAL_BATCH", "org.mutual", "approval", "2026-11-27T00:15:00Z", True),
    ]
    reports: list[dict[str, Any]] = []
    for check_id, org, role, minute_text, expected_rooted in specs:
        minute = parse_ts(minute_text)
        graph = recompute_minute(world, minute, org)
        delegations = [d for d in graph["delegations"] if d["role"] == role]
        nodes: set[tuple[str, str, str]] = set()
        adjacency: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for d in delegations:
            source = (org, role, d["delegator"])
            target = (org, role, d["delegate"])
            nodes.update((source, target))
            adjacency[source].append(d)
        components = strongly_connected_components(nodes, adjacency)
        cyclic_components: list[list[tuple[str, str, str]]] = []
        for component in components:
            if len(component) > 1:
                cyclic_components.append(component)
            elif component:
                node = component[0]
                if any((d["org"], d["role"], d["delegate"]) == node for d in adjacency.get(node, [])):
                    cyclic_components.append(component)
        scc_accounts = [[node[2] for node in component] for component in cyclic_components]
        # An SCC is authorized when the least fixed point reaches it from an
        # original shift.  The seed may be outside the mutual SCC (root -> X).
        rooted = any(any(node in graph["proofs"] for node in component) for component in cyclic_components)
        effective_accounts = graph["holders"].get((org, role), [])
        reports.append(
            {
                "check_id": check_id,
                "minute_utc": iso(minute),
                "org": org,
                "role": role,
                "cyclic_components_accounts": scc_accounts,
                "cycle_reachable_from_original_shift": rooted,
                "expected_rooted_cycle": expected_rooted,
                "effective_accounts": effective_accounts,
                "seat_produced": bool(effective_accounts),
                "passed": rooted is expected_rooted and (bool(effective_accounts) is expected_rooted),
                "rule": "Least fixed point starts from active shifts only; an unrooted SCC stays outside the closure.",
            }
        )
    return reports


def build_downstream_index(world: dict[str, Any]) -> dict[tuple[str, str, str], set[tuple[str, str, str]]]:
    downstream: dict[tuple[str, str, str], set[tuple[str, str, str]]] = defaultdict(set)
    for delegation in world["delegations"]:
        source = (delegation["org"], delegation["role"], delegation["delegator"])
        target = (delegation["org"], delegation["role"], delegation["delegate"])
        downstream[source].add(target)
    return downstream


def affected_closure(change: dict[str, Any], downstream: dict[tuple[str, str, str], set[tuple[str, str, str]]]) -> set[tuple[str, str, str]]:
    start = (change["org"], change["role"], change["account"])
    seen = {start}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for nxt in downstream.get(current, set()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def org_minute_state(world: dict[str, Any], org: str, minutes: list[datetime]) -> tuple[dict[str, list[str]], int]:
    state: dict[str, list[str]] = {}
    count = 0
    for minute in minutes:
        graph = recompute_minute(world, minute, org)
        keys = sorted(f"{role}:{account}" for item_org, role, account in graph["proofs"] if item_org == org)
        state[iso(minute)] = keys
        count += len(keys)
    return state, count


def hash_state(state: dict[str, Any]) -> str:
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class IncrementalDutyCounter:
    """Cache per-org minute states and update one changed org closure."""

    def __init__(self, world: dict[str, Any], minutes: list[datetime]):
        self.world = copy.deepcopy(world)
        self.minutes = minutes
        self.orgs = all_orgs(self.world)
        self.states: dict[str, dict[str, list[str]]] = {}
        self.counts: dict[str, int] = {}
        self.total_count = 0
        self.baseline_recomputed_orgs: set[str] = set()
        self.last_patch_recomputed_orgs: set[str] = set()
        for org in self.orgs:
            self._recompute_org(org, initial=True)

    def _recompute_org(self, org: str, initial: bool = False) -> None:
        state, count = org_minute_state(self.world, org, self.minutes)
        self.states[org] = state
        self.counts[org] = count
        if initial:
            self.baseline_recomputed_orgs.add(org)
        else:
            self.last_patch_recomputed_orgs.add(org)

    def apply_shift_change(self, shift: dict[str, Any]) -> dict[str, Any]:
        downstream = build_downstream_index(self.world)
        relation_keys = affected_closure(shift, downstream)
        affected_orgs = sorted({key[0] for key in relation_keys})
        self.last_patch_recomputed_orgs = set()

        shifts = self.world["shifts"]
        shifts[:] = [s for s in shifts if s["id"] != shift["id"]]
        shifts.append(copy.deepcopy(shift))

        previous_org_counts = {org: self.counts[org] for org in affected_orgs}
        for org in affected_orgs:
            old_count = self.counts[org]
            self._recompute_org(org)
            self.total_count += self.counts[org] - old_count

        return {
            "affected_relation_keys": sorted(key_string(k) for k in relation_keys),
            "affected_orgs": affected_orgs,
            "previous_org_counts": previous_org_counts,
            "new_org_counts": {org: self.counts[org] for org in affected_orgs},
        }

    def initialize_total(self) -> None:
        self.total_count = sum(self.counts.values())


def incremental_proof(world: dict[str, Any], minutes: list[datetime]) -> dict[str, Any]:
    counter = IncrementalDutyCounter(world, minutes)
    counter.initialize_total()
    baseline_total = counter.total_count

    patch_details = counter.apply_shift_change(PATCH_SHIFT)
    incremental_total = counter.total_count

    patched_world = counter.world
    full_states: dict[str, dict[str, list[str]]] = {}
    full_total = 0
    for org in all_orgs(patched_world):
        state, count = org_minute_state(patched_world, org, minutes)
        full_states[org] = state
        full_total += count

    incremental_hash = hash_state(counter.states)
    full_hash = hash_state(full_states)
    result = {
        "change_description": "Add one original approval shift seed at org.increment for the night handover.",
        "patch_shift": PATCH_SHIFT,
        **patch_details,
        "post_patch_recomputed_orgs": sorted(counter.last_patch_recomputed_orgs),
        "unaffected_orgs_not_recomputed": sorted(set(counter.orgs) - counter.last_patch_recomputed_orgs),
        "org_count": len(counter.orgs),
        "recomputed_org_count": len(counter.last_patch_recomputed_orgs),
        "baseline_total_count": baseline_total,
        "actual_count_after_incremental_patch": incremental_total,
        "full_recomputed_count_after_patch": full_total,
        "actual_added_fills": incremental_total - baseline_total,
        "counts_equal": incremental_total == full_total,
        "exact_minute_holder_state_equal": counter.states == full_states,
        "incremental_state_sha256": incremental_hash,
        "full_state_sha256": full_hash,
        "state_hashes_equal": incremental_hash == full_hash,
    }
    result["passed"] = (
        result["counts_equal"]
        and result["exact_minute_holder_state_equal"]
        and result["state_hashes_equal"]
        and result["post_patch_recomputed_orgs"] == ["org.increment"]
        and result["actual_added_fills"] == 120
    )
    return result


def write_minute_csv(world: dict[str, Any], minutes: list[datetime], accounts: dict[str, dict[str, Any]], path: Path) -> int:
    row_count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["minute_utc", "org", "role", "effective_account", "root_account", "registered_controller", "delegation_depth", "batches"])
        for minute in minutes:
            graph = recompute_minute(world, minute)
            for key in sorted(graph["proofs"]):
                org, role, account = key
                proof = graph["proofs"][key]
                root_account = proof["accounts"][0]
                controller = accounts.get(root_account, {}).get("controller", "")
                writer.writerow(
                    [
                        iso(minute),
                        org,
                        role,
                        account,
                        root_account,
                        controller,
                        len(proof["delegation_ids"]),
                        ";".join(b for b in proof["batch_ids"] if b),
                    ]
                )
                row_count += 1
    return row_count


def build_report(evidence: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# 离线核对证据报告")
    lines.append("")
    lines.append("所有时间均为 UTC；区间按半开 `[start, end)` 计算。授权输入仅含虚构账号、当班区间和委托事件。")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    me = evidence["minute_enumeration"]
    lines.append(f"- 枚举分钟数：{me['minute_count']}（{me['start_utc']} 至 {me['end_exclusive_utc']}，右端点不枚举）")
    lines.append(f"- 有效角色填充行数：{me['fill_count']}，见 `evidence/minute_roles.csv`")
    lines.append(f"- 输入 SHA-256：`{evidence['input_sha256']}`")
    lines.append(f"- 端点检查：{sum(c['passed'] for c in evidence['endpoint_checks'])}/{len(evidence['endpoint_checks'])} 通过")
    lines.append(f"- 席位端点检查：{sum(c['passed'] for c in evidence['occupancy_checks'])}/{len(evidence['occupancy_checks'])} 通过")
    lines.append("")
    lines.append("## 批准/拒绝结论")
    lines.append("")
    lines.append("| 用例 | 结论 | 预期 | 原因 | 共享实际控制人 |")
    lines.append("|---|---|---|---|---|")
    for case in evidence["decisions"]:
        controllers = ", ".join(case["shared_actual_controllers"]) or "—"
        reasons = ", ".join(case["reasons"]) or "—"
        lines.append(f"| `{case['case_id']}` | **{case['verdict']}** | {case['expected_verdict']} | {reasons} | {controllers} |")
    lines.append("")
    lines.append("## 关键覆盖")
    lines.append("")
    c1 = next(c for c in evidence["decisions"] if c["case_id"] == "C1_CROSSDAY_DUAL_DUTY_SHARED_CONTROLLER")
    lines.append("- 跨日批准：`2026-11-27T00:07Z` 仍在跨日班次内；受理人为 "
                  f"`{c1['role_evidence']['intake']['claimed_actor']}`，批准人为 "
                  f"`{c1['role_evidence']['approval']['claimed_actor']}`。")
    approval_paths = c1["role_evidence"]["approval"]["all_simple_proof_paths"]
    lines.append(f"- 批准席位存在 {len(approval_paths)} 条简单路径，均收敛到根账号 "
                 f"{', '.join(c1['role_evidence']['approval']['root_accounts'])}，实际控制人 "
                 f"{', '.join(c1['role_evidence']['approval']['actual_controllers'])}。")
    lines.append("- 半开端点：开始分钟包含，结束分钟排除；C2/C3 与 O1–O7 可重算。")
    lines.append("- 无原始授权环：`org.ring` 的 A→B→C→A 不进入最小不动点，不产生席位。")
    lines.append("- 同批互相依赖：`org.mutual` 的 X↔Y 与根授权同批求值；根→X 后，X、Y 均获得席位。")
    late = next(c for c in evidence["decisions"] if c["case_id"] == "C7_OLD_APPROVAL_LATE_AFTER_HANDOVER")
    lines.append("- 迟到旧审批：默认按审批到达引用监控器的时间判定；证据同时重算行为时间锚点。")
    lines.append(f"  - 行为时间策略会得到 **{late['action_time_check']['event_time_anchor_alternative_verdict']}**；"
                 f"默认收件/引用监控时间策略得到 **{late['verdict']}**。")
    inc = evidence["incremental"]
    lines.append("")
    lines.append("## 增量重算一致性")
    lines.append("")
    lines.append(f"- 单次换班影响关系键：{', '.join(f'`{k}`' for k in inc['affected_relation_keys'])}")
    lines.append(f"- 换班后实际重算组织：{', '.join(f'`{o}`' for o in inc['post_patch_recomputed_orgs'])}")
    lines.append(f"- 未重算组织（{len(inc['unaffected_orgs_not_recomputed'])} 个）：{', '.join(f'`{o}`' for o in inc['unaffected_orgs_not_recomputed'])}")
    lines.append(f"- 增量维护计数：{inc['actual_count_after_incremental_patch']}；全量重算计数：{inc['full_recomputed_count_after_patch']}；一致：{inc['counts_equal']}")
    lines.append(f"- 逐分钟席位状态完全一致：{inc['exact_minute_holder_state_equal']}；状态哈希一致：{inc['state_hashes_equal']}")
    lines.append("")
    lines.append("## 独立重算")
    lines.append("")
    lines.append("```bat")
    lines.append("py -3.12 duty_audit.py")
    lines.append("py -3.12 verify_evidence.py evidence\\evidence.json")
    lines.append("py -3.12 -m unittest discover -s tests -v")
    lines.append("```")
    lines.append("")
    lines.append("`verify_evidence.py` 不导入本模块；它从证据中嵌入的当时有效事实独立执行最小闭包，并从原始输入重算增量计数。")
    return "\n".join(lines) + "\n"


def assert_evidence(evidence: dict[str, Any]) -> None:
    failures: list[str] = []
    for check in evidence["endpoint_checks"]:
        if not check["passed"]:
            failures.append(f"endpoint check failed: {check['check_id']}")
    for check in evidence["occupancy_checks"]:
        if not check["passed"]:
            failures.append(f"occupancy check failed: {check['id']}")
    for report in evidence["cycle_checks"]:
        if not report["passed"]:
            failures.append(f"cycle check failed: {report['check_id']}")
    for case in evidence["decisions"]:
        if not case["matches_expected"]:
            failures.append(f"case {case['case_id']} got {case['verdict']}, expected {case['expected_verdict']}")
    if not evidence["incremental"]["passed"]:
        failures.append("incremental recomputation proof failed")
    if failures:
        raise SystemExit("SELF-CHECK FAILED:\n- " + "\n- ".join(failures))


def run(input_path: Path = DEFAULT_INPUT, out_dir: Path = DEFAULT_OUT) -> dict[str, Any]:
    world = load_world(input_path)
    accounts = account_index(world)
    minutes = minute_span(world)
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / "minute_roles.csv"
    fill_count = write_minute_csv(world, minutes, accounts, csv_path)

    decisions = [evaluate_claim(world, accounts, probe) for probe in PROBES]
    endpoints = endpoint_checks(world)
    occupancy = targeted_occupancy_checks(world)
    cycles = cycle_reports(world)
    incremental = incremental_proof(world, minutes)

    evidence = {
        "schema": "duty-audit-evidence/v1",
        "input_file": input_path.relative_to(ROOT).as_posix() if input_path.is_relative_to(ROOT) else str(input_path),
        "input_sha256": input_sha256(input_path),
        "python_version": sys.version.split()[0],
        "timezone_rule": "All timestamps are UTC; minute label t represents [t, t+60s).",
        "minute_enumeration": {
            "start_utc": iso(minutes[0]) if minutes else None,
            "end_exclusive_utc": iso(minutes[-1] + MINUTE) if minutes else None,
            "minute_count": len(minutes),
            "fill_count": fill_count,
            "csv_file": "minute_roles.csv",
            "csv_sha256": input_sha256(csv_path),
        },
        "endpoint_checks": endpoints,
        "occupancy_checks": occupancy,
        "cycle_checks": cycles,
        "decisions": decisions,
        "incremental": incremental,
    }

    evidence_path = out_dir / "evidence.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path = out_dir / "report.md"
    report_path.write_text(build_report(evidence), encoding="utf-8")
    assert_evidence(evidence)
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline UTC duty/delegation checker")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    evidence = run(args.input, args.out)
    print(f"OK: {len(evidence['decisions'])} claims, {evidence['minute_enumeration']['minute_count']} minutes, {evidence['minute_enumeration']['fill_count']} fills")
    for case in evidence["decisions"]:
        print(f"  {case['verdict']:<8} {case['case_id']}")
    inc = evidence["incremental"]
    print(f"  incremental orgs={inc['post_patch_recomputed_orgs']} count={inc['actual_count_after_incremental_patch']} full={inc['full_recomputed_count_after_patch']} equal={inc['counts_equal']}")
    print(f"Evidence: {args.out / 'evidence.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
