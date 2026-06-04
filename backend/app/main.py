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
from fastapi.responses import HTMLResponse, JSONResponse
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

# Paths that do NOT require a JWT
_PUBLIC = {"/", "/login", "/api/health", "/api/auth/login"}
_PUBLIC_PREFIXES = ("/assets/",)


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


@app.get("/api/business-units")
def business_units():
    return query(
        """SELECT bu.id, bu.name, gc.name AS company
           FROM business_unit bu JOIN group_company gc ON gc.id = bu.company_id
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
    gender: str = "undisclosed"
    resume_text: str = ""
    years_experience: float | None = None
    source: str = "career_site"


@app.post("/api/apply")
def apply(payload: ApplyIn):
    """An external application arrives -> create candidate -> auto-screen."""
    cand = query_one(
        """INSERT INTO candidate (full_name, email, gender, source)
           VALUES (%s, %s, %s, %s) RETURNING id""",
        [payload.full_name, payload.email, payload.gender, payload.source],
    )
    app_row = pipeline.intake_and_screen(
        payload.requisition_id, cand["id"], payload.resume_text, payload.years_experience
    )
    return {"application_id": app_row["id"], "match_score": app_row["match_score"],
            "breakdown": app_row["score_breakdown"]}


_ALLOWED_RESUME_TYPES = {".pdf", ".docx", ".doc", ".jpg", ".jpeg", ".png"}


@app.post("/api/apply/upload")
async def apply_upload(
    requisition_id: str = Form(...),
    full_name: str = Form(...),
    email: str = Form(...),
    gender: str = Form("undisclosed"),
    years_experience: float = Form(None),
    source: str = Form("career_site"),
    file: UploadFile = File(...),
):
    """File-upload path: extract text from PDF/Word, then auto-screen."""
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix not in _ALLOWED_RESUME_TYPES:
        raise HTTPException(
            400, f"Unsupported file type '{suffix or 'none'}'. Upload a PDF or Word document."
        )

    file_bytes = await file.read()

    # Extract resume text
    resume_text, warning = _parse_resume(file_bytes, file.filename or "")

    # Save locally (swap this block for GCP Storage upload when credentials are available)
    saved_name = f"{_uuid.uuid4()}{suffix}"
    saved_path = os.path.join(_UPLOADS_DIR, saved_name)
    with open(saved_path, "wb") as fout:
        fout.write(file_bytes)

    cand = query_one(
        """INSERT INTO candidate (full_name, email, gender, source, resume_url)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        [full_name, email.lower(), gender, source, saved_path],
    )
    app_row = pipeline.intake_and_screen(
        requisition_id, cand["id"], resume_text, years_experience
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
        with open(os.path.join(_FRONTEND_DIR, "login.html")) as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)

    @app.get("/", response_class=HTMLResponse)
    def index():
        with open(os.path.join(_FRONTEND_DIR, "index.html")) as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)
