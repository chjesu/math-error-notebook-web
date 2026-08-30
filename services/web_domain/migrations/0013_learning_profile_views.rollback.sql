DROP VIEW IF EXISTS v_error_knowledge_facts;
DROP VIEW IF EXISTS v_error_learning_facts;

SET @phase5_rank_exists = (
  SELECT COUNT(*)
  FROM information_schema.columns
  WHERE table_schema = DATABASE()
    AND table_name = 'recommendations'
    AND column_name = 'recommendation_rank'
);
SET @phase5_rank_ddl = IF(
  @phase5_rank_exists = 1,
  'ALTER TABLE recommendations DROP COLUMN recommendation_rank',
  'SELECT 1'
);
PREPARE phase5_rank_statement FROM @phase5_rank_ddl;
EXECUTE phase5_rank_statement;
DEALLOCATE PREPARE phase5_rank_statement;
