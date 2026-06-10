"""
One Click Hire -- FastAPI backend (prototype).

Binds to 0.0.0.0 and reads PORT from the environment, per the deployment
prerequisites. Serves a JSON API plus a simple bundled frontend so the whole
pipeline can be demonstrated end to end.
"""
import os
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
from .routers.google_oauth import router as _google_oauth_router
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
from .routers.transcript_api import router as _transcript_router
from .auth_utils import _decode

app = FastAPI(title="Egnex API", version="0.1.0")
app.include_router(_auth_router)
app.include_router(_admin_router)
app.include_router(_google_oauth_router)
app.include_router(_pipeline_router)
app.include_router(_reports2_router)
app.include_router(_nexai_router)
app.include_router(_proctoring_router)
app.include_router(_tickets_router)
app.include_router(_scorecard_router)
app.include_router(_email_template_router)
app.include_router(_offers_router)
app.include_router(_transcript_router)


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
        # Migration 18: proctoring completion flag (added 2026-06)
        "ALTER TABLE proctoring_session ADD COLUMN IF NOT EXISTS proctoring_complete BOOLEAN NOT NULL DEFAULT FALSE",
        # Migration 19: email-sent guard to prevent duplicate completion emails (added 2026-06)
        "ALTER TABLE nexai_session ADD COLUMN IF NOT EXISTS email_sent BOOLEAN NOT NULL DEFAULT FALSE",
        # Migration 22: real AI screening columns + stability dimension (added 2026-06)
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS ai_fit_score      NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS ai_screen_detail  JSONB",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS avg_tenure_months NUMERIC",
        "ALTER TABLE application ADD COLUMN IF NOT EXISTS stability_score   NUMERIC",
        """ALTER TABLE application ADD COLUMN IF NOT EXISTS stability_status TEXT
           CHECK (stability_status IS NULL
               OR stability_status IN ('computed','pending_manual','not_applicable'))""",
        # Migration 24: scorecard draft/submit workflow (added 2026-06)
        "ALTER TABLE scorecard ALTER COLUMN submitted_at DROP NOT NULL",
        "ALTER TABLE scorecard ADD COLUMN IF NOT EXISTS status     TEXT        NOT NULL DEFAULT 'draft'",
        "ALTER TABLE scorecard ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "UPDATE scorecard SET status = 'submitted' WHERE submitted_at IS NOT NULL AND status = 'draft'",
        # Migration 23: extended application fields — employment snapshot + CTC (added 2026-06)
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
    ]
    for sql in migrations:
        try:
            query(sql, fetch=False)
        except Exception as exc:
            # Log but don't crash — a failed migration shouldn't block startup
            print(f"[auto-migrate] WARNING: {exc}")

    # Seed built-in email template defaults (idempotent — skips existing rows)
    try:
        from .services.email_templates import ensure_defaults as _ensure_email_defaults
        _ensure_email_defaults()
    except Exception as _edt_exc:
        print(f"[auto-migrate] email template seed failed: {_edt_exc}")

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
    "/nexai-interview",
    # Candidate-facing NexAI interview endpoints — token-based, no JWT
    "/api/nexai/invite/validate",
    "/api/nexai/invite/begin",
}
_PUBLIC_PREFIXES = (
    "/assets/",
    "/api/nexai/invite/submit/",       # /api/nexai/invite/submit/{session_id}
    "/api/proctoring/candidate/",      # candidate token-auth proctoring endpoints
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
    return query(
        """SELECT r.id, r.title, r.status, r.roll_type, r.fiscal_year,
                  r.budgeted_ctc, b.code AS band, bu.name AS business_unit,
                  (SELECT COUNT(*) FROM application  WHERE requisition_id = r.id) AS in_pipeline,
                  (SELECT COUNT(*) FROM round_config WHERE requisition_id = r.id) AS levels
           FROM requisition r
           JOIN band b ON b.id = r.band_id
           JOIN business_unit bu ON bu.id = r.bu_id
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
    text, _ = _parse_resume(file_bytes, file.filename or "")
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
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix not in _ALLOWED_RESUME_TYPES:
        raise HTTPException(
            400, f"Unsupported file type '{suffix or 'none'}'. Upload a PDF or Word document."
        )

    file_bytes = await file.read()
    resume_text, warning = _parse_resume(file_bytes, file.filename or "")

    saved_name = f"{_uuid.uuid4()}{suffix}"
    saved_path = os.path.join(_UPLOADS_DIR, saved_name)
    with open(saved_path, "wb") as fout:
        fout.write(file_bytes)

    cand_id = _dedup_or_create_candidate(
        full_name=full_name,
        email=email,
        phone=phone or None,
        gender=gender,
        source=source,
        resume_url=saved_path,
        requisition_id=requisition_id,
    )
    app_row = pipeline.intake_and_screen(
        requisition_id, cand_id, resume_text, years_experience
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


class AdvanceIn(BaseModel):
    to_status: str
    actor_id: str | None = None
    note: str | None = None


@app.post("/api/applications/{application_id}/advance")
def advance(application_id: str, payload: AdvanceIn):
    return pipeline.advance(application_id, payload.to_status, payload.actor_id, payload.note)


@app.get("/api/requisitions/{requisition_id}/chart")
def chart(requisition_id: str):
    return pipeline.top_chart(requisition_id)


# ---------------- scheduling ----------------
class ScheduleIn(BaseModel):
    application_id: str
    recruiter_id: str
    panel_emails: list[str] = []
    start_in_hours: int = 24
    duration_min: int = 45


@app.post("/api/schedule")
def schedule(payload: ScheduleIn):
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
    try:
        meeting = connectors.schedule_meeting(
            payload.recruiter_id, app_row["email"], payload.panel_emails,
            start, payload.duration_min,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
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
          COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE a.status = 'joined') AS total_joined
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

    @app.get("/", response_class=HTMLResponse)
    def index():
        with open(os.path.join(_FRONTEND_DIR, "index.html"), encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)
