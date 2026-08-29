-- Separate operator authorization and immutable read-access audit.

CREATE TABLE IF NOT EXISTS admin_operators (
    user_id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    role ENUM('operations', 'reviewer', 'security', 'administrator') NOT NULL,
    status ENUM('active', 'disabled') NOT NULL DEFAULT 'active',
    granted_by CHAR(32) CHARACTER SET ascii NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_admin_operator_user FOREIGN KEY (user_id) REFERENCES web_users(id),
    CONSTRAINT fk_admin_operator_grantor FOREIGN KEY (granted_by) REFERENCES web_users(id),
    KEY ix_admin_operator_status_role (status, role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS operations_audit_events (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    operator_user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    event_type VARCHAR(64) CHARACTER SET ascii NOT NULL,
    resource_type VARCHAR(32) CHARACTER SET ascii NOT NULL,
    resource_id VARCHAR(64) CHARACTER SET ascii NOT NULL,
    metadata_json JSON NOT NULL,
    occurred_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_operations_audit_operator FOREIGN KEY (operator_user_id) REFERENCES web_users(id),
    KEY ix_operations_audit_operator_time (operator_user_id, occurred_at),
    KEY ix_operations_audit_event_time (event_type, occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
