-- Store only provider-reported aggregate token totals for authenticated Harness sessions.

CREATE TABLE IF NOT EXISTS model_usage_sessions (
    session_hash CHAR(64) CHARACTER SET ascii PRIMARY KEY,
    user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    uncached_input_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
    output_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
    cache_read_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
    cache_write_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_model_usage_sessions_user FOREIGN KEY (user_id) REFERENCES web_users(id) ON DELETE CASCADE,
    KEY ix_model_usage_sessions_user_updated (user_id, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
