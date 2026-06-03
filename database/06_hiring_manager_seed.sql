-- ============================================================
-- Migration 06: Seed a hiring_manager user
-- Run AFTER 01–05 migrations.
-- The hiring_manager role already exists in the app_user CHECK
-- constraint (01_schema.sql) and in the backend _VALID_ROLES set.
-- This file only adds the sample user + sets their password.
-- ============================================================

INSERT INTO app_user (full_name, email, role, password_hash)
VALUES (
    'Hiring Manager',
    'hiring.manager@amnex.com',
    'hiring_manager',
    crypt('Egnex@2026', gen_salt('bf', 10))
)
ON CONFLICT (email) DO NOTHING;
