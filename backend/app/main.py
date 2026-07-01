"""
One Click Hire -- FastAPI backend (prototype).

Binds to 0.0.0.0 and reads PORT from the environment, per the deployment
prerequisites. Serves a JSON API plus a simple bundled frontend so the whole
pipeline can be demonstrated end to end.
"""
import os
import re as _re
import uuid as _uuid
import json
from datetime import datetime, timedelta
from pathlib import Path

# Load .env.prod at startup — set all credentials there, never commit real passwords
_ROOT = Path(__file__).resolve().parents[2]   # egnex/
_env_file = _ROOT / ".env.prod"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file, override=False)
    print(f"[config] Loaded env from {_env_file.name}")

from fastapi import FastAPI, HTTPException, Request, File, UploadFile, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import psycopg2

from .db import query, query_one
from .services import pipeline, connectors
from .services.resume_parser import extract_text as _parse_resume
from .routers.auth import router as _auth_router
from .routers.admin_users import router as _admin_router
from .routers.pipeline_api import router as _pipeline_router
from .routers.reports_api import router as _reports2_router
from .routers.nexai_api import router as _nexai_router
from .routers.proctoring_api import router as _proctoring_router
from .routers.tickets_api import router as _tickets_router
from .routers.scorecard_api import router as _scorecard_router
from .routers.email_template_api import router as _email_template_router
from .routers.offers_api import router as _offers_router
from .routers.sla_api import router as _sla_router
from .routers.chain_templates_api import router as _chain_templates_router
from .routers.documentation_api import router as _documentation_router
from .routers.kpi_api import router as _kpi_router
from .routers.hiring_plan_api import router as _hiring_plan_router
from .routers.cv_api import router as _cv_router
from .routers.hm_api import router as _hm_router
from .routers.campus_bulk_api import router as _campus_router
from .routers.password_api import router as _password_router
from .routers.vendor_api import router as _vendor_router
from .routers.candidate_portal_api import router as _candidate_portal_router
from .routers.gamification_api import router as _gamification_router
from .routers.bands_api import router as _bands_router
from .routers.cv_api import ingest_and_link as _cv_ingest_and_link
from .auth_utils import _decode

app = FastAPI(title="Egnex API", version="0.1.0")
app.include_router(_auth_router)
app.include_router(_admin_router)
app.include_router(_password_router)
app.include_router(_pipeline_router)
app.include_router(_reports2_router)
app.include_router(_nexai_router)
app.include_router(_proctoring_router)
app.include_router(_tickets_router)
app.include_router(_scorecard_router)
app.include_router(_email_template_router)
app.include_router(_offers_router)
app.include_router(_sla_router)
app.include_router(_chain_templates_router)
app.include_router(_documentation_router)
app.include_router(_kpi_router)
app.include_router(_hiring_plan_router)
app.include_router(_cv_router)
app.include_router(_hm_router)
app.include_router(_campus_router)
app.include_router(_vendor_router)
app.include_router(_candidate_portal_router)
app.include_router(_gamification_router)
app.include_router(_bands_router)


@app.on_event("startup")
def _auto_migrate():
    """
    Idempotent migrations — run on every startup so developers never need
    to manually execute SQL files after pulling new code.
    Each statement is safe to re-run (uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).
    """
    from .db import query
    migrations = [
        # NexAI candidate invite tokens (added 2026-06)
        """CREATE TABLE IF NOT EXISTS nexai_invite (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            application_id UUID NOT NULL REFERENCES application(id) ON DELETE CASCADE,
            token          TEXT NOT NULL UNIQUE,
            invited_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at     TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '7 days',
            used_at        TIMESTAMPTZ,
            created_by     UUID REFERENCES app_user(id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_nexai_invite_token ON nexai_invite (token)",
        # CTC split columns (added 2026-06)
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS budgeted_fixed    NUMERIC",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS budgeted_variable NUMERIC",
        # System settings — admin-configurable key/value store (added 2026-06)
        """CREATE TABLE IF NOT EXISTS system_settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by UUID REFERENCES app_user(id)
        )""",
        # Avatar pre-render pipeline — per-question video tracking (added 2026-06 Step 4)
        "ALTER TABLE nexai_session ADD COLUMN IF NOT EXISTS question_videos JSONB NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE nexai_session ADD COLUMN IF NOT EXISTS render_status TEXT NOT NULL DEFAULT 'pending' CHECK (render_status IN ('pending','rendering','ready','partial','failed'))",
        """CREATE TABLE IF NOT EXISTS avatar_video_cache (
            cache_key   TEXT        PRIMARY KEY,
            gcs_url     TEXT        NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        # Per-requisition NexAI question editor (added 2026-06 Step 7)
        """CREATE TABLE IF NOT EXISTS requisition_questions (
            requisition_id  UUID        PRIMARY KEY
                                        REFERENCES requisition(id) ON DELETE CASCADE,
            questions       JSONB       NOT NULL DEFAULT '[]'::jsonb,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by      UUID        REFERENCES app_user(id)
        )""",
        # De-duplicate nexai_invite — keep only the latest invite per application (added 2026-06)
        """DELETE FROM nexai_invite
           WHERE id NOT IN (
               SELECT DISTINCT ON (application_id) id
               FROM nexai_invite
               ORDER BY application_id, invited_at DESC
           )""",
        # Migration 16: conversational interview turn history (added 2026-06)
        "ALTER TABLE nexai_session ADD COLUMN IF NOT EXISTS conversation JSONB",
        # Migration 17: unique index on candidate email (added 2026-06)
        "CREATE UNIQUE INDEX IF NOT EXISTS uidx_candidate_email ON candidate (LOWER(email))",
        # Migration 18: proctoring completion flag (added 2026-06)
        "ALTER TABLE proctoring_session ADD COLUMN IF NOT EXISTS proctoring_complete BOOLEAN NOT NULL DEFAULT FALSE",
        # Migration 19: email-sent guard to prevent duplicate completion emails (added 2026-06)
        "ALTER TABLE nexai_session ADD COLUMN IF NOT EXISTS email_sent BOOLEAN NOT NULL DEFAULT FALSE",
        # Migration 20: nexai_session terminated_proctoring status + termination_reason (added 2026-06)
        # Drop + recreate the status CHECK so it includes 'terminated_proctoring'
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'nexai_session'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE nexai_session DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE nexai_session ADD CONSTRAINT nexai_session_status_check
           CHECK (status IN ('pending','in_progress','completed','failed','terminated_proctoring'))""",
        "ALTER TABLE nexai_session ADD COLUMN IF NOT EXISTS termination_reason TEXT",
        # Migration 21: proctoring appeal workflow (added 2026-06)
        """CREATE TABLE IF NOT EXISTS proctoring_appeal (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            application_id        UUID NOT NULL REFERENCES application(id),
            nexai_session_id      UUID NOT NULL REFERENCES nexai_session(id),
            candidate_explanation TEXT NOT NULL,
            status                TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','reviewed','relink_sent','rejected')),
            recruiter_notes       TEXT,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            reviewed_by           UUID REFERENCES app_user(id),
            reviewed_at           TIMESTAMPTZ,
            UNIQUE (nexai_session_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_proctoring_appeal_application ON proctoring_appeal(application_id)",
        "CREATE INDEX IF NOT EXISTS idx_proctoring_appeal_status ON proctoring_appeal(status)",
        # Migration 22: real AI screening columns + stability dimension (added 2026-06)
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS ai_fit_score      NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS ai_screen_detail  JSONB",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS avg_tenure_months NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS stability_score   NUMERIC",
        """ALTER TABLE application ADD COLUMN IF NOT EXISTS stability_status TEXT
           CHECK (stability_status IS NULL
               OR stability_status IN ('computed','pending_manual','not_applicable'))""",
        # Migration 23: scorecard draft/submit workflow (added 2026-06)
        # NOTE: comment numbering was 24 before 23 historically; SQL is idempotent so order is irrelevant.
        "ALTER TABLE scorecard ALTER COLUMN submitted_at DROP NOT NULL",
        "ALTER TABLE scorecard ADD COLUMN IF NOT EXISTS status     TEXT        NOT NULL DEFAULT 'draft'",
        "ALTER TABLE scorecard ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "UPDATE scorecard SET status = 'submitted' WHERE submitted_at IS NOT NULL AND status = 'draft'",
        # Migration 24: extended application fields — employment snapshot + CTC (added 2026-06)
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS current_company       TEXT",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS current_designation   TEXT",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS current_location      TEXT",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS current_ctc_fixed     NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS current_ctc_variable  NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS current_ctc_bonus     NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS current_ctc_total     NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS expected_ctc_fixed    NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS expected_ctc_variable NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS expected_ctc_bonus    NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS expected_ctc_total    NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS notice_period_days    INTEGER",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS willing_to_relocate   BOOLEAN",
        # Migration 25: email template key + placeholder + editor columns (added 2026-06)
        "ALTER TABLE email_template ADD COLUMN IF NOT EXISTS template_key       TEXT",
        "ALTER TABLE email_template ADD COLUMN IF NOT EXISTS valid_placeholders JSONB",
        "ALTER TABLE email_template ADD COLUMN IF NOT EXISTS updated_by         UUID REFERENCES app_user(id)",
        # Migration 26: Offers & Approvals — per-requisition approval chains (added 2026-06)
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS bonus_ctc    NUMERIC",
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS designation  TEXT",
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS joining_date DATE",
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS notes        TEXT",
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS revise_note  TEXT",
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS darwin_ref   TEXT",
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS current_step INT  NOT NULL DEFAULT 1",
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS submitted_by UUID REFERENCES app_user(id)",
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMPTZ",
        "ALTER TABLE offer ADD COLUMN IF NOT EXISTS updated_at   TIMESTAMPTZ",
        # Widen offer.status — drop old CHECK and replace (name varies by Postgres)
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'offer'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE offer DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE offer ADD CONSTRAINT offer_status_check
           CHECK (status IN (
               'draft','pending_approval','approved','rejected',
               'revising','on_hold','cancelled','sent_to_darwinbox',
               'released','accepted','declined'
           ))""",
        # Widen application.status to include offer hold/cancel states
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'application'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE application DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE application ADD CONSTRAINT application_status_check
           CHECK (status IN (
               'applied','screening','screen_passed','screen_rejected',
               'interviewing','selected','rejected',
               'offer_stage','offered','offer_on_hold','offer_cancelled',
               'joined','dropped'
           ))""",
        # Per-requisition offer approval chain (user-specific ordered steps)
        """CREATE TABLE IF NOT EXISTS req_offer_approver (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            requisition_id  UUID NOT NULL REFERENCES requisition(id) ON DELETE CASCADE,
            approver_id     UUID NOT NULL REFERENCES app_user(id),
            sequence        INT  NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (requisition_id, sequence)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_req_offer_approver_req ON req_offer_approver(requisition_id)",
        # Offer approval step log (one row per step per offer)
        """CREATE TABLE IF NOT EXISTS offer_approval_step (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            offer_id    UUID NOT NULL REFERENCES offer(id) ON DELETE CASCADE,
            approver_id UUID NOT NULL REFERENCES app_user(id),
            sequence    INT  NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','approved','rejected','skipped')),
            notes       TEXT,
            acted_at    TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_offer_step_offer    ON offer_approval_step(offer_id)",
        "CREATE INDEX IF NOT EXISTS idx_offer_step_approver ON offer_approval_step(approver_id)",
        # Migration 27: Meeting Notetaker — interview transcript notes (added 2026-06)
        # Stores Drive file info, raw transcript, and Groq summary for each interview.
        """CREATE TABLE IF NOT EXISTS interview_notes (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            interview_id     UUID NOT NULL UNIQUE REFERENCES interview(id) ON DELETE CASCADE,
            application_id   UUID REFERENCES application(id) ON DELETE CASCADE,
            drive_file_id    TEXT,
            drive_file_name  TEXT,
            transcript_text  TEXT,
            summary          JSONB,
            fetch_status     TEXT NOT NULL DEFAULT 'none',
            fetch_error      TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_interview_notes_interview ON interview_notes(interview_id)",
        # Migration 28: SLA / RAG deadline tracking (added 2026-06)
        # Stores per-key SLA target in days; missing keys fall back to service-layer defaults.
        """CREATE TABLE IF NOT EXISTS sla_config (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            config_key  TEXT NOT NULL UNIQUE,
            days        INTEGER NOT NULL DEFAULT 5 CHECK (days >= 1),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by  UUID REFERENCES app_user(id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_sla_config_key ON sla_config (config_key)",
        # Migration 29: Named reusable approval chain templates + per-step SLA (added 2026-06)
        """CREATE TABLE IF NOT EXISTS offer_chain_template (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name        TEXT NOT NULL,
            description TEXT,
            is_active   BOOLEAN NOT NULL DEFAULT TRUE,
            created_by  UUID REFERENCES app_user(id),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS offer_chain_template_step (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            template_id UUID NOT NULL REFERENCES offer_chain_template(id) ON DELETE CASCADE,
            sequence    INT NOT NULL,
            approver_id UUID NOT NULL REFERENCES app_user(id),
            sla_days    INT NOT NULL DEFAULT 2 CHECK (sla_days >= 1),
            UNIQUE (template_id, sequence)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_oct_template ON offer_chain_template_step(template_id)",
        # Per-step SLA on existing approval tables
        "ALTER TABLE req_offer_approver   ADD COLUMN IF NOT EXISTS sla_days INT NOT NULL DEFAULT 2",
        "ALTER TABLE offer_approval_step  ADD COLUMN IF NOT EXISTS sla_days INT NOT NULL DEFAULT 2",
        # Migration 30: Recruitment Bifurcation — new pipeline stages + rich req fields (2026-06)
        # Step 1: Widen application.status CHECK to include all old + new values
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'application'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE application DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE application ADD CONSTRAINT application_status_check
           CHECK (status IN (
               'applied','ai_screening','nexai_bot','shortlisted','hm_screening',
               'panel_interview','hr_round','offer_approval','offered',
               'hired','rejected','on_hold',
               'screening','screen_passed','screen_rejected','interviewing','selected',
               'offer_stage','offer_on_hold','offer_cancelled','joined','dropped'
           ))""",
        # Step 2: Rename existing statuses to new pipeline stage names
        "UPDATE application SET status='ai_screening'    WHERE status='screening'",
        "UPDATE application SET status='shortlisted'     WHERE status='screen_passed'",
        "UPDATE application SET status='panel_interview' WHERE status='interviewing'",
        "UPDATE application SET status='hm_screening'    WHERE status='selected'",
        "UPDATE application SET status='offer_approval'  WHERE status='offer_stage'",
        "UPDATE application SET status='on_hold'         WHERE status='offer_on_hold'",
        "UPDATE application SET status='hired'           WHERE status='joined'",
        "UPDATE application SET status='rejected'        WHERE status IN ('screen_rejected','dropped','offer_cancelled')",
        # Step 3: Tighten CHECK to new names only (old names all migrated away)
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'application'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE application DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE application ADD CONSTRAINT application_status_check
           CHECK (status IN (
               'applied','ai_screening','nexai_bot','shortlisted','hm_screening',
               'panel_interview','hr_round','offer_approval','offered',
               'hired','rejected','on_hold'
           ))""",
        # Step 4: Rich requisition fields
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS req_code       TEXT UNIQUE",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS project        TEXT",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS grade_level    TEXT",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS max_experience NUMERIC",
        """ALTER TABLE requisition ADD COLUMN IF NOT EXISTS priority TEXT
           CHECK (priority IS NULL OR priority IN ('critical','high','medium','low'))""",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS source_channels TEXT[] NOT NULL DEFAULT '{}'",
        # Step 5: Auto-generate req_codes for existing requisitions without one
        """WITH numbered AS (
               SELECT id, 'REQ-' || LPAD(ROW_NUMBER() OVER (ORDER BY created_at)::text, 4, '0') AS code
               FROM requisition WHERE req_code IS NULL
           )
           UPDATE requisition SET req_code = numbered.code
           FROM numbered WHERE requisition.id = numbered.id""",
        # Step 6: Rename sla_config keys to match new stage names
        "UPDATE sla_config SET config_key='stage_ai_screening'    WHERE config_key='stage_screening'",
        "UPDATE sla_config SET config_key='stage_shortlisted'     WHERE config_key='stage_screen_passed'",
        "UPDATE sla_config SET config_key='stage_panel_interview' WHERE config_key='stage_interviewing'",
        "UPDATE sla_config SET config_key='stage_hm_screening'    WHERE config_key='stage_selected'",
        "UPDATE sla_config SET config_key='stage_offer_approval'  WHERE config_key='stage_offer_stage'",
        # ── Migration 31: Correct pipeline names to Amnex real flow ──────────────────
        # Step 1: Widen CHECK constraint to include both old and new stage names
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'application'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE application DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE application ADD CONSTRAINT application_status_check
           CHECK (status IN (
               'applied','screen','nexai_bot','shortlisted','interview','documentation','offered',
               'hired','rejected','on_hold',
               'ai_screening','hm_screening','panel_interview','hr_round','offer_approval'
           ))""",
        # Step 2: Rename statuses to corrected pipeline stage names
        "UPDATE application SET status='screen'        WHERE status='ai_screening'",
        "UPDATE application SET status='interview'     WHERE status IN ('hm_screening','panel_interview','hr_round')",
        "UPDATE application SET status='documentation' WHERE status='offer_approval'",
        # Step 3: Tighten CHECK to final stage names only
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'application'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE 'ALTER TABLE application DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """ALTER TABLE application ADD CONSTRAINT application_status_check
           CHECK (status IN (
               'applied','screen','nexai_bot','shortlisted','interview','documentation','offered',
               'hired','rejected','on_hold'
           ))""",
        # Step 4: Screening decision fields on application
        """ALTER TABLE application ADD COLUMN IF NOT EXISTS screening_decision TEXT
           CHECK (screening_decision IS NULL OR screening_decision IN ('pass','hold','reject'))""",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS screening_notes TEXT",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS screened_by UUID REFERENCES app_user(id)",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS screened_at TIMESTAMPTZ",
        # Step 5: Document collection table
        """CREATE TABLE IF NOT EXISTS application_document (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            application_id UUID NOT NULL REFERENCES application(id) ON DELETE CASCADE,
            file_name      TEXT NOT NULL,
            file_path      TEXT NOT NULL,
            doc_type       TEXT NOT NULL DEFAULT 'general',
            uploaded_by    UUID REFERENCES app_user(id),
            uploaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            notes          TEXT
        )""",
        # Step 6: Negotiation log table
        """CREATE TABLE IF NOT EXISTS negotiation_log (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            application_id UUID NOT NULL REFERENCES application(id) ON DELETE CASCADE,
            note           TEXT NOT NULL,
            stage_detail   TEXT,
            logged_by      UUID REFERENCES app_user(id),
            logged_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        # Step 7: Reseed sla_config with corrected key names
        "DELETE FROM sla_config WHERE config_key IN ('stage_ai_screening','stage_hm_screening','stage_panel_interview','stage_hr_round','stage_offer_approval')",
        "INSERT INTO sla_config (config_key, days) VALUES ('stage_screen',3),('stage_interview',5),('stage_documentation',5) ON CONFLICT (config_key) DO NOTHING",
        # ── Migration 32: Hiring Plan rows table ─────────────────────────────────
        """CREATE TABLE IF NOT EXISTS hiring_plan_rows (
            id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            fiscal_year             TEXT,
            quarter                 TEXT,
            company_entity          TEXT,
            finance_onboarding_date DATE,
            planned_onboarding_date DATE,
            requisition_id          UUID REFERENCES requisition(id) ON DELETE SET NULL,
            link_status             TEXT NOT NULL DEFAULT 'unlinked'
                                    CHECK (link_status IN ('unlinked','suggested','confirmed')),
            role_name               TEXT,
            bu                      TEXT,
            function                TEXT,
            sub_bu                  TEXT,
            project_name            TEXT,
            employment_type         TEXT,
            billable                TEXT,
            sow_received            TEXT,
            capex_opex              TEXT,
            capex_opex_on_track     TEXT,
            on_off_roll             TEXT,
            headcount               INT  NOT NULL DEFAULT 1,
            priority                TEXT,
            band                    TEXT,
            experience              TEXT,
            market_salary_range     TEXT,
            location                TEXT,
            budgeted_fixed          NUMERIC NOT NULL DEFAULT 0,
            budgeted_variable       NUMERIC NOT NULL DEFAULT 0,
            asset                   TEXT,
            salary_budgeted_till    DATE,
            hiring_status           TEXT NOT NULL DEFAULT 'Open Position'
                                    CHECK (hiring_status IN (
                                        'Open Position','Offered','Joined','Hold','Internal Employee'
                                    )),
            replacement_for         TEXT,
            aipl_code               TEXT,
            employee_name           TEXT,
            offered_fixed           NUMERIC NOT NULL DEFAULT 0,
            offered_variable        NUMERIC NOT NULL DEFAULT 0,
            ta_owner                TEXT,
            source_of_hire          TEXT,
            candidate_email         TEXT,
            offer_date              DATE,
            tentative_doj           DATE,
            remarks                 TEXT,
            created_by              UUID REFERENCES app_user(id),
            created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_hp_rows_fy     ON hiring_plan_rows(fiscal_year)",
        "CREATE INDEX IF NOT EXISTS idx_hp_rows_bu     ON hiring_plan_rows(bu)",
        "CREATE INDEX IF NOT EXISTS idx_hp_rows_req    ON hiring_plan_rows(requisition_id)",
        "CREATE INDEX IF NOT EXISTS idx_hp_rows_status ON hiring_plan_rows(hiring_status)",
        # ── Migration 33: CV Repository ──────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS cv_repository (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            file_name        TEXT NOT NULL,
            file_path        TEXT,
            file_hash        TEXT UNIQUE,
            file_ext         TEXT,
            candidate_name   TEXT,
            candidate_id     UUID REFERENCES candidate(id) ON DELETE SET NULL,
            requisition_id   UUID REFERENCES requisition(id) ON DELETE SET NULL,
            map_status       TEXT NOT NULL DEFAULT 'pool'
                             CHECK (map_status IN ('pool','mapped')),
            raw_text         TEXT,
            text_vector      tsvector,
            skills           TEXT[],
            enrich_status    TEXT NOT NULL DEFAULT 'pending'
                             CHECK (enrich_status IN ('pending','done','failed')),
            experience_years NUMERIC,
            current_position TEXT,
            location         TEXT,
            ai_summary       TEXT,
            source           TEXT NOT NULL DEFAULT 'upload'
                             CHECK (source IN ('bulk_folder','upload','watcher','email')),
            uploaded_by      UUID REFERENCES app_user(id) ON DELETE SET NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            enriched_at      TIMESTAMPTZ
        )""",
        "CREATE INDEX IF NOT EXISTS idx_cv_text_vector ON cv_repository USING GIN(text_vector)",
        "CREATE INDEX IF NOT EXISTS idx_cv_skills      ON cv_repository USING GIN(skills)",
        "CREATE INDEX IF NOT EXISTS idx_cv_candidate   ON cv_repository(candidate_id)",
        "CREATE INDEX IF NOT EXISTS idx_cv_hash        ON cv_repository(file_hash)",
        """CREATE TABLE IF NOT EXISTS cv_ingest_jobs (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            status      TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running','done','failed')),
            total       INT NOT NULL DEFAULT 0,
            processed   INT NOT NULL DEFAULT 0,
            mapped      INT NOT NULL DEFAULT 0,
            pooled      INT NOT NULL DEFAULT 0,
            duplicates  INT NOT NULL DEFAULT 0,
            errors      JSONB NOT NULL DEFAULT '[]',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS api_token TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_api_token ON app_user(api_token) WHERE api_token IS NOT NULL",
        "ALTER TABLE candidate ADD COLUMN IF NOT EXISTS cv_repository_id UUID REFERENCES cv_repository(id) ON DELETE SET NULL",
        # ── Migration 34: HM Requisition Approval Workflow (added 2026-06) ───────
        # Adds approval_status + created_by_role to requisition.
        # Existing rows default to 'approved' — zero behaviour change.
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS approval_status TEXT DEFAULT 'approved'",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS created_by_role TEXT",
        """DO $$
DECLARE r RECORD;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'requisition'::regclass
          AND conname  = 'requisition_approval_status_check'
    ) THEN
        EXECUTE $sql$
            ALTER TABLE requisition ADD CONSTRAINT requisition_approval_status_check
            CHECK (approval_status IN ('approved','pending_ta_approval','rejected'))
        $sql$;
    END IF;
END $$""",
        "CREATE INDEX IF NOT EXISTS idx_req_approval_status ON requisition (approval_status)",
        # ── Migration 35: is_builtin flag on email_template (added 2026-06) ─────
        "ALTER TABLE email_template ADD COLUMN IF NOT EXISTS is_builtin BOOLEAN NOT NULL DEFAULT FALSE",
        # ── Migration 36: widen cv_repository.source CHECK to add 'application' ─
        """DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'cv_repository'::regclass
               AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%source%'
    LOOP
        EXECUTE 'ALTER TABLE cv_repository DROP CONSTRAINT ' || quote_ident(r.conname);
    END LOOP;
END $$""",
        """DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'cv_repository'::regclass
          AND conname = 'cv_repository_source_check'
    ) THEN
        EXECUTE $sql$
            ALTER TABLE cv_repository ADD CONSTRAINT cv_repository_source_check
            CHECK (source IN ('bulk_folder','upload','watcher','email','application'))
        $sql$;
    END IF;
END $$""",
        # Seed new settings defaults (idempotent — ON CONFLICT preserves existing values)
        """INSERT INTO system_settings (key, value)
           VALUES
             ('about_company_text', 'About Amnex: [Configure in Settings]'),
             ('auto_jd_email', 'true')
           ON CONFLICT (key) DO NOTHING""",

        # ── Migration 37: seed company_name + ta_default_signature settings ───────
        """INSERT INTO system_settings (key, value)
           VALUES
             ('company_name',          'Amnex Infotechnologies Pvt. Ltd.'),
             ('ta_default_signature',  'Talent Acquisition Team')
           ON CONFLICT (key) DO NOTHING""",

        # ── Migration 38: guarantee final application.status constraint ──────────
        # Drops the old constraint first (safe with IF EXISTS), migrates any
        # remaining rows that still carry legacy status names, then re-adds the
        # final constraint. Runs atomically so it cannot be left half-applied.
        """DO $$
BEGIN
    ALTER TABLE application DROP CONSTRAINT IF EXISTS application_status_check;
    UPDATE application SET status='screen'        WHERE status IN ('screening','ai_screening');
    UPDATE application SET status='shortlisted'   WHERE status='screen_passed';
    UPDATE application SET status='interview'     WHERE status IN ('interviewing','hm_screening','panel_interview','hr_round');
    UPDATE application SET status='documentation' WHERE status='offer_approval';
    UPDATE application SET status='rejected'      WHERE status IN ('screen_rejected','dropped','offer_cancelled');
    UPDATE application SET status='on_hold'       WHERE status='offer_on_hold';
    UPDATE application SET status='hired'         WHERE status='joined';
    ALTER TABLE application ADD CONSTRAINT application_status_check
        CHECK (status IN (
            'applied','screen','nexai_bot','shortlisted','interview',
            'documentation','offered','hired','rejected','on_hold'
        ));
END $$""",

        # ── Migration 39: per-requisition scoring weights + fresher role flag ───
        # resume_weight + interview_weight control the combined-score blend.
        # is_fresher_role forces the fresher scoring model for campus roles.
        # panel_consensus stores the computed verdict badge directly on application
        # so list queries can surface it without parsing score_breakdown JSONB.
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS resume_weight    NUMERIC(4,2) DEFAULT 0.40",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS interview_weight NUMERIC(4,2) DEFAULT 0.60",
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS is_fresher_role  BOOLEAN      DEFAULT FALSE",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS panel_consensus  TEXT",
        """DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'application'::regclass
          AND conname  = 'application_panel_consensus_check'
    ) THEN
        EXECUTE $sql$
            ALTER TABLE application ADD CONSTRAINT application_panel_consensus_check
            CHECK (panel_consensus IS NULL OR panel_consensus IN ('advance','reject','split'))
        $sql$;
    END IF;
END $$""",

        # ── Migration 40: Campus Bulk Upload — batch invite for freshers / campus drives ──
        """CREATE TABLE IF NOT EXISTS campus_upload_batch (
            id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            requisition_id  UUID        REFERENCES requisition(id),
            uploaded_by     UUID        REFERENCES app_user(id),
            file_name       TEXT,
            total_rows      INTEGER,
            selected_count  INTEGER     NOT NULL DEFAULT 0,
            invited_count   INTEGER     NOT NULL DEFAULT 0,
            status          TEXT        NOT NULL DEFAULT 'draft'
                            CHECK (status IN ('draft','invites_sent','completed')),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_campus_batch_req ON campus_upload_batch(requisition_id)",
        """CREATE TABLE IF NOT EXISTS campus_candidate (
            id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            batch_id         UUID        REFERENCES campus_upload_batch(id),
            requisition_id   UUID        REFERENCES requisition(id),
            name             TEXT,
            email            TEXT,
            phone            TEXT,
            college          TEXT,
            branch           TEXT,
            cgpa             NUMERIC(4,2),
            graduation_year  INTEGER,
            extra_data       JSONB       NOT NULL DEFAULT '{}'::jsonb,
            invite_status    TEXT        NOT NULL DEFAULT 'pending'
                             CHECK (invite_status IN ('pending','invite_queued','invited','interview_started','completed')),
            invite_sent_at   TIMESTAMPTZ,
            nexai_session_id TEXT,
            application_id   UUID        REFERENCES application(id),
            resume_uploaded  BOOLEAN     NOT NULL DEFAULT FALSE,
            resume_url       TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_campus_cand_batch ON campus_candidate(batch_id)",
        "CREATE INDEX IF NOT EXISTS idx_campus_cand_app   ON campus_candidate(application_id)",

        # ── Password reset / first-time set-password tokens ─────────────────────
        # Must be created BEFORE Migration 41 which ALTERs this table.
        """CREATE TABLE IF NOT EXISTS password_reset_token (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
            token_hash  TEXT NOT NULL UNIQUE,
            purpose     TEXT NOT NULL DEFAULT 'reset'
                        CHECK (purpose IN ('reset','invite')),
            expires_at  TIMESTAMPTZ NOT NULL,
            used_at     TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_prt_user ON password_reset_token(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_prt_hash ON password_reset_token(token_hash)",

        # ── Migration 41: Vendor Management ──────────────────────────────────────────
        # Extend password_reset_token with account_type so one token table serves
        # staff, vendor, and candidate logins.  The FK on user_id is dropped so
        # vendor_user / candidate_user IDs can be stored in the same column.
        # Approach chosen: minimal change — no extra columns, no new token tables.
        """DO $$
DECLARE r RECORD;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'password_reset_token' AND column_name = 'account_type'
    ) THEN
        FOR r IN SELECT conname FROM pg_constraint
                 WHERE conrelid = 'password_reset_token'::regclass AND contype = 'f'
        LOOP
            EXECUTE 'ALTER TABLE password_reset_token DROP CONSTRAINT ' || quote_ident(r.conname);
        END LOOP;
        ALTER TABLE password_reset_token ALTER COLUMN user_id DROP NOT NULL;
        ALTER TABLE password_reset_token
            ADD COLUMN account_type TEXT NOT NULL DEFAULT 'staff'
            CHECK (account_type IN ('staff','vendor','candidate'));
    END IF;
END $$""",
        # Vendor (partner / sourcing company)
        """CREATE TABLE IF NOT EXISTS vendor (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name           TEXT NOT NULL,
            contact_email  TEXT,
            contact_phone  TEXT,
            status         TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active','suspended')),
            created_by     UUID REFERENCES app_user(id),
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        # Vendor user accounts (mirrors app_user auth columns)
        """CREATE TABLE IF NOT EXISTS vendor_user (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            vendor_id      UUID NOT NULL REFERENCES vendor(id) ON DELETE CASCADE,
            full_name      TEXT NOT NULL,
            email          TEXT NOT NULL UNIQUE,
            password_hash  TEXT,
            is_active      BOOLEAN NOT NULL DEFAULT TRUE,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_vendor_user_vendor ON vendor_user(vendor_id)",
        "CREATE INDEX IF NOT EXISTS idx_vendor_user_email  ON vendor_user(LOWER(email))",
        # Requisition ↔ vendor access
        """CREATE TABLE IF NOT EXISTS requisition_vendor (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            requisition_id  UUID NOT NULL REFERENCES requisition(id) ON DELETE CASCADE,
            vendor_id       UUID NOT NULL REFERENCES vendor(id) ON DELETE CASCADE,
            opened_by       UUID REFERENCES app_user(id),
            opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (requisition_id, vendor_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_req_vendor_req    ON requisition_vendor(requisition_id)",
        "CREATE INDEX IF NOT EXISTS idx_req_vendor_vendor ON requisition_vendor(vendor_id)",
        # application.source — 'vendor:<uuid>' for vendor submissions
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'direct'",

        # ── Migration 42: Candidate Portal ───────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS candidate_user (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            candidate_id   UUID NOT NULL UNIQUE REFERENCES candidate(id) ON DELETE CASCADE,
            email          TEXT NOT NULL UNIQUE,
            password_hash  TEXT,
            is_active      BOOLEAN NOT NULL DEFAULT TRUE,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_cu_candidate ON candidate_user(candidate_id)",
        "CREATE INDEX IF NOT EXISTS idx_cu_email     ON candidate_user(LOWER(email))",
        """CREATE TABLE IF NOT EXISTS candidate_feedback (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            candidate_id     UUID NOT NULL REFERENCES candidate(id) ON DELETE CASCADE,
            application_id   UUID REFERENCES application(id) ON DELETE SET NULL,
            company_rating   SMALLINT NOT NULL CHECK (company_rating  BETWEEN 1 AND 5),
            interview_rating SMALLINT NOT NULL CHECK (interview_rating BETWEEN 1 AND 5),
            comments         TEXT,
            visible_to_ta    BOOLEAN NOT NULL DEFAULT TRUE,
            submitted_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_cfb_candidate   ON candidate_feedback(candidate_id)",
        "CREATE INDEX IF NOT EXISTS idx_cfb_application ON candidate_feedback(application_id)",

        # ── Migration 43: Gamification — criticality flag + ledger + config ──
        # Criticality on requisition (multiplies gamification points)
        "ALTER TABLE requisition ADD COLUMN IF NOT EXISTS criticality TEXT NOT NULL DEFAULT 'Medium' CHECK (criticality IN ('Low','Medium','High','Critical'))",
        # Append-only gamification ledger
        """CREATE TABLE IF NOT EXISTS gamification_event (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            subject_type   TEXT NOT NULL CHECK (subject_type IN ('recruiter','vendor','candidate','hm')),
            subject_id     UUID NOT NULL,
            event_type     TEXT NOT NULL,
            base_points    NUMERIC NOT NULL,
            criticality    TEXT NOT NULL DEFAULT 'Medium',
            multiplier     NUMERIC NOT NULL DEFAULT 1.0,
            points_awarded NUMERIC NOT NULL,
            requisition_id UUID REFERENCES requisition(id) ON DELETE SET NULL,
            application_id UUID REFERENCES application(id) ON DELETE SET NULL,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_gev_subject ON gamification_event(subject_type, subject_id)",
        "CREATE INDEX IF NOT EXISTS idx_gev_req     ON gamification_event(requisition_id)",
        "CREATE INDEX IF NOT EXISTS idx_gev_app     ON gamification_event(application_id)",
        "CREATE INDEX IF NOT EXISTS idx_gev_created ON gamification_event(created_at)",
        # Config table — editable by TA admin
        """CREATE TABLE IF NOT EXISTS gamification_config (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by UUID REFERENCES app_user(id)
        )""",
        # Seed base event points and multipliers (ON CONFLICT preserves existing tuned values)
        """INSERT INTO gamification_config (key, value) VALUES
             ('points.offer_within_sla',   '50'),
             ('points.fast_screen',        '20'),
             ('points.offer_accepted',     '80'),
             ('points.offer_joined',       '100'),
             ('points.panel_pass',         '30'),
             ('points.feedback_on_time',   '25'),
             ('sla.feedback_hours',        '48'),
             ('points.sla_met_stage',      '15'),
             ('points.submission',         '5'),
             ('points.candidate_advanced', '10'),
             ('multiplier.Low',            '1.0'),
             ('multiplier.Medium',         '1.5'),
             ('multiplier.High',           '2.5'),
             ('multiplier.Critical',       '4.0'),
             ('tier.bronze',               '0'),
             ('tier.silver',               '200'),
             ('tier.gold',                 '600'),
             ('tier.platinum',             '1500')
           ON CONFLICT (key) DO NOTHING""",
        # Named achievements
        """CREATE TABLE IF NOT EXISTS gamification_badge (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            subject_type TEXT NOT NULL CHECK (subject_type IN ('recruiter','vendor','candidate','hm')),
            subject_id   UUID NOT NULL,
            badge_key    TEXT NOT NULL,
            earned_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (subject_type, subject_id, badge_key)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_gbadge_subject ON gamification_badge(subject_type, subject_id)",
        # Migration 37: per-user Gmail App Password for individual email scanning (2026-07)
        "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS gmail_address      TEXT",
        "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS gmail_app_password TEXT",
    ]
    for sql in migrations:
        try:
            query(sql, fetch=False)
        except Exception as exc:
            # Log but don't crash — a failed migration shouldn't block startup
            print(f"[auto-migrate] WARNING: {exc}")

    # Seed built-in email template defaults (idempotent — skips existing rows)
    try:
        from .services.email_templates import ensure_defaults as _ensure_email_defaults, fix_legacy_templates as _fix_legacy_templates
        _ensure_email_defaults()
        _fix_legacy_templates()
    except Exception as _edt_exc:
        print(f"[auto-migrate] email template seed failed: {_edt_exc}")

    # Ensure CV store directory exists
    import os as _os
    _cv_store = _os.environ.get("CV_STORE_DIR", "/app/cv_store")
    _os.makedirs(_cv_store, exist_ok=True)
    _cv_inbox = _os.environ.get("CV_INBOX_DIR", "/app/cv_inbox")
    _os.makedirs(_cv_inbox, exist_ok=True)


@app.on_event("startup")
async def _start_background_services():
    """Start CV enricher and email ingest poller as background asyncio tasks."""
    import asyncio as _asyncio
    try:
        from .services.cv_enricher import start_enricher as _start_cv_enricher
        _asyncio.create_task(_start_cv_enricher())
    except Exception as exc:
        print(f"[startup] cv_enricher failed to start: {exc}")
    try:
        from .services.email_ingest import start_email_poller as _start_email_poller
        _asyncio.create_task(_start_email_poller())
    except Exception as exc:
        print(f"[startup] email_ingest failed to start: {exc}")

_UPLOADS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "uploads")
)
os.makedirs(_UPLOADS_DIR, exist_ok=True)

# Resolve the frontend directory.
_FRONTEND_DIR = os.environ.get(
    "FRONTEND_DIR",
    os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "frontend")
    ),
)
_ASSETS_DIR = os.path.join(_FRONTEND_DIR, "assets")
if os.path.isdir(_ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=_ASSETS_DIR), name="assets")

# Local avatar video storage — used when GCS_BUCKET_NAME is not set (dev / orb-only mode).
# Videos are written here by prerender.py and served at /media/avatar_videos/<filename>.
_AVATAR_MEDIA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "media", "avatar_videos")
)
os.makedirs(_AVATAR_MEDIA_DIR, exist_ok=True)
app.mount("/media/avatar_videos", StaticFiles(directory=_AVATAR_MEDIA_DIR), name="avatar_videos")

_RESUME_MIME = {
    ".pdf":  "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc":  "application/msword",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
}

# Paths that do NOT require a JWT
_PUBLIC = {
    "/", "/login", "/api/health", "/api/auth/login",
    "/set-password",
    "/api/auth/forgot-password",
    "/api/auth/reset-password",
    "/api/auth/reset-token/validate",
    "/nexai-interview",
    # Candidate-facing NexAI interview endpoints — token-based, no JWT
    "/api/nexai/invite/validate",
    "/api/nexai/invite/begin",
    # Vendor portal login — vendor receives JWT after this call
    "/api/vendors/portal/login",
    # Candidate portal login
    "/api/candidate/portal/login",
}
_PUBLIC_PREFIXES = (
    "/assets/",
    "/api/nexai/invite/submit/",       # /api/nexai/invite/submit/{session_id}
    "/api/proctoring/candidate/",      # candidate token-auth proctoring endpoints
    "/api/campus/session/",            # campus resume upload + is-campus check (token-auth, no JWT)
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    print(f"[MW] {request.method} {path}", flush=True)
    # Candidate-facing interview flow — always public, no JWT needed
    if path.startswith("/api/nexai/invite") or path == "/nexai-interview":
        print(f"[MW] PASS (nexai invite): {path}", flush=True)
        return await call_next(request)
    if path in _PUBLIC or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        print(f"[MW] PASS (public): {path}", flush=True)
        return await call_next(request)
    print(f"[MW] BLOCK: {path}", flush=True)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    try:
        request.state.user = _decode(auth[7:])
    except Exception:
        return JSONResponse(status_code=401, content={"detail": "Invalid or expired token"})
    return await call_next(request)


@app.exception_handler(psycopg2.Error)
def db_error_handler(request: Request, exc: psycopg2.Error):
    return JSONResponse(status_code=400,
                        content={"error": "database constraint or bad reference",
                                 "detail": str(exc).splitlines()[0]})


# ---------------- resume serving ----------------
@app.get("/api/resume/{filename}")
def serve_resume(filename: str, request: Request):
    """Authenticated endpoint to view or download a candidate resume."""
    role = request.state.user.get("role", "")
    if role not in ("admin", "ta_manager", "recruiter"):
        return JSONResponse(status_code=403, content={"detail": "Not authorised to view resumes"})

    # Prevent path traversal
    safe_name = os.path.basename(filename)
    if not safe_name or safe_name != filename:
        raise HTTPException(400, "Invalid filename")

    file_path = os.path.join(_UPLOADS_DIR, safe_name)
    if not os.path.isfile(file_path):
        # Fallback: new resumes are stored in cv_store (single canonical location)
        _cv_store_dir = os.environ.get("CV_STORE_DIR", "/app/cv_store")
        file_path = os.path.join(_cv_store_dir, safe_name)
        if not os.path.isfile(file_path):
            raise HTTPException(404, "Resume file not found")

    ext = os.path.splitext(safe_name)[1].lower()
    media_type = _RESUME_MIME.get(ext, "application/octet-stream")

    # PDFs open inline in the browser; other formats force download
    disposition = "inline" if ext == ".pdf" else "attachment"
    return FileResponse(
        file_path,
        media_type=media_type,
        headers={"Content-Disposition": f'{disposition}; filename="{safe_name}"'},
    )


# ---------------- health ----------------
@app.get("/api/health")
def health():
    try:
        query_one("SELECT 1 AS ok")
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "degraded", "error": str(e)})


# ---------------- reference / config ----------------
@app.get("/api/users")
def users():
    return query(
        "SELECT id, full_name, email, role FROM app_user WHERE is_active = true ORDER BY full_name"
    )


@app.get("/api/bands")
def bands():
    return query("SELECT id, code, rank, description, is_active FROM band ORDER BY rank")


@app.get("/api/group-companies")
def group_companies_list():
    return query("SELECT id, name, domain FROM group_company WHERE is_active = true ORDER BY name")


@app.get("/api/business-units")
def business_units(company_id: str = None):
    if company_id:
        return query(
            """SELECT bu.id, bu.name, gc.id AS company_id, gc.name AS company
               FROM business_unit bu JOIN group_company gc ON gc.id = bu.company_id
               WHERE bu.is_active = true AND gc.id = %s
               ORDER BY bu.name""",
            [company_id],
        )
    return query(
        """SELECT bu.id, bu.name, gc.id AS company_id, gc.name AS company
           FROM business_unit bu JOIN group_company gc ON gc.id = bu.company_id
           WHERE bu.is_active = true
           ORDER BY gc.name, bu.name"""
    )


@app.get("/api/requisitions")
def requisitions():
    # Exclude pending_ta_approval reqs from the public listing used by the apply modal
    return query(
        """SELECT r.id, r.title, r.status, r.roll_type, r.fiscal_year,
                  r.budgeted_ctc, b.code AS band, bu.name AS business_unit,
                  (SELECT COUNT(*) FROM application  WHERE requisition_id = r.id) AS in_pipeline,
                  (SELECT COUNT(*) FROM round_config WHERE requisition_id = r.id) AS levels
           FROM requisition r
           JOIN band b ON b.id = r.band_id
           JOIN business_unit bu ON bu.id = r.bu_id
           WHERE COALESCE(r.approval_status, 'approved') = 'approved'
           ORDER BY r.created_at DESC"""
    )


# ---------------- applications / pipeline ----------------
class ApplyIn(BaseModel):
    requisition_id: str
    full_name: str
    email: str
    phone: str | None = None
    gender: str = "undisclosed"
    resume_text: str = ""
    years_experience: float | None = None
    source: str = "career_site"
    # Extended informational fields — captured for recruiter context only.
    # These do NOT affect the screening score or any algorithm.
    current_company: str | None = None
    current_designation: str | None = None
    current_location: str | None = None
    current_ctc_fixed: float | None = None
    current_ctc_variable: float | None = None
    current_ctc_bonus: float | None = None
    expected_ctc_fixed: float | None = None
    expected_ctc_variable: float | None = None
    expected_ctc_bonus: float | None = None
    notice_period_days: int | None = None
    willing_to_relocate: bool | None = None


def _find_existing_candidate(email: str, phone: str | None):
    """
    Return an existing candidate row (id, full_name) if one matches by email
    OR by normalised phone number.  Returns None if no match found.
    """
    from .services.resume_parser import normalize_phone
    if email:
        row = query_one(
            "SELECT id, full_name FROM candidate WHERE lower(email) = %s",
            [email.lower()],
        )
        if row:
            return row, "email"
    norm = normalize_phone(phone) if phone else None
    if norm:
        row = query_one(
            """SELECT id, full_name FROM candidate
               WHERE regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g') = %s
               AND phone IS NOT NULL AND phone <> ''""",
            [norm],
        )
        if row:
            return row, "phone"
    return None, None


def _dedup_or_create_candidate(
    full_name: str, email: str, phone: str | None,
    gender: str, source: str, resume_url: str | None,
    requisition_id: str,
):
    """
    Look for an existing candidate by email / phone.
    - If found AND already applied to this req → raise 409.
    - If found but not yet applied → reuse the candidate, update resume if provided.
    - If not found → insert new candidate.
    Returns the candidate id.
    """
    existing, matched_by = _find_existing_candidate(email, phone)
    if existing:
        cand_id = existing["id"]
        dup_app = query_one(
            "SELECT id FROM application WHERE requisition_id = %s AND candidate_id = %s",
            [requisition_id, cand_id],
        )
        if dup_app:
            raise HTTPException(
                409,
                f"Candidate '{existing['full_name']}' has already applied to this "
                f"requisition (duplicate detected by {matched_by}).",
            )
        # Reuse candidate; update resume URL if a new file was provided
        if resume_url:
            query(
                "UPDATE candidate SET resume_url = %s WHERE id = %s",
                [resume_url, cand_id],
                fetch=False,
            )
        return cand_id

    # New candidate
    from .services.resume_parser import normalize_phone
    norm_phone = normalize_phone(phone) if phone else None
    row = query_one(
        """INSERT INTO candidate (full_name, email, phone, gender, source, resume_url)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        [full_name, email.lower(), norm_phone, gender, source, resume_url],
    )
    return row["id"]


def _sum_ctc(*parts):
    """Sum CTC components, returning None if all parts are None/zero."""
    total = sum(p for p in parts if p is not None)
    return total if total > 0 else None


def _parse_relocate(val) -> bool | None:
    """Convert FormData string ('yes'/'no'/'open'/'') to bool or None."""
    if isinstance(val, bool):
        return val
    if val in ("yes", "true", "1"):
        return True
    if val in ("no", "false", "0"):
        return False
    return None


def _maybe_issue_candidate_invite(cand_id: str, email: str, full_name: str) -> None:
    """
    If a candidate_user does not yet exist for this candidate, create one and
    send a set-password invite so they can access the portal.
    Safe to call multiple times — idempotent.
    """
    try:
        existing = query_one(
            "SELECT id FROM candidate_user WHERE candidate_id=%s", [cand_id]
        )
        if existing:
            return
        cu = query_one(
            """INSERT INTO candidate_user (candidate_id, email)
               VALUES (%s, %s) ON CONFLICT (email) DO NOTHING RETURNING id""",
            [cand_id, email.lower().strip()],
        )
        if cu:
            from .routers.password_api import issue_invite_for_external_user
            issue_invite_for_external_user(str(cu["id"]), email, full_name, "candidate")
    except Exception as exc:
        print(f"[candidate-portal] Auto-invite failed for {email}: {exc}")


def _send_jd_email(candidate_name: str, candidate_email: str, req_id: str) -> None:
    """
    Send the application_received_jd confirmation email.
    Failure is logged but never raises — must not fail the application.
    Respects the 'auto_jd_email' system setting toggle.
    """
    try:
        toggle_row = query_one(
            "SELECT value FROM system_settings WHERE key='auto_jd_email'", []
        )
        if toggle_row and (toggle_row.get("value") or "true").lower() not in ("true", "1", "yes"):
            return

        req = query_one(
            """SELECT title, hiring_location, min_experience, max_experience,
                      job_description, key_skills
               FROM requisition WHERE id=%s""",
            [req_id],
        )
        if not req:
            return

        about_row = query_one(
            "SELECT value FROM system_settings WHERE key='about_company_text'", []
        )
        about_company = (about_row or {}).get("value") or ""

        # Build human-readable experience string
        min_exp = req.get("min_experience")
        max_exp = req.get("max_experience")
        if min_exp is not None and max_exp is not None:
            experience = f"{int(min_exp)}–{int(max_exp)} years"
        elif min_exp is not None:
            experience = f"{int(min_exp)}+ years"
        else:
            experience = "Not specified"

        jd_raw = (req.get("job_description") or "").strip()

        from .services.email_templates import get_template
        from .services.connectors import send_email, resolve_global_placeholders

        globals_ = resolve_global_placeholders(req_id=req_id)
        reply_to = globals_.get("recruiter_email") or None

        tmpl = get_template("application_received_jd")
        subject = tmpl["subject"]
        body    = tmpl["body"]

        # Substitute placeholders, skipping jd_body gracefully if empty
        subs = {
            "candidate_name": candidate_name or "Candidate",
            "job_title":      req["title"] or "",
            "location":       req.get("hiring_location") or "India",
            "experience":     experience,
            "qualification":  "As per role requirements",
            "about_company":  about_company,
            **globals_,  # company_name, recruiter_name, recruiter_email
        }
        if jd_raw:
            subs["jd_body"] = jd_raw
        else:
            # Remove the jd_body line from body rather than sending a blank placeholder
            body = "\n".join(
                ln for ln in body.splitlines()
                if "{{jd_body}}" not in ln
            )
            subs["jd_body"] = ""  # won't appear after line removal

        for k, v in subs.items():
            subject = subject.replace("{{" + k + "}}", str(v))
            body    = body.replace("{{" + k + "}}", str(v))

        # Defensive sweep: strip any remaining unresolved {{placeholders}} so
        # raw template tokens never appear in a sent email.
        subject = _re.sub(r'\{\{[^}]+\}\}', '', subject)
        body    = _re.sub(r'\{\{[^}]+\}\}', '', body)

        send_email(candidate_email, subject, body, reply_to=reply_to)
    except Exception as exc:
        print(f"[jd-email] Failed to send JD confirmation to {candidate_email}: {exc}")


def _store_extended_fields(application_id: str, **kwargs):
    """
    Update the informational extended columns on an application row.
    CTC totals are auto-computed. Only non-None kwargs are written.
    Does NOT touch match_score or any screening column.
    """
    cols_vals = [
        ("current_company",       kwargs.get("current_company")),
        ("current_designation",   kwargs.get("current_designation")),
        ("current_location",      kwargs.get("current_location")),
        ("current_ctc_fixed",     kwargs.get("current_ctc_fixed")),
        ("current_ctc_variable",  kwargs.get("current_ctc_variable")),
        ("current_ctc_bonus",     kwargs.get("current_ctc_bonus")),
        ("current_ctc_total",     _sum_ctc(
            kwargs.get("current_ctc_fixed"),
            kwargs.get("current_ctc_variable"),
            kwargs.get("current_ctc_bonus"),
        )),
        ("expected_ctc_fixed",    kwargs.get("expected_ctc_fixed")),
        ("expected_ctc_variable", kwargs.get("expected_ctc_variable")),
        ("expected_ctc_bonus",    kwargs.get("expected_ctc_bonus")),
        ("expected_ctc_total",    _sum_ctc(
            kwargs.get("expected_ctc_fixed"),
            kwargs.get("expected_ctc_variable"),
            kwargs.get("expected_ctc_bonus"),
        )),
        ("notice_period_days",    kwargs.get("notice_period_days")),
        ("willing_to_relocate",   kwargs.get("willing_to_relocate")),
    ]
    provided = [(col, val) for col, val in cols_vals if val is not None]
    if not provided:
        return
    sets = ", ".join(f"{col} = %s" for col, _ in provided)
    vals = [val for _, val in provided] + [application_id]
    query(f"UPDATE application SET {sets} WHERE id = %s", vals, fetch=False)


@app.post("/api/apply")
def apply(payload: ApplyIn):
    """Text-paste application: create/reuse candidate → auto-screen."""
    _req_approval = query_one(
        "SELECT approval_status FROM requisition WHERE id=%s", [payload.requisition_id]
    )
    if _req_approval and (_req_approval.get("approval_status") or "approved") != "approved":
        raise HTTPException(403, "This requisition is not open for applications yet.")
    cand_id = _dedup_or_create_candidate(
        full_name=payload.full_name,
        email=payload.email,
        phone=payload.phone,
        gender=payload.gender,
        source=payload.source,
        resume_url=None,
        requisition_id=payload.requisition_id,
    )
    app_row = pipeline.intake_and_screen(
        payload.requisition_id, cand_id, payload.resume_text, payload.years_experience
    )
    _store_extended_fields(
        app_row["id"],
        current_company=payload.current_company,
        current_designation=payload.current_designation,
        current_location=payload.current_location,
        current_ctc_fixed=payload.current_ctc_fixed,
        current_ctc_variable=payload.current_ctc_variable,
        current_ctc_bonus=payload.current_ctc_bonus,
        expected_ctc_fixed=payload.expected_ctc_fixed,
        expected_ctc_variable=payload.expected_ctc_variable,
        expected_ctc_bonus=payload.expected_ctc_bonus,
        notice_period_days=payload.notice_period_days,
        willing_to_relocate=payload.willing_to_relocate,
    )
    _send_jd_email(payload.full_name, payload.email, payload.requisition_id)
    _maybe_issue_candidate_invite(cand_id, payload.email, payload.full_name)
    return {"application_id": app_row["id"], "match_score": app_row["match_score"],
            "breakdown": app_row["score_breakdown"]}


_ALLOWED_RESUME_TYPES = {".pdf", ".docx", ".doc", ".jpg", ".jpeg", ".png"}


@app.post("/api/parse-resume-contact")
async def parse_resume_contact(file: UploadFile = File(...)):
    """Parse a resume file and return extracted contact info for form pre-fill."""
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix not in _ALLOWED_RESUME_TYPES:
        raise HTTPException(400, f"Unsupported file type '{suffix}'.")
    file_bytes = await file.read()
    try:
        text, _ = _parse_resume(file_bytes, file.filename or "")
    except NotImplementedError:
        raise HTTPException(422, "Image files are not supported as resumes; upload PDF or Word.")
    from .services.resume_parser import extract_contact_info
    return extract_contact_info(text)


@app.post("/api/apply/upload")
async def apply_upload(
    requisition_id: str = Form(...),
    full_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    gender: str = Form("undisclosed"),
    years_experience: float = Form(None),
    source: str = Form("career_site"),
    # Extended informational fields — not used in screening
    current_company: str = Form(""),
    current_designation: str = Form(""),
    current_location: str = Form(""),
    current_ctc_fixed: float = Form(None),
    current_ctc_variable: float = Form(None),
    current_ctc_bonus: float = Form(None),
    expected_ctc_fixed: float = Form(None),
    expected_ctc_variable: float = Form(None),
    expected_ctc_bonus: float = Form(None),
    notice_period_days: int = Form(None),
    willing_to_relocate: str = Form(""),
    file: UploadFile = File(...),
):
    """File-upload path: extract text from PDF/Word, dedup check, then auto-screen."""
    _req_approval_u = query_one(
        "SELECT approval_status FROM requisition WHERE id=%s", [requisition_id]
    )
    if _req_approval_u and (_req_approval_u.get("approval_status") or "approved") != "approved":
        raise HTTPException(403, "This requisition is not open for applications yet.")
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix not in _ALLOWED_RESUME_TYPES:
        raise HTTPException(
            400, f"Unsupported file type '{suffix or 'none'}'. Upload a PDF or Word document."
        )

    file_bytes = await file.read()
    try:
        resume_text, warning = _parse_resume(file_bytes, file.filename or "")
    except NotImplementedError:
        raise HTTPException(422, "Image files are not supported as resumes; upload PDF or Word.")

    # No save to _UPLOADS_DIR — cv_store is the single canonical location.
    # Candidate is created first (needed for ingest), resume_url updated after.
    cand_id = _dedup_or_create_candidate(
        full_name=full_name,
        email=email,
        phone=phone or None,
        gender=gender,
        source=source,
        resume_url=None,
        requisition_id=requisition_id,
    )
    app_row = pipeline.intake_and_screen(
        requisition_id, cand_id, resume_text, years_experience, len(file_bytes)
    )
    _store_extended_fields(
        app_row["id"],
        current_company=current_company or None,
        current_designation=current_designation or None,
        current_location=current_location or None,
        current_ctc_fixed=current_ctc_fixed,
        current_ctc_variable=current_ctc_variable,
        current_ctc_bonus=current_ctc_bonus,
        expected_ctc_fixed=expected_ctc_fixed,
        expected_ctc_variable=expected_ctc_variable,
        expected_ctc_bonus=expected_ctc_bonus,
        notice_period_days=notice_period_days,
        willing_to_relocate=_parse_relocate(willing_to_relocate),
    )
    # Ingest into CV Repository (single canonical file in cv_store, hash-deduped).
    # After ingest, point candidate.resume_url at the cv_store file so every
    # downstream consumer (profile view, rescreen, CSV export) resolves one path.
    try:
        _cv_result = _cv_ingest_and_link(
            data=file_bytes,
            filename=file.filename or f"{_uuid.uuid4()}{suffix}",
            source="application",
            uploaded_by=None,
            candidate_id=str(cand_id),
            req_id=requisition_id,
        )
        _cv_id = _cv_result.get("cv_id") if _cv_result else None
        if _cv_id:
            _cv_row = query_one(
                "SELECT file_path FROM cv_repository WHERE id=%s", [_cv_id]
            )
            if _cv_row and _cv_row.get("file_path"):
                query(
                    "UPDATE candidate SET resume_url=%s WHERE id=%s",
                    [_cv_row["file_path"], str(cand_id)],
                    fetch=False,
                )
    except Exception as _cv_exc:
        print(f"[cv-ingest] Failed to link resume for candidate {cand_id}: {_cv_exc}")
    _send_jd_email(full_name, email, requisition_id)
    _maybe_issue_candidate_invite(cand_id, email, full_name)
    return {
        "application_id": app_row["id"],
        "match_score": app_row["match_score"],
        "breakdown": app_row["score_breakdown"],
        "resume_preview": resume_text[:400] if resume_text else "",
        "warning": warning,
    }


@app.post("/api/applications/{application_id}/bot-round")
def bot_round(application_id: str):
    return pipeline.run_bot_round(application_id)


@app.get("/api/applications/{application_id}/screening-detail")
def screening_detail(application_id: str):
    """Full screening breakdown for the 'Why this score?' recruiter panel."""
    row = query_one(
        """SELECT a.id, a.match_score, a.score_breakdown, a.ai_screen_detail,
                  a.avg_tenure_months, a.stability_score, a.stability_status,
                  a.ai_fit_score, a.status,
                  c.full_name AS candidate_name
           FROM application a
           JOIN candidate c ON c.id = a.candidate_id
           WHERE a.id = %s""",
        [application_id],
    )
    if not row:
        raise HTTPException(404, "application not found")
    return dict(row)


class ManualTenureIn(BaseModel):
    avg_tenure_months: float


@app.post("/api/applications/{application_id}/manual-tenure")
def manual_tenure(application_id: str, payload: ManualTenureIn, request: Request):
    """
    Recruiter submits average tenure (months) for a pending_manual application.
    Recomputes stability_score and match_score with full four-dimension weights.
    JWT-protected (middleware handles auth).
    """
    if payload.avg_tenure_months <= 0:
        raise HTTPException(400, "avg_tenure_months must be > 0")
    actor_id = getattr(request.state, "user", {}).get("sub")
    return pipeline.update_manual_tenure(application_id, payload.avg_tenure_months, actor_id)


@app.post("/api/applications/{application_id}/re-screen")
def re_screen(application_id: str, request: Request):
    """
    Deliberate recruiter action: re-run AI screening using the stored resume.
    Does not affect bot_score / combined_score / pipeline status.
    JWT-protected (middleware handles auth).
    """
    actor_id = getattr(request.state, "user", {}).get("sub")
    return pipeline.rescreen_application(application_id, actor_id)


@app.get("/api/requisitions/{requisition_id}/chart")
def chart(requisition_id: str):
    return pipeline.top_chart(requisition_id)


# ---------------- scheduling ----------------
class ScheduleIn(BaseModel):
    application_id: str
    panel_emails: list[str] = []
    start_in_hours: int = 24
    duration_min: int = 45
    meet_link: str = ""


@app.post("/api/schedule")
def schedule(payload: ScheduleIn, request: Request):
    if request.state.user.get("role") not in ("recruiter", "ta_manager", "admin"):
        return JSONResponse(status_code=403, content={"detail": "Not authorised to schedule interviews"})
    app_row = query_one(
        """SELECT a.id, c.email, c.full_name, r.title AS job_title
           FROM application a
           JOIN candidate c ON c.id = a.candidate_id
           JOIN requisition r ON r.id = a.requisition_id
           WHERE a.id = %s""",
        [payload.application_id],
    )
    if not app_row:
        raise HTTPException(404, "application not found")
    start = datetime.utcnow() + timedelta(hours=payload.start_in_hours)
    organizer_email = request.state.user.get("email") or ""
    meeting = connectors.schedule_meeting(
        organizer_email=organizer_email,
        candidate_email=app_row["email"],
        panel_emails=payload.panel_emails,
        start_time=start,
        duration_min=payload.duration_min,
        meet_link=payload.meet_link,
        candidate_name=app_row.get("full_name") or "Candidate",
        job_title=app_row.get("job_title") or "the role",
    )
    rc = query_one(
        """SELECT id FROM round_config
           WHERE requisition_id = (SELECT requisition_id FROM application WHERE id=%s)
           ORDER BY sequence LIMIT 1""",
        [payload.application_id],
    )
    iv = query_one(
        """INSERT INTO interview
             (application_id, round_config_id, scheduled_at, meet_link, gcal_event_id, mode)
           VALUES (%s, %s, %s, %s, %s, 'virtual')
           RETURNING id""",
        [payload.application_id, rc["id"] if rc else None, start,
         meeting["meet_link"], meeting["gcal_event_id"]],
    )
    # Populate interview_panel from panel_emails (look up app_user by email)
    if iv and payload.panel_emails:
        for email in payload.panel_emails:
            pu = query_one(
                "SELECT id FROM app_user WHERE LOWER(email) = LOWER(%s) AND is_active = TRUE",
                [email],
            )
            if pu:
                query(
                    """INSERT INTO interview_panel (interview_id, interviewer_id)
                       VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                    [str(iv["id"]), str(pu["id"])],
                    fetch=False,
                )
    try:
        from .services.email_templates import render_template as _render_sched_tmpl
        _interview_time = start.strftime("%A, %d %B %Y at %I:%M %p UTC")
        _et_subj, _et_body = _render_sched_tmpl("interview_scheduled", {
            "candidate_name": app_row.get("full_name") or "Candidate",
            "job_title":      app_row.get("job_title") or "the position",
            "interview_time": _interview_time,
            "meet_link":      meeting["meet_link"],
        })
        connectors.send_email(app_row["email"], _et_subj, _et_body)
    except Exception as _sched_email_exc:
        print(f"[schedule] Email send failed: {_sched_email_exc}")
    return meeting


# ---------------- reports ----------------
@app.get("/api/reports/{view_name}")
def report(view_name: str):
    allowed = {
        "tat": "v_req_time_to_fill",
        "recruiter-load": "v_recruiter_load",
        "gender": "v_gender_split",
        "positions": "v_positions_by_fy",
        "budget": "v_budget_vs_offered",
        "bu": "v_bu_summary",
        "roll": "v_roll_split",
    }
    if view_name not in allowed:
        raise HTTPException(404, f"unknown report. choose: {list(allowed)}")
    return query(f"SELECT * FROM {allowed[view_name]}")


# ---------------- admin system endpoints ----------------
@app.get("/api/admin/db-stats")
def db_stats(request: Request):
    if request.state.user.get("role") != "admin":
        return JSONResponse(status_code=403, content={"detail": "Admin only"})
    tables = [
        "app_user", "requisition", "application", "candidate",
        "interview", "scorecard", "offer", "stage_event", "nexai_session",
    ]
    result = {}
    for t in tables:
        row = query_one(f"SELECT COUNT(*) AS n FROM {t}")
        result[t] = int(row["n"]) if row else 0
    return result


@app.get("/api/admin/cv-database")
def cv_database(request: Request):
    """CV / candidate database — full candidate list with application data."""
    if request.state.user.get("role") != "admin":
        return JSONResponse(status_code=403, content={"detail": "Admin only"})

    summary = query_one(
        """
        SELECT
          COUNT(DISTINCT LOWER(c.email))                                    AS total_candidates,
          COUNT(a.id)                                                       AS total_applications,
          COUNT(DISTINCT LOWER(c.email)) FILTER
            (WHERE c.resume_url IS NOT NULL AND c.resume_url <> '')         AS resumes_on_file,
          ROUND(AVG(a.combined_score)
            FILTER (WHERE a.combined_score IS NOT NULL)::numeric, 1)        AS avg_score,
          COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE a.status = 'hired') AS total_joined
        FROM candidate c
        LEFT JOIN application a ON a.candidate_id = c.id
        """,
    )

    candidates = query(
        """
        SELECT * FROM (
          SELECT DISTINCT ON (LOWER(c.email))
            c.id, c.full_name, c.email, c.gender, c.source,
            c.resume_url,
            c.created_at                                                     AS registered_at,
            (SELECT COUNT(DISTINCT a_cnt.requisition_id)
             FROM application a_cnt
             JOIN candidate c_dup ON c_dup.id = a_cnt.candidate_id
             WHERE LOWER(c_dup.email) = LOWER(c.email))                     AS total_applications,
            (SELECT r.title
             FROM application a2
             JOIN candidate c2d ON c2d.id = a2.candidate_id
             JOIN requisition r ON r.id = a2.requisition_id
             WHERE LOWER(c2d.email) = LOWER(c.email)
             ORDER BY a2.applied_at DESC LIMIT 1)                           AS latest_position,
            (SELECT a3.status
             FROM application a3
             JOIN candidate c3d ON c3d.id = a3.candidate_id
             WHERE LOWER(c3d.email) = LOWER(c.email)
             ORDER BY a3.applied_at DESC LIMIT 1)                           AS latest_status,
            (SELECT a4.combined_score
             FROM application a4
             JOIN candidate c4d ON c4d.id = a4.candidate_id
             WHERE LOWER(c4d.email) = LOWER(c.email)
             ORDER BY a4.combined_score DESC NULLS LAST LIMIT 1)            AS best_score,
            (SELECT a5.bot_score
             FROM application a5
             JOIN candidate c5d ON c5d.id = a5.candidate_id
             WHERE LOWER(c5d.email) = LOWER(c.email)
             ORDER BY a5.bot_score DESC NULLS LAST LIMIT 1)                 AS ai_score,
            (SELECT a6.match_score
             FROM application a6
             JOIN candidate c6d ON c6d.id = a6.candidate_id
             WHERE LOWER(c6d.email) = LOWER(c.email)
             ORDER BY a6.match_score DESC NULLS LAST LIMIT 1)               AS match_score
          FROM candidate c
          ORDER BY LOWER(c.email), c.created_at ASC
        ) deduped
        ORDER BY registered_at DESC
        """,
    )

    return {"summary": dict(summary) if summary else {}, "candidates": candidates}


@app.get("/api/admin/sys-logs")
def sys_logs(request: Request, limit: int = 100):
    if request.state.user.get("role") != "admin":
        return JSONResponse(status_code=403, content={"detail": "Admin only"})
    return query(
        """SELECT se.id, se.from_status, se.to_status,
                  COALESCE(u.full_name, 'system') AS actor,
                  se.note, se.occurred_at
           FROM stage_event se
           LEFT JOIN app_user u ON u.id = se.actor_id
           ORDER BY se.occurred_at DESC
           LIMIT %s""",
        [min(limit, 500)],
    )


# ---------------- frontend ----------------
_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

if os.path.isdir(_FRONTEND_DIR):
    @app.get("/login", response_class=HTMLResponse)
    def login_page():
        with open(os.path.join(_FRONTEND_DIR, "login.html"), encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)

    @app.get("/nexai-interview", response_class=HTMLResponse)
    def nexai_interview_page():
        """Public candidate-facing AI interview page — accessed via invite token."""
        with open(os.path.join(_FRONTEND_DIR, "interview.html"), encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)

    @app.get("/set-password", response_class=HTMLResponse)
    def set_password_page():
        with open(os.path.join(_FRONTEND_DIR, "set-password.html"), encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)

    @app.get("/", response_class=HTMLResponse)
    def index():
        with open(os.path.join(_FRONTEND_DIR, "index.html"), encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)
