from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import duty_audit as da  # noqa: E402


class DutyAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.world = da.load_world(da.DEFAULT_INPUT)
        cls.accounts = da.account_index(cls.world)
        cls.minutes = da.minute_span(cls.world)

    def test_half_open_interval_endpoints(self) -> None:
        record = next(s for s in self.world["shifts"] if s["id"] == "shift.late.approval.day")
        self.assertTrue(da.covers_half_open(record, da.parse_ts("2026-11-26T23:00:00Z")))
        self.assertTrue(da.covers_half_open(record, da.parse_ts("2026-11-27T00:59:00Z")))
        self.assertFalse(da.covers_half_open(record, da.parse_ts("2026-11-27T01:00:00Z")))

    def test_crossday_approval_and_converging_paths(self) -> None:
        claim = next(p for p in da.PROBES if p["case_id"] == "C1_CROSSDAY_DUAL_DUTY_SHARED_CONTROLLER")
        result = da.evaluate_claim(self.world, self.accounts, claim)
        self.assertEqual(result["verdict"], "APPROVED")
        self.assertEqual(result["reasons"], [])

        approval = result["role_evidence"]["approval"]
        self.assertTrue(approval["actor_effective"])
        self.assertGreaterEqual(len(approval["all_simple_proof_paths"]), 2)
        self.assertEqual(approval["root_accounts"], ["acct.harbor.controller"])
        self.assertEqual(approval["actual_controllers"], ["ctrl.harbor-zhou"])
        self.assertEqual(result["shared_actual_controllers"], ["ctrl.harbor-zhou"])

    def test_start_and_end_endpoint_decisions(self) -> None:
        start = next(p for p in da.PROBES if p["case_id"] == "C2_START_ENDPOINT_IS_INCLUSIVE")
        end = next(p for p in da.PROBES if p["case_id"] == "C3_END_ENDPOINT_IS_EXCLUSIVE")
        self.assertEqual(da.evaluate_claim(self.world, self.accounts, start)["verdict"], "APPROVED")
        end_result = da.evaluate_claim(self.world, self.accounts, end)
        self.assertEqual(end_result["verdict"], "REJECTED")
        self.assertIn("INTAKE_CLAIMED_ACTOR_NOT_EFFECTIVE", end_result["reasons"])

    def test_sourceless_ring_has_no_seat(self) -> None:
        graph = da.recompute_minute(self.world, da.parse_ts("2026-11-27T00:10:00Z"), "org.ring")
        self.assertEqual(graph["holders"].get(("org.ring", "intake"), []), [])
        self.assertEqual(graph["proofs"], {})
        claim = next(p for p in da.PROBES if p["case_id"] == "C4_SOURCELESS_RING_CANNOT_FILL_SEAT")
        self.assertEqual(da.evaluate_claim(self.world, self.accounts, claim)["verdict"], "REJECTED")

    def test_rooted_mutual_batch_produces_seat(self) -> None:
        claim = next(p for p in da.PROBES if p["case_id"] == "C5_ROOTED_MUTUAL_BATCH_APPROVE")
        result = da.evaluate_claim(self.world, self.accounts, claim)
        self.assertEqual(result["verdict"], "APPROVED")
        self.assertTrue(result["role_evidence"]["approval"]["actor_effective"])

    def test_late_old_approval_is_rejected_at_receipt_but_was_valid_when_acted(self) -> None:
        claim = next(p for p in da.PROBES if p["case_id"] == "C7_OLD_APPROVAL_LATE_AFTER_HANDOVER")
        result = da.evaluate_claim(self.world, self.accounts, claim)
        self.assertEqual(result["verdict"], "REJECTED")
        self.assertIn("APPROVAL_CLAIMED_ACTOR_NOT_EFFECTIVE", result["reasons"])
        self.assertTrue(result["action_time_check"]["both_roles_effective_when_acted"])
        self.assertEqual(result["action_time_check"]["event_time_anchor_alternative_verdict"], "APPROVED")

    def test_incremental_recompute_matches_full(self) -> None:
        proof = da.incremental_proof(self.world, self.minutes)
        self.assertTrue(proof["counts_equal"])
        self.assertTrue(proof["exact_minute_holder_state_equal"])
        self.assertTrue(proof["state_hashes_equal"])
        self.assertEqual(proof["post_patch_recomputed_orgs"], ["org.increment"])
        self.assertEqual(proof["actual_added_fills"], 120)
        self.assertEqual(
            proof["actual_count_after_incremental_patch"],
            proof["full_recomputed_count_after_patch"],
        )

    def test_generated_evidence_passes_builtin_and_independent_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "evidence"
            evidence = da.run(da.DEFAULT_INPUT, out)
            self.assertTrue((out / "evidence.json").exists())
            self.assertTrue((out / "minute_roles.csv").exists())
            da.assert_evidence(evidence)

            verifier = ROOT / "verify_evidence.py"
            import subprocess

            completed = subprocess.run(
                [sys.executable, str(verifier), str(out / "evidence.json")],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(completed.returncode, 0, msg=completed.stdout + completed.stderr)

    def test_publish_canonical_evidence_directory(self) -> None:
        # The deliverable keeps a recomputable evidence/ directory in the
        # workspace; regenerate and independently verify it during tests.
        evidence = da.run(da.DEFAULT_INPUT, da.DEFAULT_OUT)
        da.assert_evidence(evidence)
        verifier = ROOT / "verify_evidence.py"
        import subprocess

        completed = subprocess.run(
            [sys.executable, str(verifier), str(da.DEFAULT_OUT / "evidence.json")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
