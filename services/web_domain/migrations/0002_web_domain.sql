-- MySQL 8.0 Web domain baseline. Personal rows are scoped directly by user_id.

CREATE TABLE IF NOT EXISTS question_sources (
    id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    source_uri VARCHAR(1024) NULL,
    license_status ENUM('user_authorized', 'open', 'restricted') NOT NULL,
    content_sha256 CHAR(64) CHARACTER SET ascii NOT NULL,
    created_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_question_sources_hash (content_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS questions (
    id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    source_id CHAR(32) CHARACTER SET ascii NOT NULL,
    canonical_sha256 CHAR(64) CHARACTER SET ascii NOT NULL,
    grade SMALLINT UNSIGNED NULL,
    difficulty DECIMAL(3,1) NULL,
    status ENUM('candidate', 'verified', 'rejected', 'retired') NOT NULL DEFAULT 'candidate',
    current_version_no INT UNSIGNED NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_questions_source FOREIGN KEY (source_id) REFERENCES question_sources(id),
    UNIQUE KEY uq_questions_canonical_hash (canonical_sha256),
    KEY ix_questions_recommendation (status, grade, difficulty),
    CHECK (grade IS NULL OR grade BETWEEN 10 AND 12),
    CHECK (difficulty IS NULL OR difficulty BETWEEN 1.0 AND 5.0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS question_versions (
    id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    question_id CHAR(32) CHARACTER SET ascii NOT NULL,
    version_no INT UNSIGNED NOT NULL,
    stem_text MEDIUMTEXT NOT NULL,
    answer_text MEDIUMTEXT NULL,
    solution_text MEDIUMTEXT NULL,
    content_sha256 CHAR(64) CHARACTER SET ascii NOT NULL,
    created_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_question_versions_question FOREIGN KEY (question_id) REFERENCES questions(id),
    UNIQUE KEY uq_question_versions_no (question_id, version_no),
    UNIQUE KEY uq_question_versions_hash (question_id, content_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS question_verifications (
    id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    question_version_id CHAR(32) CHARACTER SET ascii NOT NULL,
    verdict ENUM('verified', 'rejected', 'needs_review') NOT NULL,
    method ENUM('independent', 'human') NOT NULL,
    evidence_sha256 CHAR(64) CHARACTER SET ascii NOT NULL,
    verified_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_question_verifications_version FOREIGN KEY (question_version_id) REFERENCES question_versions(id),
    UNIQUE KEY uq_question_verification_evidence (question_version_id, method, evidence_sha256),
    KEY ix_question_verifications_latest (question_version_id, verdict, verified_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS web_files (
    id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    purpose ENUM('exam', 'answer_photo', 'question_image', 'audit_packet', 'practice_pdf', 'export') NOT NULL,
    original_name VARCHAR(255) NOT NULL,
    object_key VARCHAR(512) CHARACTER SET ascii NOT NULL,
    content_sha256 CHAR(64) CHARACTER SET ascii NOT NULL,
    media_type VARCHAR(80) CHARACTER SET ascii NOT NULL,
    byte_size BIGINT UNSIGNED NOT NULL,
    status ENUM('quarantined', 'ready', 'rejected', 'deleted') NOT NULL DEFAULT 'quarantined',
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_web_files_user FOREIGN KEY (user_id) REFERENCES web_users(id),
    UNIQUE KEY uq_web_files_object (object_key),
    UNIQUE KEY uq_web_files_user_content (user_id, purpose, content_sha256),
    KEY ix_web_files_user_status (user_id, status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS intake_items (
    id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    file_id CHAR(32) CHARACTER SET ascii NOT NULL,
    item_no INT UNSIGNED NOT NULL DEFAULT 1,
    input_version INT UNSIGNED NOT NULL DEFAULT 1,
    status ENUM('extracting', 'waiting_confirmation', 'confirmed', 'cancelled') NOT NULL DEFAULT 'extracting',
    question_text MEDIUMTEXT NULL,
    answer_text MEDIUMTEXT NULL,
    evidence_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_intake_items_user FOREIGN KEY (user_id) REFERENCES web_users(id),
    CONSTRAINT fk_intake_items_file FOREIGN KEY (file_id) REFERENCES web_files(id),
    UNIQUE KEY uq_intake_items_file_item (user_id, file_id, item_no),
    KEY ix_intake_items_user_status (user_id, status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS web_jobs (
    id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    job_type ENUM('extract', 'grade', 'import', 'practice_pdf') NOT NULL,
    resource_type ENUM('file', 'intake', 'attempt', 'question_source', 'error') NOT NULL,
    resource_id CHAR(32) CHARACTER SET ascii NOT NULL,
    idempotency_key VARCHAR(64) CHARACTER SET ascii NOT NULL,
    input_sha256 CHAR(64) CHARACTER SET ascii NOT NULL,
    status ENUM('queued', 'running', 'waiting_confirmation', 'completed', 'failed_retryable', 'failed_final', 'cancelled') NOT NULL DEFAULT 'queued',
    checkpoint_json JSON NULL,
    result_json JSON NULL,
    last_error_code VARCHAR(64) CHARACTER SET ascii NULL,
    lease_owner VARCHAR(80) CHARACTER SET ascii NULL,
    lease_expires_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_web_jobs_user FOREIGN KEY (user_id) REFERENCES web_users(id),
    UNIQUE KEY uq_web_jobs_user_request (user_id, job_type, idempotency_key),
    KEY ix_web_jobs_claim (status, lease_expires_at, created_at),
    KEY ix_web_jobs_user_status (user_id, status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS attempts (
    id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    intake_id CHAR(32) CHARACTER SET ascii NOT NULL,
    question_id CHAR(32) CHARACTER SET ascii NULL,
    input_version INT UNSIGNED NOT NULL,
    idempotency_key VARCHAR(64) CHARACTER SET ascii NOT NULL,
    question_text MEDIUMTEXT NOT NULL,
    answer_text MEDIUMTEXT NOT NULL,
    status ENUM('grading', 'grade_ready', 'committed', 'cancelled') NOT NULL DEFAULT 'grading',
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_attempts_user FOREIGN KEY (user_id) REFERENCES web_users(id),
    CONSTRAINT fk_attempts_intake FOREIGN KEY (intake_id) REFERENCES intake_items(id),
    CONSTRAINT fk_attempts_question FOREIGN KEY (question_id) REFERENCES questions(id),
    UNIQUE KEY uq_attempts_user_request (user_id, idempotency_key),
    KEY ix_attempts_user_status (user_id, status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS grade_candidates (
    id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    attempt_id CHAR(32) CHARACTER SET ascii NOT NULL,
    input_version INT UNSIGNED NOT NULL,
    verdict ENUM('correct', 'partial', 'incorrect', 'unclear') NOT NULL,
    first_error TEXT NULL,
    evidence_text MEDIUMTEXT NULL,
    confidence DECIMAL(4,3) NULL,
    result_sha256 CHAR(64) CHARACTER SET ascii NOT NULL,
    status ENUM('candidate', 'committed', 'superseded', 'rejected') NOT NULL DEFAULT 'candidate',
    created_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_grade_candidates_user FOREIGN KEY (user_id) REFERENCES web_users(id),
    CONSTRAINT fk_grade_candidates_attempt FOREIGN KEY (attempt_id) REFERENCES attempts(id),
    UNIQUE KEY uq_grade_candidates_result (user_id, attempt_id, input_version, result_sha256),
    KEY ix_grade_candidates_attempt_status (user_id, attempt_id, status),
    CHECK (confidence IS NULL OR confidence BETWEEN 0.000 AND 1.000)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS error_notebook_entries (
    id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    attempt_id CHAR(32) CHARACTER SET ascii NOT NULL,
    grade_candidate_id CHAR(32) CHARACTER SET ascii NOT NULL,
    question_id CHAR(32) CHARACTER SET ascii NULL,
    question_text MEDIUMTEXT NOT NULL,
    answer_text MEDIUMTEXT NOT NULL,
    first_error TEXT NULL,
    status ENUM('open', 'reviewing', 'mastered', 'removed') NOT NULL DEFAULT 'open',
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_error_entries_user FOREIGN KEY (user_id) REFERENCES web_users(id),
    CONSTRAINT fk_error_entries_attempt FOREIGN KEY (attempt_id) REFERENCES attempts(id),
    CONSTRAINT fk_error_entries_candidate FOREIGN KEY (grade_candidate_id) REFERENCES grade_candidates(id),
    CONSTRAINT fk_error_entries_question FOREIGN KEY (question_id) REFERENCES questions(id),
    UNIQUE KEY uq_error_entries_attempt (user_id, attempt_id),
    KEY ix_error_entries_user_status (user_id, status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS recommendations (
    id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    error_id CHAR(32) CHARACTER SET ascii NOT NULL,
    question_id CHAR(32) CHARACTER SET ascii NOT NULL,
    reason VARCHAR(255) NOT NULL,
    status ENUM('candidate', 'assigned', 'completed', 'withdrawn') NOT NULL DEFAULT 'candidate',
    created_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_recommendations_user FOREIGN KEY (user_id) REFERENCES web_users(id),
    CONSTRAINT fk_recommendations_error FOREIGN KEY (error_id) REFERENCES error_notebook_entries(id),
    CONSTRAINT fk_recommendations_question FOREIGN KEY (question_id) REFERENCES questions(id),
    UNIQUE KEY uq_recommendations_item (user_id, error_id, question_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS review_tasks (
    id CHAR(32) CHARACTER SET ascii PRIMARY KEY,
    user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    error_id CHAR(32) CHARACTER SET ascii NOT NULL,
    stage SMALLINT UNSIGNED NOT NULL,
    due_at DATETIME(6) NOT NULL,
    status ENUM('pending', 'ready', 'completed', 'cancelled') NOT NULL DEFAULT 'pending',
    generated_file_id CHAR(32) CHARACTER SET ascii NULL,
    created_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_review_tasks_user FOREIGN KEY (user_id) REFERENCES web_users(id),
    CONSTRAINT fk_review_tasks_error FOREIGN KEY (error_id) REFERENCES error_notebook_entries(id),
    CONSTRAINT fk_review_tasks_file FOREIGN KEY (generated_file_id) REFERENCES web_files(id),
    UNIQUE KEY uq_review_tasks_stage (user_id, error_id, stage),
    KEY ix_review_tasks_due (user_id, status, due_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS domain_audit_events (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id CHAR(32) CHARACTER SET ascii NOT NULL,
    event_type VARCHAR(64) CHARACTER SET ascii NOT NULL,
    resource_type VARCHAR(32) CHARACTER SET ascii NOT NULL,
    resource_id CHAR(32) CHARACTER SET ascii NOT NULL,
    metadata_json JSON NOT NULL,
    occurred_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_domain_audit_user FOREIGN KEY (user_id) REFERENCES web_users(id),
    KEY ix_domain_audit_user_time (user_id, occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
