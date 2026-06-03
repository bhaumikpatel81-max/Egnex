"""
One Click Hire -- FastAPI backend.
"""
import os
import json
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import psycopg2

from .db import query, query_one
from .services import pipeline, connectors, notetaker
from .routers.google_oauth import router as _google_oauth_router
from .routers.auth import router as _auth_router, get_current_user

app = FastAPI(title="Egnex API", version="0.2.0")
app.include_router(_google_oauth_router)
app.include_router(_auth_router)

_FRONTEND_DIR = os.environ.get(
    "FRONTEND_DIR",
    os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "frontend")
    ),
)
_ASSETS_DIR = os.path.join(_FRONTEND_DIR, "assets")
if os.path.isdir(_ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=_ASSETS_DIR), name="assets")


@app.exception_handler(psycopg2.Error)
def db_error_handler(request: Request, exc: psycopg2.Error):
    return JSONResponse(status_code=400,
                        content={"error": "database constraint or bad reference",
                                 "detail": str(exc).splitlines()[0]})


# ── health ──────────────────────────────────────────────────
@app.get("/api/health")
def health():
    try:
        query_one("SELECT 1 AS ok")
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "degraded", "error": str(e)})


# ── reference / config ──────────────────────────────────────
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


# ── requisitions ─────────────────────────────────────────────
@app.get("/api/requisitions")
def requisitions():
    return query(
        """SELECT r.id, r.title, r.status, r.roll_type, r.fiscal_year,
                  r.budgeted_ctc, r.openings, r.min_experience,
                  b.code AS band, bu.name AS business_unit,
                  COALESCE((
                      SELECT COUNT(*) FROM application a
                      WHERE a.requisition_id = r.id
                        AND a.status NOT IN ('rejected','dropped','screen_rejected')
                  ), 0) AS in_pipeline
           FROM requisition r
           JOIN band b  ON b.id  = r.band_id
           JOIN business_unit bu ON bu.id = r.bu_id
           ORDER BY r.created_at DESC"""
    )


@app.get("/api/requisitions/{requisition_id}")
def requisition_detail(requisition_id: str):
    req = query_one(
        """SELECT r.id, r.title, r.status, r.roll_type, r.fiscal_year,
                  r.budgeted_ctc, r.openings, r.min_experience,
                  r.job_description, r.key_skills,
                  b.code AS band, bu.name AS business_unit
           FROM requisition r
           JOIN band b  ON b.id  = r.band_id
           JOIN business_unit bu ON bu.id = r.bu_id
           WHERE r.id = %s""",
        [requisition_id],
    )
    if not req:
        raise HTTPException(404, "requisition not found")
    rounds = query(
        """SELECT id, sequence, name, round_type, is_auto
           FROM round_config WHERE requisition_id = %s ORDER BY sequence""",
        [requisition_id],
    )
    return {**dict(req), "rounds": rounds}


@app.get("/api/requisitions/{requisition_id}/kanban")
def kanban_candidates(requisition_id: str):
    """Return all active candidates for a requisition with stage info."""
    return query(
        """SELECT a.id, c.full_name, c.gender, a.status, a.current_round,
                  a.match_score, a.bot_score, a.combined_score, a.applied_at
           FROM application a
           JOIN candidate c ON c.id = a.candidate_id
           WHERE a.requisition_id = %s
             AND a.status NOT IN ('rejected','dropped','screen_rejected')
           ORDER BY COALESCE(a.combined_score, a.match_score, 0) DESC""",
        [requisition_id],
    )


class RequisitionIn(BaseModel):
    title: str
    bu_id: str
    band_id: str
    roll_type: str = "on_roll"
    openings: int = 1
    min_experience: float | None = None
    budgeted_ctc: float | None = None
    fiscal_year: str | None = "FY25-26"
    key_skills: list[str] = []
    job_description: str | None = None
    rounds: list[dict] = []


@app.post("/api/requisitions")
def create_requisition(payload: RequisitionIn, request: Request):
    # Get creator from auth token if present; fall back to None
    creator_id = None
    try:
        u = get_current_user(request)
        creator_id = str(u["id"])
    except HTTPException:
        pass

    req = query_one(
        """INSERT INTO requisition
             (title, bu_id, band_id, roll_type, openings, min_experience,
              budgeted_ctc, fiscal_year, key_skills, job_description,
              status, created_by, opened_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',%s,now())
           RETURNING id""",
        [payload.title, payload.bu_id, payload.band_id, payload.roll_type,
         payload.openings, payload.min_experience, payload.budgeted_ctc,
         payload.fiscal_year, payload.key_skills or [],
         payload.job_description, creator_id],
    )
    req_id = str(req["id"])

    # Assign creator as recruiter/owner if they have recruiter role
    if creator_id:
        try:
            query(
                """INSERT INTO requisition_recruiter (requisition_id, recruiter_id, is_owner, assigned_by)
                   VALUES (%s,%s,true,%s)""",
                [req_id, creator_id, creator_id],
                fetch=False,
            )
        except Exception:
            pass  # non-recruiter role — skip

    # Insert round configs
    for r in payload.rounds:
        query(
            """INSERT INTO round_config (requisition_id, sequence, name, round_type, is_auto)
               VALUES (%s,%s,%s,%s,%s)""",
            [req_id, r.get("sequence", 1), r.get("name", "Round"),
             r.get("round_type", "panel"), bool(r.get("is_auto", False))],
            fetch=False,
        )

    return {"id": req_id, "title": payload.title, "status": "open"}


# ── applications / pipeline ───────────────────────────────────
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


# ── candidates (cross-requisition) ───────────────────────────
@app.get("/api/candidates")
def all_candidates(request: Request):
    """All candidates with their current stage, optionally filtered by recruiter."""
    user = None
    try:
        user = get_current_user(request)
    except HTTPException:
        pass

    is_admin = not user or user["role"] in ("ta_manager", "admin")

    if is_admin:
        return query(
            """SELECT a.id, c.full_name, c.email, c.gender,
                      r.title AS requisition_title,
                      a.status, a.match_score, a.bot_score, a.combined_score,
                      a.applied_at
               FROM application a
               JOIN candidate c ON c.id = a.candidate_id
               JOIN requisition r ON r.id = a.requisition_id
               WHERE a.status NOT IN ('rejected','dropped')
               ORDER BY COALESCE(a.combined_score, a.match_score, 0) DESC
               LIMIT 200"""
        )
    else:
        uid = str(user["id"])
        return query(
            """SELECT a.id, c.full_name, c.email, c.gender,
                      r.title AS requisition_title,
                      a.status, a.match_score, a.bot_score, a.combined_score,
                      a.applied_at
               FROM application a
               JOIN candidate c ON c.id = a.candidate_id
               JOIN requisition r ON r.id = a.requisition_id
               WHERE a.status NOT IN ('rejected','dropped')
                 AND a.requisition_id IN (
                     SELECT requisition_id FROM requisition_recruiter WHERE recruiter_id = %s
                 )
               ORDER BY COALESCE(a.combined_score, a.match_score, 0) DESC
               LIMIT 200""",
            [uid],
        )


# ── interviews ────────────────────────────────────────────────
@app.get("/api/interviews")
def list_interviews(request: Request):
    user = None
    try:
        user = get_current_user(request)
    except HTTPException:
        pass

    is_admin = not user or user["role"] in ("ta_manager", "admin")

    if is_admin:
        return query(
            """SELECT i.id, i.scheduled_at, i.meet_link, i.status, i.mode,
                      c.full_name AS candidate_name,
                      r.title AS requisition_title,
                      rc.name AS round_name
               FROM interview i
               JOIN application a  ON a.id  = i.application_id
               JOIN candidate c    ON c.id  = a.candidate_id
               JOIN requisition r  ON r.id  = a.requisition_id
               LEFT JOIN round_config rc ON rc.id = i.round_config_id
               ORDER BY i.scheduled_at DESC
               LIMIT 100"""
        )
    else:
        uid = str(user["id"])
        return query(
            """SELECT i.id, i.scheduled_at, i.meet_link, i.status, i.mode,
                      c.full_name AS candidate_name,
                      r.title AS requisition_title,
                      rc.name AS round_name
               FROM interview i
               JOIN application a  ON a.id  = i.application_id
               JOIN candidate c    ON c.id  = a.candidate_id
               JOIN requisition r  ON r.id  = a.requisition_id
               LEFT JOIN round_config rc ON rc.id = i.round_config_id
               WHERE a.requisition_id IN (
                   SELECT requisition_id FROM requisition_recruiter WHERE recruiter_id = %s
               )
               ORDER BY i.scheduled_at DESC
               LIMIT 100""",
            [uid],
        )


# ── dashboard stats ───────────────────────────────────────────
@app.get("/api/dashboard/stats")
def dashboard_stats(request: Request):
    user = None
    try:
        user = get_current_user(request)
    except HTTPException:
        pass

    is_admin = not user or user["role"] in ("ta_manager", "admin")
    uid = str(user["id"]) if user else None

    def _count(where: str, params: list | None = None) -> int:
        """Count applications matching `where`. Internal use only — where is always a literal."""
        if is_admin:
            row = query_one(f"SELECT COUNT(*) AS n FROM application a WHERE {where}",
                            params or [])
        else:
            row = query_one(
                f"""SELECT COUNT(*) AS n FROM application a
                    WHERE a.requisition_id IN (
                        SELECT requisition_id FROM requisition_recruiter WHERE recruiter_id = %s
                    ) AND {where}""",
                [uid] + (params or []),
            )
        return int(row["n"] or 0) if row else 0

    if is_admin:
        open_reqs = int(
            (query_one("SELECT COUNT(*) AS n FROM requisition WHERE status='open'") or {}).get("n", 0)
        )
    else:
        open_reqs = int(
            (query_one(
                """SELECT COUNT(DISTINCT r.id) AS n FROM requisition r
                   JOIN requisition_recruiter rr ON rr.requisition_id = r.id
                   WHERE r.status='open' AND rr.recruiter_id=%s""",
                [uid],
            ) or {}).get("n", 0)
        ) if uid else 0

    avg_row = query_one(
        """SELECT ROUND(
               AVG(EXTRACT(EPOCH FROM (
                   (SELECT min(se.occurred_at) FROM stage_event se
                    WHERE se.application_id = a.id AND se.to_status = 'joined')
                   - a.applied_at
               )) / 86400)::numeric, 1
           ) AS avg_days
           FROM application a WHERE a.status = 'joined'"""
    )
    avg_days = float(avg_row["avg_days"]) if avg_row and avg_row["avg_days"] else None

    return {
        "open_reqs":         open_reqs,
        "applications":      _count("a.status='applied'"),
        "under_screening":   _count("a.status='screening'"),
        "screening_cleared": _count("a.status='screen_passed'"),
        "ai_interview":      _count("a.status='interviewing' AND a.current_round=1"),
        "panel_interview":   _count("a.status='interviewing' AND a.current_round>1"),
        "selected":          _count("a.status='selected'"),
        "offer_stage":       _count("a.status IN ('offer_stage','offered')"),
        "joined":            _count("a.status='joined'"),
        "avg_days_to_hire":  avg_days,
    }


# ── scheduling ────────────────────────────────────────────────
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


# ── notetaker ─────────────────────────────────────────────────
class ConsentIn(BaseModel):
    interview_id: str
    candidate_id: str
    consent_text: str = ("This interview will be recorded and processed by Egnex "
                         "to generate notes shared with the hiring panel. Do you consent?")
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


# ── reports ───────────────────────────────────────────────────
@app.get("/api/reports/{view_name}")
def report(view_name: str):
    allowed = {
        "tat":             "v_req_time_to_fill",
        "recruiter-load":  "v_recruiter_load",
        "gender":          "v_gender_split",
        "positions":       "v_positions_by_fy",
        "budget":          "v_budget_vs_offered",
        "bu":              "v_bu_summary",
        "roll":            "v_roll_split",
    }
    if view_name not in allowed:
        raise HTTPException(404, f"unknown report. choose: {list(allowed)}")
    return query(f"SELECT * FROM {allowed[view_name]}")


# ── frontend (SPA catch-all) ──────────────────────────────────
if os.path.isdir(_FRONTEND_DIR):
    _index = os.path.join(_FRONTEND_DIR, "index.html")

    @app.get("/", response_class=HTMLResponse)
    def index():
        with open(_index) as f:
            return f.read()

    @app.get("/{full_path:path}", response_class=HTMLResponse)
    def spa_fallback(full_path: str):
        # Let /api/* and /assets/* pass through before reaching here.
        # This catch-all serves index.html for any non-API path so the
        # client-side router handles it.
        if full_path.startswith("api/") or full_path.startswith("assets/"):
            raise HTTPException(404)
        with open(_index) as f:
            return f.read()
