-- Recoverable domain-first deletion state. 0006 is immutable once ledgered.

ALTER TABLE account_deletions
    ADD COLUMN status ENUM('pending', 'completed') NOT NULL DEFAULT 'pending' AFTER requested_at,
    ADD COLUMN updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) AFTER status,
    ADD COLUMN last_error_code VARCHAR(64) CHARACTER SET ascii NULL AFTER updated_at;
