CREATE TABLE IF NOT EXISTS daily_learning_usage (
    user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    usage_date DATE NOT NULL,
    kind ENUM('grade', 'recommendation') NOT NULL,
    resource_id CHAR(64) CHARACTER SET ascii NOT NULL,
    status ENUM('reserved', 'counted') NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (user_id, usage_date, kind, resource_id),
    CONSTRAINT fk_daily_learning_usage_user FOREIGN KEY (user_id) REFERENCES web_users(id),
    KEY ix_daily_learning_usage_count (user_id, usage_date, kind, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
