-- Bind each public upload idempotency key to one exact request and one file.

CREATE TABLE IF NOT EXISTS file_upload_idempotency (
    user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    idempotency_key VARCHAR(64) CHARACTER SET ascii NOT NULL,
    request_sha256 CHAR(64) CHARACTER SET ascii NOT NULL,
    file_id CHAR(32) CHARACTER SET ascii NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (user_id, idempotency_key),
    CONSTRAINT fk_file_upload_idempotency_user FOREIGN KEY (user_id) REFERENCES web_users(id),
    CONSTRAINT fk_file_upload_idempotency_file FOREIGN KEY (file_id) REFERENCES web_files(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
