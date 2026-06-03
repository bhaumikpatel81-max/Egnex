-- ============================================================
-- ONE CLICK HIRE  -  Auth layer (Phase 2 add-on)
-- Adds password authentication and browser sessions.
-- Uses pgcrypto bcrypt (already enabled by 01_schema.sql).
-- Run AFTER 01_schema.sql..05_google_oauth.sql.
-- ============================================================

ALTER TABLE app_user ADD COLUMN IF NOT EXISTS password_hash TEXT;

CREATE TABLE IF NOT EXISTS user_session (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '8 hours'
);

CREATE INDEX IF NOT EXISTS idx_session_token ON user_session(token_hash);
CREATE INDEX IF NOT EXISTS idx_session_user  ON user_session(user_id);

-- Default password for all seed users: egnex2025
-- Uses bcrypt via pgcrypto (already available).
UPDATE app_user
SET password_hash = crypt('egnex2025', gen_salt('bf', 8))
WHERE password_hash IS NULL;
