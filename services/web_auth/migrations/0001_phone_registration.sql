-- MySQL 8.0; execute with utf8mb4 and strict SQL mode.
-- Secrets: phone_ciphertext is application-encrypted; OTP/session plaintext is never stored.

CREATE TABLE web_users (
    id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    phone_lookup_hash CHAR(64) CHARACTER SET ascii NOT NULL,
    phone_ciphertext VARBINARY(512) NULL,
    phone_last4 CHAR(4) CHARACTER SET ascii NOT NULL,
    display_name VARCHAR(80) NOT NULL,
    birth_date DATE NOT NULL,
    guardian_consent_receipt VARCHAR(128) NULL,
    status ENUM('active', 'restricted', 'pending_delete', 'deleted') NOT NULL DEFAULT 'active',
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_web_users_phone_hash (phone_lookup_hash),
    KEY ix_web_users_status_created (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE auth_sms_challenges (
    id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    phone_lookup_hash CHAR(64) CHARACTER SET ascii NOT NULL,
    tenant_scope_hash CHAR(64) CHARACTER SET ascii NOT NULL,
    purpose ENUM('register', 'login', 'bind_phone', 'recover') NOT NULL,
    code_hash CHAR(64) CHARACTER SET ascii NOT NULL,
    status ENUM('pending', 'sent', 'verified', 'cancelled', 'expired', 'locked', 'delivery_failed') NOT NULL,
    attempt_count SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    expires_at DATETIME(6) NOT NULL,
    provider_receipt VARCHAR(191) NULL,
    created_at DATETIME(6) NOT NULL,
    consumed_at DATETIME(6) NULL,
    KEY ix_auth_sms_phone_status (phone_lookup_hash, status, created_at),
    KEY ix_auth_sms_tenant_time (tenant_scope_hash, created_at),
    KEY ix_auth_sms_expiry (status, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE auth_rate_limit_buckets (
    dimension ENUM('phone', 'ip', 'ip_prefix', 'device', 'global', 'tenant') NOT NULL,
    subject_hash CHAR(64) CHARACTER SET ascii NOT NULL,
    window_kind ENUM('minute', 'hour', 'day') NOT NULL,
    window_start DATETIME NOT NULL,
    request_count INT UNSIGNED NOT NULL DEFAULT 0,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (dimension, subject_hash, window_kind, window_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE auth_send_cooldowns (
    phone_lookup_hash CHAR(64) CHARACTER SET ascii PRIMARY KEY,
    next_send_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE auth_sms_send_events (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    phone_lookup_hash CHAR(64) CHARACTER SET ascii NOT NULL,
    ip_hash CHAR(64) CHARACTER SET ascii NOT NULL,
    ip_prefix_hash CHAR(64) CHARACTER SET ascii NOT NULL,
    device_hash CHAR(64) CHARACTER SET ascii NOT NULL,
    tenant_scope_hash CHAR(64) CHARACTER SET ascii NOT NULL,
    occurred_at DATETIME(6) NOT NULL,
    KEY ix_auth_send_phone_time (phone_lookup_hash, occurred_at),
    KEY ix_auth_send_ip_time (ip_hash, occurred_at),
    KEY ix_auth_send_prefix_time (ip_prefix_hash, occurred_at),
    KEY ix_auth_send_device_time (device_hash, occurred_at),
    KEY ix_auth_send_tenant_time (tenant_scope_hash, occurred_at),
    KEY ix_auth_send_time (occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE auth_sessions (
    session_hash CHAR(64) CHARACTER SET ascii PRIMARY KEY,
    user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    revoked_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_auth_sessions_user FOREIGN KEY (user_id) REFERENCES web_users(id),
    KEY ix_auth_sessions_user_expiry (user_id, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE guardian_consents (
    id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    student_phone_lookup_hash CHAR(64) CHARACTER SET ascii NOT NULL,
    guardian_user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    policy_version VARCHAR(32) CHARACTER SET ascii NOT NULL,
    status ENUM('active', 'revoked', 'expired') NOT NULL,
    consented_at DATETIME(6) NOT NULL,
    revoked_at DATETIME(6) NULL,
    expires_at DATETIME(6) NULL,
    CONSTRAINT fk_guardian_consents_guardian FOREIGN KEY (guardian_user_id) REFERENCES web_users(id),
    KEY ix_guardian_consents_student_status (student_phone_lookup_hash, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE auth_audit_events (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    event_type VARCHAR(64) CHARACTER SET ascii NOT NULL,
    outcome VARCHAR(64) CHARACTER SET ascii NOT NULL,
    phone_masked VARCHAR(16) CHARACTER SET ascii NOT NULL,
    phone_lookup_hash CHAR(64) CHARACTER SET ascii NOT NULL,
    ip_hash CHAR(64) CHARACTER SET ascii NOT NULL,
    ip_prefix_hash CHAR(64) CHARACTER SET ascii NOT NULL,
    device_hash CHAR(64) CHARACTER SET ascii NOT NULL,
    tenant_scope_hash CHAR(64) CHARACTER SET ascii NOT NULL,
    metadata JSON NOT NULL,
    occurred_at DATETIME(6) NOT NULL,
    KEY ix_auth_audit_phone_time (phone_lookup_hash, occurred_at),
    KEY ix_auth_audit_event_time (event_type, occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
