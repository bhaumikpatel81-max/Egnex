-- Migration 36 (auto-migrate slot 36)
-- Widens cv_repository.source CHECK to include 'application'
-- so resumes submitted via the apply endpoint can be tracked in the CV Repository.
-- Also seeds new system_settings defaults (about_company_text, auto_jd_email).
-- NOTE: SQL files in database/ are documentation only — actual execution happens
--       via the inline auto-migrate list in backend/app/main.py.
--
-- Relationship between file numbers and auto-migrate slot numbers:
--   Files 01-15 = schema/seed files executed at DB setup time (outside main.py).
--   Files 16-28 = documentation mirrors of auto-migrate slots 16-35.
--   File numbers are sequential by creation order; slot numbers by list position.
--   File 28 (hm_requisition_approval) = slot 34.  No 1:1 mapping — intentional.

DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'cv_repository'::regclass
               AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%source%'
    LOOP
        EXECUTE 'ALTER TABLE cv_repository DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$;

ALTER TABLE cv_repository ADD CONSTRAINT cv_repository_source_check
    CHECK (source IN ('bulk_folder','upload','watcher','email','application'));

-- Seed new settings (idempotent)
INSERT INTO system_settings (key, value)
VALUES
    ('about_company_text', 'Amnex Infotechnologies Pvt. Ltd. is a leading technology company specialising in smart city, public safety, and e-governance solutions.'),
    ('auto_jd_email', 'true')
ON CONFLICT (key) DO NOTHING;
