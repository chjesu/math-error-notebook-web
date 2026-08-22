-- Additive learning-loop history. Existing tables and rows are preserved.

CREATE TABLE IF NOT EXISTS review_attempts (
    id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    review_task_id CHAR(32) CHARACTER SET ascii NOT NULL,
    error_id CHAR(32) CHARACTER SET ascii NOT NULL,
    stage SMALLINT UNSIGNED NOT NULL,
    result ENUM('correct', 'partial', 'wrong') NOT NULL,
    idempotency_key VARCHAR(64) CHARACTER SET ascii NOT NULL,
    completed_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_review_attempts_user FOREIGN KEY (user_id) REFERENCES web_users(id),
    CONSTRAINT fk_review_attempts_task FOREIGN KEY (review_task_id) REFERENCES review_tasks(id),
    CONSTRAINT fk_review_attempts_error FOREIGN KEY (error_id) REFERENCES error_notebook_entries(id),
    UNIQUE KEY uq_review_attempts_request (user_id, idempotency_key),
    KEY ix_review_attempts_error_time (user_id, error_id, completed_at),
    CHECK (stage BETWEEN 1 AND 6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
