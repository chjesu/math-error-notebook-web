ALTER TABLE review_tasks
    ADD COLUMN deferred_from DATETIME(6) NULL AFTER due_at,
    ADD COLUMN defer_reason VARCHAR(64) CHARACTER SET ascii NULL AFTER deferred_from;

