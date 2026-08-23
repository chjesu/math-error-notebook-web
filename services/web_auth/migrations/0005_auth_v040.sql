-- v0.4 authentication additions; never rewrite 0001--0004.

ALTER TABLE auth_sms_challenges
    MODIFY COLUMN purpose ENUM('register', 'login', 'bind_phone', 'recover', 'sensitive_export', 'sensitive_delete') NOT NULL;

CREATE TABLE auth_password_credentials (
    user_id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    salt BINARY(16) NOT NULL,
    password_hash BINARY(64) NOT NULL,
    parameters VARCHAR(128) CHARACTER SET ascii NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_auth_password_user FOREIGN KEY (user_id) REFERENCES web_users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE auth_agreement_acceptances (
    id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    agreement_version VARCHAR(128) CHARACTER SET utf8mb4 NOT NULL,
    accepted_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_auth_agreement_user FOREIGN KEY (user_id) REFERENCES web_users(id),
    KEY ix_auth_agreement_user_time (user_id, accepted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
