-- ============================================================
-- Auth migration: add password-based login support
-- Run AFTER 01_schema.sql and 02_seed.sql
-- pgcrypto is already enabled by 01_schema.sql
-- ============================================================

ALTER TABLE app_user
  ADD COLUMN IF NOT EXISTS password_hash        TEXT,
  ADD COLUMN IF NOT EXISTS reset_token          TEXT,
  ADD COLUMN IF NOT EXISTS reset_token_expires  TIMESTAMPTZ;

-- Set default password "Egnex@2026" for all existing users.
-- crypt() with 'bf' produces a bcrypt $2a$ hash compatible with
-- Python's passlib/bcrypt verify().
UPDATE app_user
SET password_hash = crypt('Egnex@2026', gen_salt('bf', 10))
WHERE password_hash IS NULL;
