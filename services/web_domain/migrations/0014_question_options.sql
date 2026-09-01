-- Preserve printed multiple-choice options for bank questions on the web side.
-- The canonical desktop bank carries options_json; earlier migrations dropped it,
-- leaving 选择题 stems without their A/B/C/D choices in generated PDFs.

ALTER TABLE question_versions
    ADD COLUMN options_json JSON NULL AFTER solution_text;
