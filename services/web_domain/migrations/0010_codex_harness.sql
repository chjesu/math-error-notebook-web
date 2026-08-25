-- Persist the opaque Codex app-server thread used by each notebook conversation.

CREATE TABLE IF NOT EXISTS codex_conversations (
    user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    conversation_id CHAR(32) CHARACTER SET ascii NOT NULL,
    thread_id VARCHAR(128) CHARACTER SET ascii NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (user_id, conversation_id),
    UNIQUE KEY uq_codex_conversations_thread (thread_id),
    CONSTRAINT fk_codex_conversations_user FOREIGN KEY (user_id) REFERENCES web_users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
