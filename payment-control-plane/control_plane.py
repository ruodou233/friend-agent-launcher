#!/usr/bin/env python3
"""Small, dependency-free V1A recharge state machine.

The module is a local-verifiable reference implementation. It deliberately has no
balance table and no New API client: a caller may invoke its provisioner callback
only after claim_recharge returns a claim. Unknown provider outcomes stay in
crediting until an operator reconciles them.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional, Tuple, Union


MODULE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = MODULE_DIR / "schema.sql"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SENSITIVE_NOTE = re.compile(r"(?i)(sk-|bearer\s|api[_-]?key|password|secret|credential)")


class ControlPlaneError(Exception):
    """Base class for fail-closed control-plane errors."""


class NotFoundError(ControlPlaneError):
    pass


class IdempotencyConflictError(ControlPlaneError):
    pass


class InvalidStateError(ControlPlaneError):
    pass


class EvidenceRequiredError(ControlPlaneError):
    pass


class ClaimConflictError(ControlPlaneError):
    pass


@dataclass(frozen=True)
class RechargeRecord:
    request_id: str
    business_ref: str
    account_id: str
    amount_minor: int
    currency: str
    state: str
    attempt_no: int
    retry_count: int
    claim_id: Optional[str]
    claim_operator_id: Optional[str]
    claim_at: Optional[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Claim:
    request_id: str
    business_ref: str
    account_id: str
    amount_minor: int
    currency: str
    claim_id: str
    operator_id: str
    attempt_no: int


@dataclass(frozen=True)
class ProvisionResult:
    """Redacted outcome evidence supplied by the New API adapter/operator.

    ``not_executed`` is intentionally not a failure transition. It only records
    the two independent evidence references that may later authorize failed or
    one retry. ``unknown`` has no transition out of crediting.
    """

    status: str
    evidence_id: str
    executed: Optional[bool]
    debited: Optional[bool]
    new_api_evidence_ref: Optional[str]
    manual_confirmation_ref: Optional[str] = None
    details_digest: Optional[str] = None

    @classmethod
    def credited(cls, evidence_id: str, new_api_evidence_ref: str, details_digest: Optional[str] = None):
        return cls("credited", evidence_id, True, True, new_api_evidence_ref, None, details_digest)

    @classmethod
    def unknown(cls, evidence_id: str, new_api_evidence_ref: str, details_digest: Optional[str] = None):
        return cls("unknown", evidence_id, None, None, new_api_evidence_ref, None, details_digest)

    @classmethod
    def not_executed(
        cls,
        evidence_id: str,
        new_api_evidence_ref: str,
        manual_confirmation_ref: str,
        details_digest: Optional[str] = None,
    ):
        return cls(
            "not_executed",
            evidence_id,
            False,
            False,
            new_api_evidence_ref,
            manual_confirmation_ref,
            details_digest,
        )

    @classmethod
    def inconsistent(cls, evidence_id: str, new_api_evidence_ref: str, details_digest: Optional[str] = None):
        return cls("inconsistent", evidence_id, None, None, new_api_evidence_ref, None, details_digest)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_id(value: str, label: str) -> None:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError("{} must be a short opaque identifier".format(label))


def _validate_ref(value: Optional[str], label: str, required: bool = False) -> None:
    if value is None:
        if required:
            raise ValueError("{} is required".format(label))
        return
    if not isinstance(value, str) or not SAFE_REF.fullmatch(value):
        raise ValueError("{} must be a redacted opaque reference".format(label))


def _validate_digest(value: Optional[str]) -> None:
    if value is not None and not SHA256.fullmatch(value):
        raise ValueError("details_digest must be lowercase SHA-256 hex")


def _validate_amount_currency(amount_minor: int, currency: str) -> None:
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
        raise ValueError("amount_minor must be a positive integer")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("currency must be three uppercase letters")


def _validate_note(note: Optional[str]) -> None:
    if note is None:
        return
    if not isinstance(note, str) or len(note) > 512 or any(ord(char) < 32 for char in note):
        raise ValueError("operator_note must be a short printable note")
    if SENSITIVE_NOTE.search(note):
        raise ValueError("operator_note must not contain credential-like content")


def connect(database: Union[str, Path]) -> sqlite3.Connection:
    """Open a connection with explicit transactional settings and no secret logging."""

    connection = sqlite3.connect(
        str(database),
        timeout=5.0,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if str(database) != ":memory:":
        connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


class PaymentControlPlane:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _fetch(self, request_id: str) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM manual_recharges WHERE request_id = ?", (request_id,)
        ).fetchone()

    def _fetch_by_business_ref(self, business_ref: str) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM manual_recharges WHERE business_ref = ?", (business_ref,)
        ).fetchone()

    @staticmethod
    def _record(row: sqlite3.Row) -> RechargeRecord:
        return RechargeRecord(
            request_id=row["request_id"],
            business_ref=row["business_ref"],
            account_id=row["account_id"],
            amount_minor=row["amount_minor"],
            currency=row["currency"],
            state=row["state"],
            attempt_no=row["attempt_no"],
            retry_count=row["retry_count"],
            claim_id=row["claim_id"],
            claim_operator_id=row["claim_operator_id"],
            claim_at=row["claim_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, request_id: str) -> RechargeRecord:
        _validate_id(request_id, "request_id")
        row = self._fetch(request_id)
        if row is None:
            raise NotFoundError("recharge request not found")
        return self._record(row)

    def get_by_business_ref(self, business_ref: str) -> RechargeRecord:
        if not isinstance(business_ref, str) or not business_ref.startswith("manual-wechat:"):
            raise ValueError("business_ref must start with manual-wechat:")
        request_id = business_ref[len("manual-wechat:") :]
        _validate_id(request_id, "business_ref request id")
        row = self._fetch_by_business_ref(business_ref)
        if row is None:
            raise NotFoundError("recharge request not found")
        return self._record(row)

    def create_recharge(
        self,
        request_id: str,
        account_id: str,
        amount_minor: int,
        currency: str,
        operator_note: Optional[str] = None,
    ) -> RechargeRecord:
        _validate_id(request_id, "request_id")
        _validate_id(account_id, "account_id")
        _validate_amount_currency(amount_minor, currency)
        _validate_note(operator_note)
        business_ref = "manual-wechat:" + request_id
        now = utc_now()
        with self._write_transaction():
            existing = self._fetch(request_id)
            if existing is not None:
                same = (
                    existing["business_ref"] == business_ref
                    and existing["account_id"] == account_id
                    and existing["amount_minor"] == amount_minor
                    and existing["currency"] == currency
                )
                if not same:
                    raise IdempotencyConflictError("immutable recharge fields conflict with existing request")
                return self._record(existing)
            try:
                self.connection.execute(
                    """
                    INSERT INTO manual_recharges (
                        request_id, business_ref, account_id, amount_minor, currency,
                        state, attempt_no, retry_count, operator_note, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', 0, 0, ?, ?, ?)
                    """,
                    (request_id, business_ref, account_id, amount_minor, currency, operator_note, now, now),
                )
            except sqlite3.IntegrityError as error:
                raise IdempotencyConflictError("request_id or business_ref already exists") from error
            self._audit(request_id, None, "pending", "create", None, None, None, None, now)
            return self._record(self._fetch(request_id))

    def claim_recharge(self, request_id: str, operator_id: str) -> Optional[Claim]:
        """Atomically change pending to crediting; a miss means no provider call is allowed."""

        _validate_id(request_id, "request_id")
        _validate_id(operator_id, "operator_id")
        claim_id = uuid.uuid4().hex
        now = utc_now()
        with self._write_transaction():
            row = self._fetch(request_id)
            if row is None:
                raise NotFoundError("recharge request not found")
            updated = self.connection.execute(
                """
                UPDATE manual_recharges
                   SET state = 'crediting',
                       claim_id = ?,
                       claim_operator_id = ?,
                       claim_at = ?,
                       attempt_no = attempt_no + 1,
                       updated_at = ?
                 WHERE request_id = ?
                   AND state = 'pending'
                   AND attempt_no < 2
                """,
                (claim_id, operator_id, now, now, request_id),
            )
            if updated.rowcount != 1:
                return None
            row = self._fetch(request_id)
            self._audit(request_id, "pending", "crediting", "claim", operator_id, claim_id, None, None, now)
            return Claim(
                request_id=row["request_id"],
                business_ref=row["business_ref"],
                account_id=row["account_id"],
                amount_minor=row["amount_minor"],
                currency=row["currency"],
                claim_id=row["claim_id"],
                operator_id=row["claim_operator_id"],
                attempt_no=row["attempt_no"],
            )

    def run_claim(
        self,
        request_id: str,
        operator_id: str,
        provisioner: Callable[[Claim], ProvisionResult],
    ) -> Tuple[Optional[Claim], RechargeRecord]:
        """Call the provisioner only after a successful atomic claim.

        Any adapter exception is converted to redacted unknown evidence. It never
        becomes failed and it never triggers an automatic retry.
        """

        claim = self.claim_recharge(request_id, operator_id)
        if claim is None:
            return None, self.get(request_id)
        try:
            result = provisioner(claim)
            if not isinstance(result, ProvisionResult):
                raise TypeError("provisioner returned an unsupported result")
        except Exception as error:
            digest = hashlib.sha256(type(error).__name__.encode("utf-8")).hexdigest()
            result = ProvisionResult.unknown(
                evidence_id="adapter-" + uuid.uuid4().hex,
                new_api_evidence_ref="adapter-exception-" + uuid.uuid4().hex,
                details_digest=digest,
            )
        return claim, self.record_provision_result(request_id, claim.claim_id, operator_id, result)

    def record_provision_result(
        self,
        request_id: str,
        claim_id: str,
        operator_id: str,
        result: ProvisionResult,
    ) -> RechargeRecord:
        _validate_id(request_id, "request_id")
        _validate_id(claim_id, "claim_id")
        _validate_id(operator_id, "operator_id")
        self._validate_result(result)
        now = utc_now()
        with self._write_transaction():
            row = self._fetch(request_id)
            if row is None:
                raise NotFoundError("recharge request not found")
            existing_evidence = self._evidence(result.evidence_id)
            if existing_evidence is not None:
                if existing_evidence["request_id"] != request_id or existing_evidence["claim_id"] != claim_id:
                    raise IdempotencyConflictError("evidence_id belongs to another claim")
                return self._record(row)
            if row["state"] != "crediting":
                raise InvalidStateError("only crediting can accept a claim result")
            if row["claim_id"] != claim_id or row["claim_operator_id"] != operator_id:
                raise ClaimConflictError("claim is not owned by this operator")
            self._insert_evidence(request_id, claim_id, operator_id, result, now)
            if result.status == "credited":
                self.connection.execute(
                    "UPDATE manual_recharges SET state = 'credited', updated_at = ? WHERE request_id = ? AND state = 'crediting'",
                    (now, request_id),
                )
                self._audit(request_id, "crediting", "credited", "provider_credited", operator_id, claim_id, result.evidence_id, result.details_digest, now)
            else:
                # Unknown, not_executed and inconsistent evidence all remain crediting.
                self._audit(request_id, "crediting", "crediting", "provider_" + result.status, operator_id, claim_id, result.evidence_id, result.details_digest, now)
            return self._record(self._fetch(request_id))

    def fail_after_no_execution(
        self,
        request_id: str,
        claim_id: str,
        operator_id: str,
        evidence_id: str,
    ) -> RechargeRecord:
        """Enter failed only after a stored two-source no-execution/no-debit proof."""

        return self._resolve_no_execution(request_id, claim_id, operator_id, evidence_id, "failed")

    def retry_after_no_execution(
        self,
        request_id: str,
        claim_id: str,
        operator_id: str,
        evidence_id: str,
    ) -> RechargeRecord:
        """Return to pending for at most one explicitly evidenced second claim."""

        return self._resolve_no_execution(request_id, claim_id, operator_id, evidence_id, "pending")

    def _resolve_no_execution(
        self,
        request_id: str,
        claim_id: str,
        operator_id: str,
        evidence_id: str,
        target_state: str,
    ) -> RechargeRecord:
        if target_state not in ("failed", "pending"):
            raise ValueError("unsupported no-execution target")
        _validate_id(request_id, "request_id")
        _validate_id(claim_id, "claim_id")
        _validate_id(operator_id, "operator_id")
        _validate_id(evidence_id, "evidence_id")
        now = utc_now()
        with self._write_transaction():
            row = self._fetch(request_id)
            if row is None:
                raise NotFoundError("recharge request not found")
            if row["state"] != "crediting" or row["claim_id"] != claim_id or row["claim_operator_id"] != operator_id:
                raise ClaimConflictError("claim is not active for this operator")
            evidence = self._evidence(evidence_id)
            if evidence is None or evidence["request_id"] != request_id or evidence["claim_id"] != claim_id:
                raise EvidenceRequiredError("no matching stored evidence")
            if not self._qualifies_for_no_execution(evidence):
                raise EvidenceRequiredError("both no-execution and no-debit evidence are required")
            if target_state == "pending" and row["retry_count"] >= 1:
                raise EvidenceRequiredError("only one evidenced retry is allowed")
            if target_state == "pending" and row["attempt_no"] >= 2:
                raise EvidenceRequiredError("maximum claim attempts reached")
            if target_state == "pending":
                self.connection.execute(
                    """
                    UPDATE manual_recharges
                       SET state = 'pending', retry_count = retry_count + 1,
                           claim_id = NULL, claim_operator_id = NULL, claim_at = NULL,
                           updated_at = ?
                     WHERE request_id = ? AND state = 'crediting' AND claim_id = ?
                    """,
                    (now, request_id, claim_id),
                )
                self._audit(request_id, "crediting", "pending", "authorized_retry", operator_id, claim_id, evidence_id, None, now)
            else:
                self.connection.execute(
                    "UPDATE manual_recharges SET state = 'failed', updated_at = ? WHERE request_id = ? AND state = 'crediting' AND claim_id = ?",
                    (now, request_id, claim_id),
                )
                self._audit(request_id, "crediting", "failed", "failed_no_execution", operator_id, claim_id, evidence_id, None, now)
            return self._record(self._fetch(request_id))

    def _evidence(self, evidence_id: str) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM recharge_evidence WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()

    @staticmethod
    def _qualifies_for_no_execution(evidence: sqlite3.Row) -> bool:
        return bool(
            evidence["provider_state"] == "not_executed"
            and evidence["executed"] == 0
            and evidence["debited"] == 0
            and evidence["manual_confirmation_ref"]
            and evidence["new_api_evidence_ref"]
        )

    @staticmethod
    def _validate_result(result: ProvisionResult) -> None:
        if not isinstance(result, ProvisionResult):
            raise ValueError("result must be ProvisionResult")
        _validate_id(result.evidence_id, "evidence_id")
        _validate_ref(result.new_api_evidence_ref, "new_api_evidence_ref", required=True)
        _validate_ref(result.manual_confirmation_ref, "manual_confirmation_ref")
        _validate_digest(result.details_digest)
        if result.status not in ("credited", "unknown", "not_executed", "inconsistent"):
            raise ValueError("unsupported provider result")
        if result.status == "credited" and (result.executed is not True or result.debited is not True):
            raise EvidenceRequiredError("credited requires executed and debited evidence")
        if result.status == "unknown" and (result.executed is not None or result.debited is not None):
            raise EvidenceRequiredError("unknown must keep execution and debit unknown")
        if result.status == "not_executed":
            if result.executed is not False or result.debited is not False or not result.manual_confirmation_ref:
                raise EvidenceRequiredError("not_executed requires both independent confirmations")

    def _insert_evidence(
        self,
        request_id: str,
        claim_id: str,
        operator_id: str,
        result: ProvisionResult,
        recorded_at: str,
    ) -> None:
        try:
            self.connection.execute(
                """
                INSERT INTO recharge_evidence (
                    evidence_id, request_id, claim_id, operator_id, provider_state,
                    executed, debited, manual_confirmation_ref, new_api_evidence_ref,
                    details_digest, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.evidence_id,
                    request_id,
                    claim_id,
                    operator_id,
                    result.status,
                    None if result.executed is None else int(result.executed),
                    None if result.debited is None else int(result.debited),
                    result.manual_confirmation_ref,
                    result.new_api_evidence_ref,
                    result.details_digest,
                    recorded_at,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise IdempotencyConflictError("evidence_id already exists") from error

    def _audit(
        self,
        request_id: str,
        from_state: Optional[str],
        to_state: str,
        action: str,
        operator_id: Optional[str],
        claim_id: Optional[str],
        evidence_id: Optional[str],
        detail_digest: Optional[str],
        recorded_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO recharge_audit (
                audit_id, request_id, from_state, to_state, action,
                operator_id, claim_id, evidence_id, detail_digest, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                request_id,
                from_state,
                to_state,
                action,
                operator_id,
                claim_id,
                evidence_id,
                detail_digest,
                recorded_at,
            ),
        )


__all__ = [
    "Claim",
    "ClaimConflictError",
    "ControlPlaneError",
    "EvidenceRequiredError",
    "IdempotencyConflictError",
    "InvalidStateError",
    "NotFoundError",
    "PaymentControlPlane",
    "ProvisionResult",
    "RechargeRecord",
    "connect",
    "initialize",
]
