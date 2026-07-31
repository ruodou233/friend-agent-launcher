-- Friend V1A payment control-plane reference migration.
-- It stores request/operation evidence only; it is not a balance ledger and never stores a Friend Key.
-- Apply once to a MySQL 8-compatible database or use the local SQLite verifier in verify.sh.

CREATE TABLE IF NOT EXISTS manual_recharges (
    request_id VARCHAR(128) NOT NULL PRIMARY KEY,
    business_ref VARCHAR(255) NOT NULL UNIQUE,
    account_id VARCHAR(128) NOT NULL,
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    currency VARCHAR(3) NOT NULL CHECK (length(currency) = 3),
    state VARCHAR(16) NOT NULL CHECK (state IN ('pending', 'crediting', 'credited', 'failed')),
    attempt_no INTEGER NOT NULL DEFAULT 0 CHECK (attempt_no BETWEEN 0 AND 2),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count BETWEEN 0 AND 1),
    operator_note VARCHAR(512),
    claim_id VARCHAR(64),
    claim_operator_id VARCHAR(128),
    claim_at VARCHAR(64),
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    CONSTRAINT uq_manual_recharges_request_id UNIQUE (request_id),
    CHECK (state <> 'pending' OR (claim_id IS NULL AND claim_operator_id IS NULL AND claim_at IS NULL))
);

CREATE TABLE IF NOT EXISTS recharge_evidence (
    evidence_id VARCHAR(128) NOT NULL PRIMARY KEY,
    request_id VARCHAR(128) NOT NULL,
    claim_id VARCHAR(64) NOT NULL,
    operator_id VARCHAR(128) NOT NULL,
    provider_state VARCHAR(32) NOT NULL CHECK (provider_state IN ('credited', 'unknown', 'not_executed', 'inconsistent')),
    executed INTEGER CHECK (executed IN (0, 1) OR executed IS NULL),
    debited INTEGER CHECK (debited IN (0, 1) OR debited IS NULL),
    manual_confirmation_ref VARCHAR(255),
    new_api_evidence_ref VARCHAR(255),
    details_digest CHAR(64),
    recorded_at VARCHAR(64) NOT NULL,
    FOREIGN KEY (request_id) REFERENCES manual_recharges(request_id),
    CHECK (
        (provider_state = 'credited' AND executed = 1 AND debited = 1)
        OR (provider_state = 'unknown' AND executed IS NULL AND debited IS NULL)
        OR (provider_state = 'not_executed' AND executed = 0 AND debited = 0)
        OR (provider_state = 'inconsistent')
    )
);

CREATE TABLE IF NOT EXISTS recharge_audit (
    audit_id VARCHAR(64) NOT NULL PRIMARY KEY,
    request_id VARCHAR(128) NOT NULL,
    from_state VARCHAR(16),
    to_state VARCHAR(16) NOT NULL CHECK (to_state IN ('pending', 'crediting', 'credited', 'failed')),
    action VARCHAR(64) NOT NULL,
    operator_id VARCHAR(128),
    claim_id VARCHAR(64),
    evidence_id VARCHAR(128),
    detail_digest CHAR(64),
    recorded_at VARCHAR(64) NOT NULL,
    FOREIGN KEY (request_id) REFERENCES manual_recharges(request_id)
);

CREATE INDEX idx_manual_recharges_state ON manual_recharges (state);
CREATE INDEX idx_recharge_evidence_request ON recharge_evidence (request_id, claim_id);
CREATE INDEX idx_recharge_audit_request ON recharge_audit (request_id, recorded_at);
