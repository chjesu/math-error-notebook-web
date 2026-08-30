CREATE TABLE IF NOT EXISTS intake_batches (
    id CHAR(32) CHARACTER SET ascii NOT NULL,
    user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    operation_version VARCHAR(32) CHARACTER SET ascii NOT NULL,
    idempotency_key VARCHAR(128) CHARACTER SET ascii NOT NULL,
    request_sha256 CHAR(64) CHARACTER SET ascii NOT NULL,
    status ENUM('pending','slicing','solving','grading','completed','failed') NOT NULL,
    total_files SMALLINT UNSIGNED NOT NULL,
    completed_files SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    total_items SMALLINT UNSIGNED NULL,
    completed_items SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    stage_completed_items SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    last_event_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
    error_code VARCHAR(64) CHARACTER SET ascii NULL,
    slicing_attempts TINYINT UNSIGNED NOT NULL DEFAULT 0,
    solving_attempts TINYINT UNSIGNED NOT NULL DEFAULT 0,
    grading_attempts TINYINT UNSIGNED NOT NULL DEFAULT 0,
    retry_at DATETIME(6) NOT NULL,
    slot_id TINYINT UNSIGNED NULL,
    claim_epoch BIGINT UNSIGNED NOT NULL DEFAULT 0,
    lease_owner VARCHAR(64) CHARACTER SET ascii NULL,
    lease_expires_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_intake_batch_request (user_id, operation_version, idempotency_key),
    UNIQUE KEY uq_intake_batch_active_slot (slot_id),
    KEY ix_intake_batch_due (status, retry_at, lease_expires_at, created_at),
    CONSTRAINT fk_intake_batches_user FOREIGN KEY (user_id) REFERENCES web_users(id),
    CONSTRAINT ck_intake_batch_slot CHECK (slot_id IS NULL OR slot_id IN (1, 2)),
    CONSTRAINT ck_intake_batch_file_progress CHECK (completed_files <= total_files),
    CONSTRAINT ck_intake_batch_item_progress CHECK (total_items IS NULL OR completed_items <= total_items)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS intake_batch_files (
    batch_id CHAR(32) CHARACTER SET ascii NOT NULL,
    file_ordinal SMALLINT UNSIGNED NOT NULL,
    file_id CHAR(32) CHARACTER SET ascii NOT NULL,
    object_key VARCHAR(512) NOT NULL,
    content_sha256 CHAR(64) CHARACTER SET ascii NOT NULL,
    media_type VARCHAR(64) CHARACTER SET ascii NOT NULL,
    byte_size BIGINT UNSIGNED NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (batch_id, file_ordinal),
    UNIQUE KEY uq_intake_batch_file (batch_id, file_id),
    CONSTRAINT fk_intake_batch_files_batch FOREIGN KEY (batch_id) REFERENCES intake_batches(id),
    CONSTRAINT fk_intake_batch_files_file FOREIGN KEY (file_id) REFERENCES web_files(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS intake_batch_operations (
    batch_id CHAR(32) CHARACTER SET ascii NOT NULL,
    operation_key VARCHAR(64) CHARACTER SET ascii NOT NULL,
    stage ENUM('slicing','solving','grading') NOT NULL,
    item_ordinal SMALLINT UNSIGNED NOT NULL,
    status ENUM('intent','completed') NOT NULL,
    result_json JSON NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (batch_id, operation_key),
    KEY ix_intake_batch_operations_stage (batch_id, stage, status, item_ordinal),
    CONSTRAINT fk_intake_batch_operations_batch FOREIGN KEY (batch_id) REFERENCES intake_batches(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS intake_batch_events (
    batch_id CHAR(32) CHARACTER SET ascii NOT NULL,
    event_sequence BIGINT UNSIGNED NOT NULL,
    event_type VARCHAR(48) CHARACTER SET ascii NOT NULL,
    data_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (batch_id, event_sequence),
    CONSTRAINT fk_intake_batch_events_batch FOREIGN KEY (batch_id) REFERENCES intake_batches(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS intake_worker_slots (
    slot_id TINYINT UNSIGNED NOT NULL,
    batch_id CHAR(32) CHARACTER SET ascii NULL,
    claim_epoch BIGINT UNSIGNED NOT NULL DEFAULT 0,
    lease_owner VARCHAR(64) CHARACTER SET ascii NULL,
    lease_expires_at DATETIME(6) NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (slot_id),
    UNIQUE KEY uq_intake_worker_slot_batch (batch_id),
    CONSTRAINT ck_intake_worker_slot_id CHECK (slot_id IN (1, 2)),
    CONSTRAINT fk_intake_worker_slot_batch FOREIGN KEY (batch_id) REFERENCES intake_batches(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT IGNORE INTO intake_worker_slots (slot_id, batch_id, claim_epoch, lease_owner, lease_expires_at, updated_at)
VALUES (1, NULL, 0, NULL, NULL, UTC_TIMESTAMP(6)),
       (2, NULL, 0, NULL, NULL, UTC_TIMESTAMP(6));
