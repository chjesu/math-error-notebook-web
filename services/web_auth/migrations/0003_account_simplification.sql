-- Forward-only v0.3.2 account simplification. 0001 remains immutable.
-- The product no longer collects profile, age, identity, family, or guardian data.

UPDATE web_users SET status='active' WHERE status='restricted';

DROP TABLE IF EXISTS guardian_consents;

ALTER TABLE web_users
    DROP COLUMN display_name,
    DROP COLUMN birth_date,
    DROP COLUMN guardian_consent_receipt,
    MODIFY COLUMN status ENUM('active', 'locked', 'pending_delete', 'deleted')
        NOT NULL DEFAULT 'active';
