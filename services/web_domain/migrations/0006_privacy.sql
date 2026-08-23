-- Forward-only personal data export and account-deletion support.
-- Existing business rows are retained for policy-controlled deletion workflows,
-- but application code makes a deleted user's rows and files inaccessible.

ALTER TABLE web_jobs
    MODIFY COLUMN job_type ENUM('extract', 'grade', 'import', 'practice_pdf', 'export') NOT NULL,
    MODIFY COLUMN resource_type ENUM('file', 'intake', 'attempt', 'question_source', 'error', 'export') NOT NULL;

CREATE TABLE IF NOT EXISTS account_deletions (
    user_id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    requested_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_account_deletions_user FOREIGN KEY (user_id) REFERENCES web_users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
