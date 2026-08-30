SET @phase5_rank_exists = (
  SELECT COUNT(*)
  FROM information_schema.columns
  WHERE table_schema = DATABASE()
    AND table_name = 'recommendations'
    AND column_name = 'recommendation_rank'
);
SET @phase5_rank_ddl = IF(
  @phase5_rank_exists = 0,
  'ALTER TABLE recommendations ADD COLUMN recommendation_rank TINYINT UNSIGNED NOT NULL DEFAULT 1 AFTER status',
  'SELECT 1'
);
PREPARE phase5_rank_statement FROM @phase5_rank_ddl;
EXECUTE phase5_rank_statement;
DEALLOCATE PREPARE phase5_rank_statement;

CREATE OR REPLACE SQL SECURITY INVOKER VIEW v_error_learning_facts AS
SELECT
  e.user_id,
  e.id AS error_id,
  e.status,
  e.created_at,
  JSON_UNQUOTE(JSON_EXTRACT(c.evidence_text, '$.cause_code')) AS cause_code
FROM error_notebook_entries e
JOIN grade_candidates c ON c.id = e.grade_candidate_id AND c.user_id = e.user_id
WHERE e.status <> 'removed'
  AND JSON_VALID(c.evidence_text)
  AND JSON_UNQUOTE(JSON_EXTRACT(c.evidence_text, '$.schema')) = 'math-error-diagnosis/v1'
  AND JSON_UNQUOTE(JSON_EXTRACT(c.evidence_text, '$.cause_code')) IN (
    'knowledge_gap', 'concept_confusion', 'formula_condition', 'method_choice',
    'reasoning_gap', 'algebra_transform', 'calculation', 'misreading',
    'incomplete_cases', 'expression', 'careless', 'unclear'
  )
  AND JSON_TYPE(JSON_EXTRACT(c.evidence_text, '$.cause_evidence')) = 'STRING'
  AND CHAR_LENGTH(TRIM(JSON_UNQUOTE(JSON_EXTRACT(c.evidence_text, '$.cause_evidence')))) BETWEEN 1 AND 12000
  AND JSON_TYPE(JSON_EXTRACT(c.evidence_text, '$.knowledge_points')) = 'ARRAY'
  AND JSON_LENGTH(JSON_EXTRACT(c.evidence_text, '$.knowledge_points')) BETWEEN 1 AND 8
  AND JSON_TYPE(JSON_EXTRACT(c.evidence_text, '$.correct_solution')) = 'STRING'
  AND CHAR_LENGTH(TRIM(JSON_UNQUOTE(JSON_EXTRACT(c.evidence_text, '$.correct_solution')))) BETWEEN 1 AND 12000
  AND JSON_TYPE(JSON_EXTRACT(c.evidence_text, '$.final_answer')) = 'STRING'
  AND CHAR_LENGTH(TRIM(JSON_UNQUOTE(JSON_EXTRACT(c.evidence_text, '$.final_answer')))) BETWEEN 1 AND 12000;

CREATE OR REPLACE SQL SECURITY INVOKER VIEW v_error_knowledge_facts AS
SELECT
  facts.user_id,
  facts.error_id,
  facts.status,
  facts.created_at,
  TRIM(points.knowledge_point) AS knowledge_point
FROM v_error_learning_facts facts
JOIN error_notebook_entries e ON e.id = facts.error_id AND e.user_id = facts.user_id
JOIN grade_candidates c ON c.id = e.grade_candidate_id AND c.user_id = e.user_id
JOIN JSON_TABLE(
  c.evidence_text,
  '$.knowledge_points[*]' COLUMNS (knowledge_point VARCHAR(200) PATH '$')
) AS points
WHERE points.knowledge_point IS NOT NULL
  AND CHAR_LENGTH(TRIM(points.knowledge_point)) BETWEEN 1 AND 200;
