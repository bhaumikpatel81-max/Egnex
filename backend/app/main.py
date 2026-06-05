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

from fastapi import FastAPI, HTTPException, Request, File, UploadFile, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import psycopg2

from .db import query, query_one
from .services import pipeline, connectors, notetaker
from .services.resume_parser import extract_text as _parse_resume
from .routers.google_oauth import router as _google_oauth_router
from .routers.auth import router as _auth_router
from .routers.admin_users import router as _admin_router
from .routers.pipeline_api import router as _pipeline_router
from .routers.reports_api import router as _reports2_router
from .routers.nexai_api import router as _nexai_router
from .routers.proctoring_api import router as _proctoring_router
from .routers.tickets_api import router as _tickets_router
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
    ]
    for sql in migrations:
        try:
            query(sql, fetch=False)
        except Exception as exc:
            # Log but don't crash — a failed migration shouldn't block startup
            print(f"[auto-migrate] WARNING: {exc}")

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

_RESUME_MIME = {
    ".pdf":  "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc":  "application/msword",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
}

# Paths that do NOT require a JWT
_PUBLIC = {"/", "/login", "/api/health", "/api/auth/login", "/nexai-interview"}
_PUBLIC_PREFIXES = (
    "/assets/",
    "/api/nexai/invite/",   # candidate-facing: validate, start, submit — token-based, no JWT
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)
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
        """SELECT a.id, c.email FROM application a
           JOIN candidate c ON c.id = a.candidate_id WHERE a.id = %s""",
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
    query(
        """INSERT INTO interview
             (application_id, round_config_id, scheduled_at, meet_link, gcal_event_id, mode)
           VALUES (%s, %s, %s, %s, %s, 'virtual')""",
        [payload.application_id, rc["id"] if rc else None, start,
         meeting["meet_link"], meeting["gcal_event_id"]], fetch=False,
    )
    connectors.send_email(app_row["email"], "Interview scheduled",
                          f"Your interview is at {start}. Link: {meeting['meet_link']}")
    return meeting


# ---------------- notetaker ----------------
class ConsentIn(BaseModel):
    interview_id: str
    candidate_id: str
    consent_text: str = ("This interview will be recorded and processed by Egnex "
                         "to generate notes shared with the hiring panel. "
                         "Do you consent?")
    region: str = "IN"


@app.post("/api/consent/request")
def consent_request(payload: ConsentIn):
    return notetaker.request_consent(payload.interview_id, payload.candidate_id,
                                     payload.consent_text, payload.region)


class ConsentResponseIn(BaseModel):
    interview_id: str
    granted: bool


@app.post("/api/consent/respond")
def consent_respond(payload: ConsentResponseIn):
    return notetaker.record_consent_response(payload.interview_id, payload.granted)


class NotesIn(BaseModel):
    interview_id: str
    job_description: str = ""
    share_with: list[str] = []


@app.post("/api/interviews/notes")
def interview_notes(payload: NotesIn):
    return notetaker.process_interview(payload.interview_id, payload.job_description,
                                      payload.share_with)


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
          COUNT(DISTINCT c.id)                                              AS total_candidates,
          COUNT(a.id)                                                       AS total_applications,
          COUNT(DISTINCT c.id) FILTER
            (WHERE c.resume_url IS NOT NULL AND c.resume_url <> '')         AS resumes_on_file,
          ROUND(AVG(a.combined_score)
            FILTER (WHERE a.combined_score IS NOT NULL)::numeric, 1)        AS avg_score,
          COUNT(DISTINCT c.id) FILTER (WHERE a.status = 'joined')           AS total_joined
        FROM candidate c
        LEFT JOIN application a ON a.candidate_id = c.id
        """,
    )

    candidates = query(
        """
        SELECT
          c.id, c.full_name, c.email, c.gender, c.source,
          c.resume_url,
          c.created_at                                                     AS registered_at,
          (SELECT COUNT(*) FROM application WHERE candidate_id = c.id)    AS total_applications,
          (SELECT r.title
           FROM application a2
           JOIN requisition r ON r.id = a2.requisition_id
           WHERE a2.candidate_id = c.id
           ORDER BY a2.applied_at DESC LIMIT 1)                           AS latest_position,
          (SELECT a3.status
           FROM application a3
           WHERE a3.candidate_id = c.id
           ORDER BY a3.applied_at DESC LIMIT 1)                           AS latest_status,
          (SELECT a4.combined_score
           FROM application a4
           WHERE a4.candidate_id = c.id
           ORDER BY a4.combined_score DESC NULLS LAST LIMIT 1)            AS best_score,
          (SELECT a5.bot_score
           FROM application a5
           WHERE a5.candidate_id = c.id
           ORDER BY a5.bot_score DESC NULLS LAST LIMIT 1)                 AS ai_score,
          (SELECT a6.match_score
           FROM application a6
           WHERE a6.candidate_id = c.id
           ORDER BY a6.match_score DESC NULLS LAST LIMIT 1)               AS match_score
        FROM candidate c
        ORDER BY c.created_at DESC
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
