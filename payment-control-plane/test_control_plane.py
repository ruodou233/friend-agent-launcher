#!/usr/bin/env python3
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from control_plane import (
    EvidenceRequiredError,
    IdempotencyConflictError,
    InvalidStateError,
    PaymentControlPlane,
    ProvisionResult,
    connect,
    initialize,
)


class PaymentControlPlaneTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "control-plane.sqlite3"
        self.connection = connect(self.database)
        initialize(self.connection)
        self.control = PaymentControlPlane(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def test_schema_has_required_not_null_unique_identifiers_and_no_balance_ledger(self):
        columns = {
            row[1]: row[3] for row in self.connection.execute("PRAGMA table_info(manual_recharges)")
        }
        self.assertEqual(columns["request_id"], 1)
        self.assertEqual(columns["business_ref"], 1)
        unique_indexes = {
            row[1]
            for row in self.connection.execute("PRAGMA index_list(manual_recharges)")
            if row[2]
        }
        unique_columns = set()
        for index_name in unique_indexes:
            unique_columns.update(
                row[2]
                for row in self.connection.execute("PRAGMA index_info('{}')".format(index_name))
            )
        self.assertIn("request_id", unique_columns)
        self.assertIn("business_ref", unique_columns)
        table_names = {
            row[0]
            for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.assertNotIn("balances", table_names)
        self.assertNotIn("account_balances", table_names)

    def test_recharge_is_idempotent_and_immutable(self):
        first = self.control.create_recharge("wechat-001", "acct-001", 100, "CNY")
        replay = self.control.create_recharge("wechat-001", "acct-001", 100, "CNY")
        self.assertEqual(first, replay)
        self.assertEqual(first.business_ref, "manual-wechat:wechat-001")
        with self.assertRaises(IdempotencyConflictError):
            self.control.create_recharge("wechat-001", "acct-001", 101, "CNY")
        with self.assertRaises(IdempotencyConflictError):
            self.control.create_recharge("wechat-001", "acct-002", 100, "CNY")

    def test_two_atomic_claimers_yield_one_claim(self):
        self.control.create_recharge("wechat-002", "acct-002", 200, "CNY")
        database = self.database

        def claim_from_new_connection(operator):
            connection = connect(database)
            try:
                return PaymentControlPlane(connection).claim_recharge("wechat-002", operator)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim_from_new_connection, ["operator-a", "operator-b"]))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.control.get("wechat-002").state, "crediting")

    def test_unknown_stays_crediting_and_cannot_be_failed_or_retried(self):
        self.control.create_recharge("wechat-003", "acct-003", 300, "CNY")
        claim = self.control.claim_recharge("wechat-003", "operator-a")
        current = self.control.record_provision_result(
            "wechat-003",
            claim.claim_id,
            "operator-a",
            ProvisionResult.unknown("evidence-unknown", "new-api-check-unknown"),
        )
        self.assertEqual(current.state, "crediting")
        with self.assertRaises(EvidenceRequiredError):
            self.control.fail_after_no_execution("wechat-003", claim.claim_id, "operator-a", "evidence-unknown")
        with self.assertRaises(EvidenceRequiredError):
            self.control.retry_after_no_execution("wechat-003", claim.claim_id, "operator-a", "evidence-unknown")

    def test_only_two_source_no_execution_proof_allows_one_retry(self):
        self.control.create_recharge("wechat-004", "acct-004", 400, "CNY")
        first_claim = self.control.claim_recharge("wechat-004", "operator-a")
        evidence = ProvisionResult.not_executed(
            "evidence-no-exec",
            "new-api-no-accept",
            "human-reconcile-record",
        )
        self.control.record_provision_result("wechat-004", first_claim.claim_id, "operator-a", evidence)
        pending = self.control.retry_after_no_execution(
            "wechat-004", first_claim.claim_id, "operator-a", evidence.evidence_id
        )
        self.assertEqual(pending.state, "pending")
        self.assertEqual(pending.retry_count, 1)
        second_claim = self.control.claim_recharge("wechat-004", "operator-b")
        self.assertEqual(second_claim.attempt_no, 2)
        self.control.record_provision_result(
            "wechat-004",
            second_claim.claim_id,
            "operator-b",
            ProvisionResult.unknown("evidence-second-unknown", "new-api-second-unknown"),
        )
        with self.assertRaises(EvidenceRequiredError):
            self.control.retry_after_no_execution(
                "wechat-004", second_claim.claim_id, "operator-b", "evidence-no-exec"
            )
        self.assertEqual(self.control.get("wechat-004").state, "crediting")

    def test_failed_requires_proof_and_is_not_claimable(self):
        self.control.create_recharge("wechat-005", "acct-005", 500, "CNY")
        claim = self.control.claim_recharge("wechat-005", "operator-a")
        evidence = ProvisionResult.not_executed(
            "evidence-failed",
            "new-api-confirmed-no-debit",
            "human-confirmed-no-execution",
        )
        self.control.record_provision_result("wechat-005", claim.claim_id, "operator-a", evidence)
        failed = self.control.fail_after_no_execution(
            "wechat-005", claim.claim_id, "operator-a", evidence.evidence_id
        )
        self.assertEqual(failed.state, "failed")
        self.assertIsNone(self.control.claim_recharge("wechat-005", "operator-b"))
        with self.assertRaises(InvalidStateError):
            self.control.record_provision_result(
                "wechat-005",
                claim.claim_id,
                "operator-a",
                ProvisionResult.credited("evidence-late", "new-api-late"),
            )

    def test_run_claim_calls_provider_only_after_claim_and_never_replays_credited(self):
        self.control.create_recharge("wechat-006", "acct-006", 600, "CNY")
        calls = []

        def provision(claim):
            calls.append(claim.claim_id)
            return ProvisionResult.credited("evidence-credited", "new-api-credited")

        first_claim, first_record = self.control.run_claim("wechat-006", "operator-a", provision)
        self.assertIsNotNone(first_claim)
        self.assertEqual(first_record.state, "credited")
        second_claim, second_record = self.control.run_claim("wechat-006", "operator-b", provision)
        self.assertIsNone(second_claim)
        self.assertEqual(second_record.state, "credited")
        self.assertEqual(len(calls), 1)

    def test_adapter_exception_is_redacted_unknown_not_failed(self):
        self.control.create_recharge("wechat-007", "acct-007", 700, "CNY")

        def explode(_claim):
            raise RuntimeError("do not persist this provider response")

        _, record = self.control.run_claim("wechat-007", "operator-a", explode)
        self.assertEqual(record.state, "crediting")
        evidence = self.connection.execute(
            "SELECT provider_state, details_digest FROM recharge_evidence WHERE request_id = ?",
            ("wechat-007",),
        ).fetchone()
        self.assertEqual(evidence[0], "unknown")
        self.assertIsNotNone(evidence[1])
        self.assertNotIn("provider response", evidence[1])

    def test_schema_can_be_reopened_and_duplicate_business_ref_is_rejected(self):
        self.control.create_recharge("wechat-008", "acct-008", 800, "CNY")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO manual_recharges (
                    request_id, business_ref, account_id, amount_minor, currency,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                ("wechat-009", "manual-wechat:wechat-008", "acct-009", 900, "CNY", "now", "now"),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
