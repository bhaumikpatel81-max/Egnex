"""
New pipeline API endpoints: dashboard, requisitions CRUD,
kanban, candidates, interviews, hiring-manager review.
"""
import json as _json
import os as _os
import re as _re
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, File, Form, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import get_current_user

router = APIRouter(prefix="/api", tags=["pipeline"])

from ..services.sla import (
    PIPELINE_STAGES, PIPELINE_STAGE_LABELS, NEXT_STAGE, TERMINAL,
    STAGE_SLA_KEY, load_config, compute_rag,
)
from .hiring_plan_api import sync_plan_on_advance


# ─── helpers ──────────────────────────────────────────────────────────────────

def _is_recruiter_scoped(role: str) -> bool:
    return role == "recruiter"


# ─── Dashboard ────────────────────────────────────────────────────────────────

@router.get("/dashboard")
def dashboard(user: dict = Depends(get_current_user)):
    role = user["role"]
    uid  = user["sub"]

    if role == "recruiter":
        # Only count applications under the recruiter's own requisitions
        app_filter = """
            a.requisition_id IN (
                SELECT requisition_id FROM requisition_recruiter WHERE recruiter_id = %s
            )
        """
        req_filter = """
            r.id IN (
                SELECT requisition_id FROM requisition_recruiter WHERE recruiter_id = %s
            )
        """
        p = [uid]
    else:
        app_filter = "1=1"
        req_filter  = "1=1"
        p = []

    def cnt(extra_where):
        row = query_one(
            f"SELECT COUNT(*) AS n FROM application a WHERE {app_filter} AND ({extra_where})",
            p,
        )
        return int(row["n"]) if row else 0

    open_reqs = query_one(
        f"SELECT COUNT(*) AS n FROM requisition r WHERE {req_filter} AND r.status='open' AND COALESCE(r.approval_status,'approved')='approved'",
        p,
    )

    counts = {
        "open_reqs":         int(open_reqs["n"]) if open_reqs else 0,
        "apps_received":     cnt("1=1"),
        "under_screening":   cnt("a.status='screen'"),
        "screening_cleared": cnt("a.status IN ('shortlisted','nexai_bot')"),
        "ai_interview":      cnt("a.bot_score IS NOT NULL"),
        "panel_interview":   cnt("a.status='interview'"),
        "selected":          cnt("a.status='documentation'"),
        "offer_stage":       cnt("a.status='offered'"),
        "joined":            cnt("a.status='hired'"),
    }

    # Average days to hire (stage_event: applied → joined)
    ath = query_one(
        """
        SELECT ROUND(
            AVG(EXTRACT(EPOCH FROM (e2.occurred_at - e1.occurred_at)) / 86400)::numeric, 1
        ) AS avg_days
        FROM stage_event e1
        JOIN stage_event e2 ON e2.application_id = e1.application_id
        WHERE e1.to_status = 'applied' AND e2.to_status = 'joined'
        """,
        [],
    )
    counts["avg_days_to_hire"] = float(ath["avg_days"]) if ath and ath["avg_days"] else None

    # Gender split (global or scoped)
    if role == "recruiter":
        gender = query(
            """
            SELECT c.gender, COUNT(*) AS n
            FROM application a
            JOIN candidate c ON c.id = a.candidate_id
            WHERE a.requisition_id IN (
                SELECT requisition_id FROM requisition_recruiter WHERE recruiter_id = %s
            )
            GROUP BY c.gender
            """,
            [uid],
        )
    else:
        gender = query(
            "SELECT gender, COUNT(*) AS n FROM candidate GROUP BY gender",
            [],
        )
    counts["gender_split"] = gender

    # Recent requisitions (scoped)
    if role == "recruiter":
        reqs = query(
            """
            SELECT r.id, r.title, r.status, b.code AS band, bu.name AS business_unit,
                   (SELECT COUNT(*) FROM application WHERE requisition_id = r.id) AS in_pipeline,
                   (SELECT COUNT(*) FROM round_config  WHERE requisition_id = r.id) AS levels
            FROM requisition r
            JOIN requisition_recruiter rr ON rr.requisition_id = r.id AND rr.recruiter_id = %s
            JOIN band b          ON b.id = r.band_id
            JOIN business_unit bu ON bu.id = r.bu_id
            ORDER BY r.created_at DESC LIMIT 10
            """,
            [uid],
        )
    elif role == "hiring_manager":
        reqs = query(
            """
            SELECT r.id, r.title, r.status, b.code AS band, bu.name AS business_unit,
                   (SELECT COUNT(*) FROM application WHERE requisition_id = r.id) AS in_pipeline,
                   (SELECT COUNT(*) FROM round_config  WHERE requisition_id = r.id) AS levels
            FROM requisition r
            JOIN band b          ON b.id = r.band_id
            JOIN business_unit bu ON bu.id = r.bu_id
            WHERE r.hiring_manager_id = %s
            ORDER BY r.created_at DESC LIMIT 10
            """,
            [uid],
        )
    else:
        reqs = query(
            """
            SELECT r.id, r.title, r.status, b.code AS band, bu.name AS business_unit,
                   (SELECT COUNT(*) FROM application WHERE requisition_id = r.id) AS in_pipeline,
                   (SELECT COUNT(*) FROM round_config  WHERE requisition_id = r.id) AS levels
            FROM requisition r
            JOIN band b          ON b.id = r.band_id
            JOIN business_unit bu ON bu.id = r.bu_id
            ORDER BY r.created_at DESC LIMIT 10
            """,
            [],
        )
    counts["recent_reqs"] = reqs

    # ─── NexAI data ────────────────────────────────────────────────────────────

    _NX_SUMMARY_COLS = """
        COUNT(*)                                                           AS total,
        COUNT(*) FILTER (WHERE ns.status = 'completed')                    AS completed,
        COUNT(*) FILTER (WHERE ns.status = 'failed')                       AS failed,
        COUNT(*) FILTER (WHERE ns.status IN ('pending','in_progress'))     AS pending,
        ROUND(AVG(ns.raw_score) FILTER (WHERE ns.status='completed')
              ::numeric, 1)                                                AS avg_score,
        COUNT(*) FILTER (WHERE ns.raw_score >= 70 AND ns.status='completed') AS high_scorers,
        COUNT(*) FILTER (WHERE ns.raw_score <  40 AND ns.status='completed') AS low_scorers,
        ROUND(
          COALESCE(
            COUNT(*) FILTER (WHERE ns.raw_score >= 50 AND ns.status='completed')
            ::numeric /
            NULLIF(COUNT(*) FILTER (WHERE ns.status='completed'), 0),
          0) * 100, 1
        )                                                                  AS pass_rate
    """

    if role == "recruiter":
        nx_where = """
            JOIN requisition r2 ON r2.id = ns.requisition_id
            JOIN requisition_recruiter rr2
                 ON rr2.requisition_id = r2.id AND rr2.recruiter_id = %s
        """
        nx_params = [uid]
        nx_dist_where = f"""
            WHERE ns.status = 'completed' AND ns.raw_score IS NOT NULL
              AND ns.requisition_id IN (
                  SELECT requisition_id FROM requisition_recruiter WHERE recruiter_id = %s
              )
        """
        nx_dist_params = [uid]
        nx_recent_where = f"""
            JOIN requisition ri ON ri.id = ns.requisition_id
            JOIN requisition_recruiter rir
                 ON rir.requisition_id = ri.id AND rir.recruiter_id = %s
        """
        nx_recent_params = [uid]
    else:
        nx_where = ""
        nx_params = []
        nx_dist_where = "WHERE ns.status = 'completed' AND ns.raw_score IS NOT NULL"
        nx_dist_params = []
        nx_recent_where = ""
        nx_recent_params = []

    if role in ("recruiter", "ta_manager"):
        nx_row = query_one(
            f"SELECT {_NX_SUMMARY_COLS} FROM nexai_session ns {nx_where}",
            nx_params,
        )
        counts["nexai_summary"] = dict(nx_row) if nx_row else {}

        counts["nexai_score_dist"] = query(
            f"""
            SELECT
              CASE
                WHEN raw_score >= 80 THEN '80-100'
                WHEN raw_score >= 60 THEN '60-79'
                WHEN raw_score >= 40 THEN '40-59'
                WHEN raw_score >= 20 THEN '20-39'
                ELSE '0-19'
              END AS bucket,
              CASE
                WHEN raw_score >= 80 THEN 5
                WHEN raw_score >= 60 THEN 4
                WHEN raw_score >= 40 THEN 3
                WHEN raw_score >= 20 THEN 2
                ELSE 1
              END AS sort_ord,
              COUNT(*) AS n
            FROM nexai_session ns
            {nx_dist_where}
            GROUP BY bucket, sort_ord
            ORDER BY sort_ord
            """,
            nx_dist_params,
        )

        counts["nexai_recent"] = query(
            f"""
            SELECT ns.id, ns.raw_score, ns.status,
                   ns.created_at, ns.completed_at,
                   c.full_name AS candidate_name,
                   r.title     AS req_title
            FROM nexai_session ns
            JOIN application a ON a.id = ns.application_id
            JOIN candidate   c ON c.id = a.candidate_id
            JOIN requisition r ON r.id = ns.requisition_id
            {nx_recent_where}
            ORDER BY ns.created_at DESC LIMIT 10
            """,
            nx_recent_params,
        )

    if role == "ta_manager":
        counts["nexai_by_recruiter"] = query(
            """
            SELECT u.full_name AS recruiter_name,
                   COUNT(ns.id)                                                   AS total,
                   COUNT(ns.id) FILTER (WHERE ns.status='completed')              AS completed,
                   ROUND(AVG(ns.raw_score) FILTER
                         (WHERE ns.status='completed')::numeric, 1)               AS avg_score,
                   COUNT(ns.id) FILTER
                         (WHERE ns.raw_score >= 70 AND ns.status='completed')     AS high_scorers
            FROM app_user u
            LEFT JOIN requisition_recruiter rr ON rr.recruiter_id = u.id
            LEFT JOIN nexai_session ns ON ns.requisition_id = rr.requisition_id
            WHERE u.role IN ('recruiter','ta_manager') AND u.is_active = true
            GROUP BY u.id, u.full_name
            ORDER BY avg_score DESC NULLS LAST, u.full_name
            """,
            [],
        )

    # Recruiter load panel (ta_manager / admin only)
    if role in ("ta_manager", "admin"):
        counts["recruiter_load"] = query("SELECT * FROM v_recruiter_load", [])

    # TA Manager: hiring manager overview
    if role == "ta_manager":
        counts["hiring_manager_stats"] = query(
            """
            SELECT u.id AS hm_id, u.full_name, u.email,
                   COUNT(DISTINCT r.id) AS assigned_reqs,
                   SUM(CASE WHEN a.status = 'interview'
                                 AND (a.hm_feedback IS NULL OR a.hm_feedback = '')
                            THEN 1 ELSE 0 END)              AS pending_reviews,
                   COUNT(DISTINCT CASE WHEN a.hm_feedback IS NOT NULL
                                            AND a.hm_feedback != ''
                                       THEN a.id END)       AS reviews_done
            FROM app_user u
            LEFT JOIN requisition r  ON r.hiring_manager_id = u.id
            LEFT JOIN application a  ON a.requisition_id    = r.id
            WHERE u.role = 'hiring_manager' AND u.is_active = true
            GROUP BY u.id, u.full_name, u.email
            ORDER BY pending_reviews DESC, u.full_name
            """,
            [],
        )

    # Hiring manager: profiles + interviews + nexai + skills + time data
    if role == "hiring_manager":
        counts["profiles_to_review"] = query(
            """
            SELECT a.id, c.full_name, r.title AS req_title,
                   a.combined_score, a.match_score, a.status
            FROM application a
            JOIN candidate  c ON c.id  = a.candidate_id
            JOIN requisition r ON r.id = a.requisition_id
            WHERE r.hiring_manager_id = %s
              AND a.status = 'interview'
              AND (a.hm_feedback IS NULL OR a.hm_feedback = '')
            ORDER BY a.combined_score DESC NULLS LAST
            LIMIT 20
            """,
            [uid],
        )
        counts["my_interviews"] = query(
            """
            SELECT i.id, i.scheduled_at, i.mode, i.duration_min,
                   COALESCE(i.status, 'scheduled') AS status,
                   c.full_name  AS candidate_name,
                   r.title      AS req_title,
                   rc.name      AS round_name
            FROM interview i
            JOIN application  a  ON a.id  = i.application_id
            JOIN candidate    c  ON c.id  = a.candidate_id
            JOIN requisition  r  ON r.id  = a.requisition_id
            LEFT JOIN round_config rc ON rc.id = i.round_config_id
            WHERE r.hiring_manager_id = %s
            ORDER BY i.scheduled_at DESC LIMIT 10
            """,
            [uid],
        )
        counts["feedback_outcomes"] = query(
            """
            SELECT COALESCE(NULLIF(a.hm_feedback,''), 'pending') AS outcome,
                   COUNT(*) AS n
            FROM application a
            JOIN requisition r ON r.id = a.requisition_id
            WHERE r.hiring_manager_id = %s
              AND a.status IN ('interview','documentation','offered','hired','rejected')
            GROUP BY COALESCE(NULLIF(a.hm_feedback,''), 'pending')
            ORDER BY n DESC
            """,
            [uid],
        )

        # Interviews conducted + time stats
        itime = query_one(
            """
            SELECT COUNT(DISTINCT i.id)                          AS n,
                   ROUND(AVG(i.duration_min)::numeric, 0)        AS avg_min,
                   ROUND(SUM(i.duration_min)::numeric / 60.0, 1) AS total_hrs
            FROM interview i
            JOIN application a  ON a.id = i.application_id
            JOIN requisition r  ON r.id = a.requisition_id
            WHERE r.hiring_manager_id = %s
            """,
            [uid],
        )
        counts["interviews_conducted"] = int(itime["n"]) if itime else 0
        counts["avg_interview_min"]    = float(itime["avg_min"])   if itime and itime["avg_min"]   else None
        counts["total_interview_hrs"]  = float(itime["total_hrs"]) if itime and itime["total_hrs"] else 0

        # NexAI screening summary for HM's requisitions
        nexai = query_one(
            """
            SELECT
              COUNT(*)                                                  AS total,
              COUNT(*) FILTER (WHERE ns.status = 'completed')          AS completed,
              COUNT(*) FILTER (WHERE ns.status = 'failed')             AS failed,
              COUNT(*) FILTER (WHERE ns.status IN ('pending','in_progress')) AS pending,
              ROUND(AVG(ns.raw_score) FILTER
                    (WHERE ns.status='completed')::numeric, 1)          AS avg_score,
              ROUND(AVG(
                EXTRACT(EPOCH FROM (ns.completed_at - ns.started_at))/60.0
              ) FILTER (WHERE ns.status='completed')::numeric, 1)       AS avg_session_min
            FROM nexai_session ns
            JOIN requisition r ON r.id = ns.requisition_id
            WHERE r.hiring_manager_id = %s
            """,
            [uid],
        )
        counts["nexai_summary"] = dict(nexai) if nexai else {
            "total": 0, "completed": 0, "failed": 0, "pending": 0,
            "avg_score": None, "avg_session_min": None,
        }

        # Skills breakdown: aggregate key_skills from HM's requisitions
        counts["skills_summary"] = query(
            """
            SELECT UNNEST(key_skills) AS skill, COUNT(*) AS n
            FROM requisition
            WHERE hiring_manager_id = %s
              AND key_skills IS NOT NULL AND array_length(key_skills, 1) > 0
            GROUP BY skill
            ORDER BY n DESC, skill
            LIMIT 15
            """,
            [uid],
        )

    return counts


# ─── Requisitions ─────────────────────────────────────────────────────────────

@router.get("/requisitions/full")
def list_requisitions_full(user: dict = Depends(get_current_user)):
    role = user["role"]
    uid  = user["sub"]
    if role == "recruiter":
        return query(
            """
            SELECT r.id, r.title, r.status, r.roll_type, r.fiscal_year,
                   r.is_p1, r.risk, r.hiring_location,
                   b.code AS band, bu.name AS business_unit,
                   r.hiring_manager_id,
                   hm.full_name AS hiring_manager_name,
                   (SELECT COUNT(*) FROM application  WHERE requisition_id = r.id) AS in_pipeline,
                   (SELECT COUNT(*) FROM round_config WHERE requisition_id = r.id) AS levels
            FROM requisition r
            JOIN requisition_recruiter rr ON rr.requisition_id = r.id AND rr.recruiter_id = %s
            JOIN band b          ON b.id = r.band_id
            JOIN business_unit bu ON bu.id = r.bu_id
            LEFT JOIN app_user hm ON hm.id = r.hiring_manager_id
            WHERE COALESCE(r.approval_status,'approved')='approved'
            ORDER BY r.created_at DESC
            """,
            [uid],
        )
    return query(
        """
        SELECT r.id, r.title, r.status, r.roll_type, r.fiscal_year,
               r.is_p1, r.risk, r.hiring_location,
               b.code AS band, bu.name AS business_unit,
               r.hiring_manager_id,
               hm.full_name AS hiring_manager_name,
               (SELECT COUNT(*) FROM application  WHERE requisition_id = r.id) AS in_pipeline,
               (SELECT COUNT(*) FROM round_config WHERE requisition_id = r.id) AS levels
        FROM requisition r
        JOIN band b          ON b.id = r.band_id
        JOIN business_unit bu ON bu.id = r.bu_id
        LEFT JOIN app_user hm ON hm.id = r.hiring_manager_id
        WHERE COALESCE(r.approval_status,'approved')='approved'
        ORDER BY r.created_at DESC
        """,
        [],
    )


class RoundIn(BaseModel):
    sequence: int
    name: str
    round_type: str = "panel"
    is_auto: bool = False


class RequisitionIn(BaseModel):
    title: str
    bu_id: str
    band_id: str
    roll_type: str = "on_roll"
    key_skills: list[str] = []
    min_experience: Optional[float] = None
    max_experience: Optional[float] = None
    budgeted_ctc: Optional[float] = None
    budgeted_fixed: Optional[float] = None
    budgeted_variable: Optional[float] = None
    openings: int = 1
    fiscal_year: Optional[str] = None
    job_description: Optional[str] = None
    is_p1: bool = False
    risk: Optional[str] = None
    hiring_location: Optional[str] = None
    project: Optional[str] = None
    grade_level: Optional[str] = None
    priority: Optional[str] = None
    source_channels: list[str] = []
    rounds: list[RoundIn] = []


@router.post("/requisitions")
def create_requisition(body: RequisitionIn, user: dict = Depends(get_current_user)):
    role = user["role"]
    if role not in ("recruiter", "ta_manager", "admin", "hiring_manager"):
        raise HTTPException(403, "Not authorised to create requisitions")

    # Derive total CTC from fixed + variable if not explicitly provided
    fixed = body.budgeted_fixed
    variable = body.budgeted_variable
    total_ctc = body.budgeted_ctc
    if total_ctc is None and (fixed is not None or variable is not None):
        total_ctc = (fixed or 0) + (variable or 0)

    # Hiring manager reqs start as pending_ta_approval — not visible until TA approves
    approval_status = "pending_ta_approval" if role == "hiring_manager" else "approved"

    # Auto-generate req_code
    seq_row = query_one(
        "SELECT COALESCE(MAX(CAST(REGEXP_REPLACE(req_code,'[^0-9]','','g') AS INTEGER)),0)+1 AS n FROM requisition WHERE req_code ~ '^REQ-[0-9]+'",
        [],
    )
    req_code = f"REQ-{int((seq_row or {}).get('n') or 1):04d}"

    req = query_one(
        """
        INSERT INTO requisition
          (title, bu_id, band_id, roll_type, key_skills, min_experience, max_experience,
           budgeted_ctc, budgeted_fixed, budgeted_variable,
           openings, fiscal_year, job_description,
           is_p1, risk, hiring_location,
           project, grade_level, priority, source_channels,
           req_code, status, opened_at, created_by,
           approval_status, created_by_role)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',now(),%s,%s,%s)
        RETURNING id, title, status, req_code, approval_status
        """,
        [
            body.title, body.bu_id, body.band_id, body.roll_type,
            body.key_skills, body.min_experience, body.max_experience, total_ctc,
            fixed, variable,
            body.openings, body.fiscal_year, body.job_description,
            body.is_p1, body.risk, body.hiring_location,
            body.project, body.grade_level, body.priority, body.source_channels,
            req_code, user["sub"], approval_status, role,
        ],
    )

    # Auto-assign the creating recruiter as owner
    if role == "recruiter":
        query(
            """INSERT INTO requisition_recruiter
               (requisition_id, recruiter_id, is_owner, assigned_by)
               VALUES (%s,%s,true,%s)""",
            [req["id"], user["sub"], user["sub"]],
            fetch=False,
        )

    # Notify TA managers when a hiring manager creates a requisition needing approval
    if role == "hiring_manager":
        hm_row = query_one("SELECT full_name FROM app_user WHERE id=%s", [user["sub"]])
        hm_name = (hm_row or {}).get("full_name") or "Hiring Manager"
        ta_emails = query(
            "SELECT email FROM app_user WHERE role='ta_manager' AND is_active=TRUE", []
        )
        ta_email_list = [r["email"] for r in (ta_emails or []) if r.get("email")]
        if ta_email_list:
            try:
                from ..services.email_templates import render_template as _rt
                from ..services.connectors import send_email as _se, resolve_global_placeholders
                req_id_for_globals = str(req["id"]) if req else None
                globals_ = resolve_global_placeholders(req_id=req_id_for_globals, actor=user)
                reply_to = globals_.get("recruiter_email") or None
                subj, bdy = _rt("hm_req_approval_request", {
                    "hm_name":   hm_name,
                    "req_title": body.title,
                }, req_id=req_id_for_globals, actor=user)
                for addr in ta_email_list:
                    try:
                        _se(addr, subj, bdy, reply_to=reply_to)
                    except Exception as exc:
                        print(f"[pipeline] ta-notification to {addr} failed: {exc}")
            except Exception as exc:
                print(f"[pipeline] hm req notification failed: {exc}")

    # Create panel rounds
    for r in body.rounds:
        query(
            """INSERT INTO round_config
               (requisition_id, sequence, name, round_type, is_auto)
               VALUES (%s,%s,%s,%s,%s)""",
            [req["id"], r.sequence, r.name, r.round_type, r.is_auto],
            fetch=False,
        )
    return req


_JD_PARSE_SYSTEM = """\
You are a job description parser. Extract structured data from the job description text.
Return ONLY a valid JSON object — no markdown fences, no prose before or after.

Required fields:
{
  "job_title": "<role title, or null>",
  "key_skills": ["list", "of", "required", "skills"],
  "min_experience": <minimum years of experience as a number, or null>,
  "max_experience": <maximum years of experience as a number, or null>,
  "job_description": "<cleaned 2-3 paragraph summary of the role and responsibilities>",
  "hiring_location": "<city or location mentioned, or null>",
  "band_or_grade": "<band, grade, or level if mentioned, or null>"
}"""


def _jd_safe_float(val) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


@router.post("/requisitions/parse-jd")
async def parse_jd(
    file: Optional[UploadFile] = File(None),
    raw_text: str = Form(""),
    user: dict = Depends(get_current_user),
):
    role = user.get("role", "")
    if role not in ("recruiter", "ta_manager", "hiring_manager", "admin"):
        raise HTTPException(403, "Not authorised to use JD parsing")

    # Extract text from file or use pasted text
    text = ""
    if file and file.filename:
        suffix = _os.path.splitext(file.filename or "")[1].lower().lstrip(".")
        if suffix not in ("pdf", "docx", "doc"):
            return JSONResponse(
                status_code=422,
                content={"detail": "Unsupported file type. Upload a PDF or Word document (.pdf, .docx, .doc)."},
            )
        file_bytes = await file.read()
        from ..services.cv_parser import extract_text as _cv_extract_text
        text = _cv_extract_text(file_bytes, suffix)
        if not text.strip():
            return JSONResponse(
                status_code=422,
                content={"detail": "Could not extract text from the uploaded file. Try pasting the JD text instead."},
            )
    elif raw_text.strip():
        text = raw_text.strip()
    else:
        return JSONResponse(
            status_code=422,
            content={"detail": "Provide a JD file or paste JD text."},
        )

    # Call Groq LLM (reuse cv_enricher credentials/model)
    try:
        import openai as _openai
        client = _openai.OpenAI(
            api_key=_os.environ.get("GROQ_API_KEY"),
            base_url=_os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        )
        resp = client.chat.completions.create(
            model=_os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile"),
            messages=[
                {"role": "system", "content": _JD_PARSE_SYSTEM},
                {"role": "user", "content": f"Job description:\n\n{text[:6000]}"},
            ],
            temperature=0,
            max_tokens=600,
        )
        raw_resp = resp.choices[0].message.content or ""
        # Strip markdown fences before parsing
        cleaned = _re.sub(r"^```(?:json)?\s*", "", raw_resp.strip(), flags=_re.IGNORECASE)
        cleaned = _re.sub(r"\s*```$", "", cleaned.strip())
        data = _json.loads(cleaned)
    except _json.JSONDecodeError:
        return JSONResponse(
            status_code=422,
            content={"detail": "The AI could not parse this JD into structured data. Try pasting cleaner text."},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": f"JD parsing failed: {str(exc)[:120]}"},
        )

    # Normalise key_skills — could be a string or a list
    skills = data.get("key_skills") or []
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.split(",") if s.strip()]

    return {
        "job_title":       data.get("job_title") or None,
        "key_skills":      skills,
        "min_experience":  _jd_safe_float(data.get("min_experience")),
        "max_experience":  _jd_safe_float(data.get("max_experience")),
        "job_description": data.get("job_description") or None,
        "hiring_location": data.get("hiring_location") or None,
        "band_or_grade":   data.get("band_or_grade") or None,
    }


@router.get("/requisitions/{req_id}/detail")
def get_requisition_detail(req_id: str, _user: dict = Depends(get_current_user)):
    req = query_one(
        """
        SELECT r.*, b.code AS band_code, bu.name AS business_unit_name
        FROM requisition r
        JOIN band b          ON b.id = r.band_id
        JOIN business_unit bu ON bu.id = r.bu_id
        WHERE r.id = %s
        """,
        [req_id],
    )
    if not req:
        raise HTTPException(404, "requisition not found")
    rounds = query(
        "SELECT * FROM round_config WHERE requisition_id = %s ORDER BY sequence",
        [req_id],
    )
    return {**dict(req), "rounds": rounds}


@router.get("/requisitions/{req_id}/kanban")
def kanban(req_id: str, _user: dict = Depends(get_current_user)):
    rounds = query(
        """SELECT id, sequence, name, round_type, is_auto
           FROM round_config WHERE requisition_id = %s ORDER BY sequence""",
        [req_id],
    )
    candidates = query(
        """
        SELECT a.id AS app_id, a.status, a.current_round,
               COALESCE(a.combined_score, a.match_score) AS score,
               c.full_name, c.gender, c.email
        FROM application a
        JOIN candidate c ON c.id = a.candidate_id
        WHERE a.requisition_id = %s
          AND a.status NOT IN ('hired','rejected','on_hold','screen_rejected','dropped','offer_cancelled')
        ORDER BY score DESC NULLS LAST
        """,
        [req_id],
    )
    return {"rounds": rounds, "candidates": candidates}


# ─── Recruiter Assignment ─────────────────────────────────────────────────────

@router.get("/requisitions/{req_id}/recruiters")
def get_req_recruiters(req_id: str, user: dict = Depends(get_current_user)):
    if user["role"] not in ("ta_manager", "admin", "recruiter"):
        raise HTTPException(403, "Not authorised")
    return query(
        """SELECT u.id, u.full_name, u.email, rr.is_owner, rr.assigned_at
           FROM requisition_recruiter rr
           JOIN app_user u ON u.id = rr.recruiter_id
           WHERE rr.requisition_id = %s
           ORDER BY rr.is_owner DESC, rr.assigned_at""",
        [req_id],
    )


class AssignRecruiterIn(BaseModel):
    recruiter_id: str


@router.post("/requisitions/{req_id}/assign-recruiter")
def assign_recruiter(req_id: str, body: AssignRecruiterIn, user: dict = Depends(get_current_user)):
    if user["role"] not in ("ta_manager", "admin"):
        raise HTTPException(403, "Only TA managers can assign recruiters")
    req = query_one("SELECT id FROM requisition WHERE id = %s", [req_id])
    if not req:
        raise HTTPException(404, "Requisition not found")
    recruiter = query_one(
        "SELECT id FROM app_user WHERE id = %s AND role = 'recruiter' AND is_active = true",
        [body.recruiter_id],
    )
    if not recruiter:
        raise HTTPException(404, "Active recruiter not found")
    query(
        """INSERT INTO requisition_recruiter (requisition_id, recruiter_id, is_owner, assigned_by)
           VALUES (%s, %s, false, %s)
           ON CONFLICT (requisition_id, recruiter_id) DO NOTHING""",
        [req_id, body.recruiter_id, user["sub"]],
        fetch=False,
    )
    return {"ok": True}


@router.delete("/requisitions/{req_id}/recruiters/{recruiter_id}")
def unassign_recruiter(req_id: str, recruiter_id: str, user: dict = Depends(get_current_user)):
    if user["role"] not in ("ta_manager", "admin"):
        raise HTTPException(403, "Only TA managers can remove assignments")
    query(
        "DELETE FROM requisition_recruiter WHERE requisition_id = %s AND recruiter_id = %s",
        [req_id, recruiter_id],
        fetch=False,
    )
    return {"ok": True}


# ─── Team ──────────────────────────────────────────────────────────────────────

@router.get("/team")
def get_team(user: dict = Depends(get_current_user)):
    if user["role"] not in ("ta_manager", "admin"):
        raise HTTPException(403, "TA Manager access required")
    return query(
        """
        SELECT u.id, u.full_name, u.email, u.role,
               COUNT(DISTINCT rr.requisition_id)
                 FILTER (WHERE r.status = 'open') AS active_req_count,
               COALESCE(
                 json_agg(
                   json_build_object('req_id', r.id, 'title', r.title, 'status', r.status)
                 ) FILTER (WHERE r.id IS NOT NULL AND r.status = 'open'),
                 '[]'::json
               ) AS assigned_requisitions
        FROM app_user u
        LEFT JOIN requisition_recruiter rr ON rr.recruiter_id = u.id
        LEFT JOIN requisition r ON r.id = rr.requisition_id
        WHERE u.role IN ('recruiter', 'ta_manager', 'hiring_manager')
          AND u.is_active = true
        GROUP BY u.id, u.full_name, u.email, u.role
        ORDER BY u.role, u.full_name
        """
    )


# ─── Candidates ───────────────────────────────────────────────────────────────

@router.get("/candidates")
def list_candidates(user: dict = Depends(get_current_user)):
    role = user["role"]
    uid  = user["sub"]
    # Recruiter LATERAL sub-select: owner recruiter of each requisition
    _rec_lat = """
        LEFT JOIN LATERAL (
            SELECT rr2.recruiter_id, ru.full_name AS recruiter_name
            FROM requisition_recruiter rr2
            JOIN app_user ru ON ru.id = rr2.recruiter_id
            WHERE rr2.requisition_id = r.id
            ORDER BY rr2.is_owner DESC NULLS LAST
            LIMIT 1
        ) rc_info ON true
    """
    if role == "recruiter":
        return query(
            f"""
            SELECT * FROM (
              SELECT DISTINCT ON (LOWER(c.email), r.id)
                c.id, c.full_name, c.email, c.gender,
                r.id AS req_id, r.title AS requisition, a.status,
                a.combined_score, a.match_score, a.id AS app_id,
                rc_info.recruiter_id, rc_info.recruiter_name,
                a.applied_at
              FROM candidate c
              JOIN application  a ON a.candidate_id = c.id
              JOIN requisition  r ON r.id = a.requisition_id
              JOIN requisition_recruiter rr
                   ON rr.requisition_id = r.id AND rr.recruiter_id = %s
              {_rec_lat}
              ORDER BY LOWER(c.email), r.id, a.combined_score DESC NULLS LAST, a.applied_at DESC
            ) deduped
            ORDER BY applied_at DESC
            """,
            [uid],
        )
    return query(
        f"""
        SELECT * FROM (
          SELECT DISTINCT ON (LOWER(c.email), r.id)
            c.id, c.full_name, c.email, c.gender,
            r.id AS req_id, r.title AS requisition, a.status,
            a.combined_score, a.match_score, a.id AS app_id,
            rc_info.recruiter_id, rc_info.recruiter_name,
            a.applied_at
          FROM candidate c
          JOIN application a ON a.candidate_id = c.id
          JOIN requisition r ON r.id = a.requisition_id
          {_rec_lat}
          ORDER BY LOWER(c.email), r.id, a.combined_score DESC NULLS LAST, a.applied_at DESC
        ) deduped
        ORDER BY applied_at DESC
        """,
        [],
    )


# ─── Interviews ───────────────────────────────────────────────────────────────

@router.get("/interviews")
def list_interviews(user: dict = Depends(get_current_user)):
    role = user["role"]
    uid  = user["sub"]

    _sc_status_sub = (
        "(SELECT s.status FROM scorecard s "
        "WHERE s.interview_id = i.id AND s.interviewer_id = %s LIMIT 1) AS my_scorecard_status"
    )
    _panel_sub = (
        "EXISTS(SELECT 1 FROM interview_panel ip "
        "WHERE ip.interview_id = i.id AND ip.interviewer_id = %s) AS is_on_panel"
    )

    _notes_sub = (
        "COALESCE((SELECT inotes.fetch_status FROM interview_notes inotes "
        "WHERE inotes.interview_id = i.id LIMIT 1), 'none') AS transcript_status"
    )

    if role == "recruiter":
        return query(
            f"""
            SELECT i.id, i.scheduled_at, i.status, i.meet_link, i.mode,
                   i.gcal_event_id,
                   c.full_name AS candidate_name, r.title AS requisition,
                   rc.name AS round_name, a.id AS application_id,
                   {_panel_sub},
                   {_sc_status_sub},
                   {_notes_sub}
            FROM interview i
            JOIN application  a  ON a.id  = i.application_id
            JOIN candidate    c  ON c.id  = a.candidate_id
            JOIN requisition  r  ON r.id  = a.requisition_id
            JOIN round_config rc ON rc.id = i.round_config_id
            JOIN requisition_recruiter rr
                 ON rr.requisition_id = r.id AND rr.recruiter_id = %s
            ORDER BY i.scheduled_at DESC NULLS LAST
            """,
            [uid, uid, uid],
        )

    if role == "interviewer":
        # Pure interviewers see only their own panel assignments
        return query(
            f"""
            SELECT i.id, i.scheduled_at, i.status, i.meet_link, i.mode,
                   i.gcal_event_id,
                   c.full_name AS candidate_name, r.title AS requisition,
                   rc.name AS round_name, a.id AS application_id,
                   TRUE AS is_on_panel,
                   {_sc_status_sub},
                   {_notes_sub}
            FROM interview i
            JOIN application  a  ON a.id  = i.application_id
            JOIN candidate    c  ON c.id  = a.candidate_id
            JOIN requisition  r  ON r.id  = a.requisition_id
            JOIN round_config rc ON rc.id = i.round_config_id
            JOIN interview_panel ip ON ip.interview_id = i.id AND ip.interviewer_id = %s
            ORDER BY i.scheduled_at DESC NULLS LAST
            """,
            [uid, uid],
        )

    return query(
        f"""
        SELECT i.id, i.scheduled_at, i.status, i.meet_link, i.mode,
               i.gcal_event_id,
               c.full_name AS candidate_name, r.title AS requisition,
               rc.name AS round_name, a.id AS application_id,
               {_panel_sub},
               {_sc_status_sub},
               {_notes_sub}
        FROM interview i
        JOIN application  a  ON a.id  = i.application_id
        JOIN candidate    c  ON c.id  = a.candidate_id
        JOIN requisition  r  ON r.id  = a.requisition_id
        JOIN round_config rc ON rc.id = i.round_config_id
        ORDER BY i.scheduled_at DESC NULLS LAST
        """,
        [uid, uid],
    )


# ─── Hiring-manager review ────────────────────────────────────────────────────

@router.get("/profiles-to-review")
def profiles_to_review(user: dict = Depends(get_current_user)):
    uid = user["sub"]
    return query(
        """
        SELECT a.id, c.full_name, c.email, c.gender,
               r.title AS req_title, r.id AS req_id,
               a.combined_score, a.match_score, a.status,
               a.hm_feedback, a.hm_reviewed_at
        FROM application a
        JOIN candidate  c ON c.id  = a.candidate_id
        JOIN requisition r ON r.id = a.requisition_id
        WHERE r.hiring_manager_id = %s
          AND a.status = 'interview'
          AND (a.hm_feedback IS NULL OR a.hm_feedback = '')
        ORDER BY a.combined_score DESC NULLS LAST
        """,
        [uid],
    )


class HMFeedbackIn(BaseModel):
    approved: bool
    comment: Optional[str] = None


# ─── CV Database (Admin / TA Manager / Recruiter) ────────────────────────────

@router.get("/cv-database")
def cv_database(user: dict = Depends(get_current_user)):
    role = user["role"]
    uid  = user["sub"]

    if role not in ("admin", "ta_manager", "recruiter"):
        raise HTTPException(403, "Not authorised to view CV database")

    # For recruiter, only show candidates from their requisitions
    if role == "recruiter":
        scope_where = """
            AND c.id IN (
                SELECT DISTINCT a_s.candidate_id
                FROM application a_s
                JOIN requisition_recruiter rr_s
                     ON rr_s.requisition_id = a_s.requisition_id
                     AND rr_s.recruiter_id = %s
            )
        """
        scope_params = [uid]
    else:
        scope_where  = ""
        scope_params = []

    summary = query_one(
        f"""
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
        WHERE 1=1 {scope_where}
        """,
        scope_params,
    )

    # DISTINCT ON (email) collapses duplicate candidate records for the same person.
    # Keeps the oldest record (first created) as the canonical row.
    candidates = query(
        f"""
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
          WHERE 1=1 {scope_where}
          ORDER BY LOWER(c.email), c.created_at ASC
        ) deduped
        ORDER BY registered_at DESC
        """,
        scope_params,
    )

    return {"summary": dict(summary) if summary else {}, "candidates": candidates}


@router.post("/applications/{app_id}/hm-feedback")
def hm_feedback(
    app_id: str,
    body: HMFeedbackIn,
    _user: dict = Depends(get_current_user),
):
    verdict = body.comment or ("Approved" if body.approved else "Not approved")
    row = query_one(
        """UPDATE application
           SET hm_feedback = %s, hm_reviewed_at = now()
           WHERE id = %s
           RETURNING id, status, hm_feedback""",
        [verdict, app_id],
    )
    if not row:
        raise HTTPException(404, "application not found")
    return row


# ─── Pipeline bifurcated view ─────────────────────────────────────────────────

@router.get("/requisitions/{req_id}/pipeline")
def get_req_pipeline(req_id: str, user: dict = Depends(get_current_user)):
    """
    Return all candidates for a requisition grouped by pipeline stage,
    with SLA/RAG status per candidate. Role-scoped.
    """
    role, uid = user["role"], user["sub"]

    # Role scope: recruiter must own the req
    if role == "recruiter":
        if not query_one(
            "SELECT 1 FROM requisition_recruiter WHERE requisition_id=%s AND recruiter_id=%s",
            [req_id, uid],
        ):
            raise HTTPException(403, "Not authorised")

    rows = query(
        """
        SELECT a.id AS app_id, a.status, a.ai_fit_score, a.bot_score,
               a.combined_score, a.match_score, a.current_round,
               a.screening_decision,
               c.full_name, c.email,
               EXTRACT(EPOCH FROM (
                   now() - COALESCE(
                       (SELECT se.occurred_at FROM stage_event se
                        WHERE se.application_id=a.id AND se.to_status=a.status
                        ORDER BY se.occurred_at DESC LIMIT 1),
                       a.applied_at
                   )
               ))/86400.0 AS elapsed_days
        FROM application a
        JOIN candidate c ON c.id=a.candidate_id
        WHERE a.requisition_id=%s
        ORDER BY a.applied_at DESC
        """,
        [req_id],
    ) or []

    cfg = load_config()

    # Fetch round_config for dynamic interview level names
    round_cfg = query(
        "SELECT sequence, name FROM round_config WHERE requisition_id=%s ORDER BY sequence",
        [req_id],
    ) or []
    round_names = {r["sequence"]: r["name"] for r in round_cfg}
    max_interview_rounds = max((r["sequence"] for r in round_cfg), default=0)

    stage_map = {s: [] for s in PIPELINE_STAGES}
    terminals = {"hired": [], "rejected": [], "on_hold": []}

    for r in rows:
        status = r["status"]
        elapsed = float(r["elapsed_days"] or 0)
        if status in TERMINAL:
            rag = None
        else:
            sla_key = STAGE_SLA_KEY.get(status, "stage_default")
            tgt = cfg.get(sla_key, cfg.get("stage_default", 5))
            rag = compute_rag(elapsed, tgt)

        entry = {
            "app_id":            str(r["app_id"]),
            "full_name":         r["full_name"],
            "email":             r["email"],
            "elapsed_days":      round(elapsed, 1),
            "ai_fit_score":      r["ai_fit_score"],
            "bot_score":         r["bot_score"],
            "combined_score":    r["combined_score"],
            "current_round":     r["current_round"],
            "screening_decision": r["screening_decision"],
            "rag":               rag,
        }
        if status in stage_map:
            stage_map[status].append(entry)
        elif status in terminals:
            terminals[status].append(entry)

    # Build per-level sub-groups for the interview stage
    interview_candidates = stage_map["interview"]
    level_map: dict[int, list] = {}
    for c in interview_candidates:
        lvl = int(c["current_round"] or 1)
        level_map.setdefault(lvl, []).append(c)
    interview_levels = [
        {
            "level":      lvl,
            "round_name": round_names.get(lvl, f"Level {lvl}"),
            "count":      len(cands),
            "candidates": cands,
        }
        for lvl, cands in sorted(level_map.items())
    ]

    req = query_one("SELECT title, req_code FROM requisition WHERE id=%s", [req_id])

    stages_out = []
    for s in PIPELINE_STAGES:
        stage_entry = {
            "stage":      s,
            "label":      PIPELINE_STAGE_LABELS.get(s, s.replace("_", " ").title()),
            "count":      len(stage_map[s]),
            "candidates": stage_map[s],
        }
        if s == "interview":
            stage_entry["levels"] = interview_levels
        stages_out.append(stage_entry)

    return {
        "req_id":               req_id,
        "title":                req["title"] if req else "",
        "req_code":             req["req_code"] if req else "",
        "max_interview_rounds": max_interview_rounds,
        "round_config":         [{"sequence": r["sequence"], "name": r["name"]} for r in round_cfg],
        "stages":               stages_out,
        "terminal":             {k: {"count": len(v), "candidates": v} for k, v in terminals.items()},
    }


# ─── One-click advance ────────────────────────────────────────────────────────

class AdvanceIn(BaseModel):
    target: Optional[str] = None   # "rejected" | "on_hold" | "hired" for terminal; None = auto-next


@router.post("/applications/{app_id}/advance")
def advance_application(
    app_id: str,
    body: AdvanceIn = AdvanceIn(),
    user: dict = Depends(get_current_user),
):
    """Advance (or terminate) a candidate in the pipeline. Logs to stage_event."""
    role = user["role"]
    if role not in ("recruiter", "ta_manager", "admin", "hiring_manager"):
        raise HTTPException(403, "Not authorised to advance candidates")

    app = query_one(
        "SELECT id, status, requisition_id, current_round FROM application WHERE id=%s", [app_id]
    )
    if not app:
        raise HTTPException(404, "Application not found")

    # Hiring manager may only advance on their own requisitions
    if role == "hiring_manager":
        req_id_str = str(app["requisition_id"]) if app["requisition_id"] else None
        if not req_id_str or not query_one(
            "SELECT 1 FROM requisition WHERE id=%s AND hiring_manager_id=%s",
            [req_id_str, user["sub"]],
        ):
            raise HTTPException(403, "Not authorised to advance candidates on this requisition")

    current = app["status"]
    req_id  = str(app["requisition_id"]) if app["requisition_id"] else None

    # Terminal move (reject / hold / hire)
    if body.target in ("rejected", "on_hold", "hired"):
        if current in TERMINAL:
            raise HTTPException(400, f"Already in terminal status '{current}'")
        query(
            "UPDATE application SET status=%s WHERE id=%s",
            [body.target, app_id], fetch=False,
        )
        query(
            "INSERT INTO stage_event (application_id, from_status, to_status, actor_id) VALUES (%s,%s,%s,%s)",
            [app_id, current, body.target, user["sub"]], fetch=False,
        )
        sync_plan_on_advance(app_id, body.target, current, req_id)
        return {"ok": True, "prev_stage": current, "new_stage": body.target}

    # Auto advance
    if current in TERMINAL:
        raise HTTPException(400, f"Application is in terminal status '{current}'")

    # ── Dynamic interview level advance ───────────────────────────────────────
    if current == "interview":
        current_round = int(app["current_round"] or 1)
        max_row = query_one(
            "SELECT COALESCE(MAX(sequence),0) AS max_seq FROM round_config WHERE requisition_id=%s",
            [req_id],
        )
        max_seq = int((max_row or {}).get("max_seq") or 1)

        if current_round < max_seq:
            # Advance to next interview level (status stays 'interview')
            new_round = current_round + 1
            round_name_row = query_one(
                "SELECT name FROM round_config WHERE requisition_id=%s AND sequence=%s",
                [req_id, new_round],
            )
            round_label = (round_name_row or {}).get("name") or f"Level {new_round}"
            query(
                "UPDATE application SET current_round=%s WHERE id=%s",
                [new_round, app_id], fetch=False,
            )
            query(
                "INSERT INTO stage_event (application_id, from_status, to_status, actor_id, note) VALUES (%s,%s,%s,%s,%s)",
                [app_id, current, current, user["sub"], f"Interview level {new_round}: {round_label}"],
                fetch=False,
            )
            return {
                "ok":         True,
                "prev_stage": "interview",
                "new_stage":  "interview",
                "level":      new_round,
                "level_name": round_label,
            }
        else:
            # All levels complete — advance to documentation
            query(
                "UPDATE application SET status='documentation' WHERE id=%s",
                [app_id], fetch=False,
            )
            query(
                "INSERT INTO stage_event (application_id, from_status, to_status, actor_id) VALUES (%s,%s,%s,%s)",
                [app_id, "interview", "documentation", user["sub"]], fetch=False,
            )
            return {
                "ok":         True,
                "prev_stage": "interview",
                "new_stage":  "documentation",
                "needs_offer": True,
            }

    # ── Standard next-stage advance ──────────────────────────────────────────
    next_stage = NEXT_STAGE.get(current)
    if not next_stage:
        raise HTTPException(400, f"No next stage after '{current}' (already at end of pipeline)")

    extra_set = ""
    extra_params: list = [app_id]
    # Set current_round=1 when entering interview for the first time
    if next_stage == "interview":
        # Skip interview if no rounds are configured — go straight to documentation
        rounds_count_row = query_one(
            "SELECT COALESCE(MAX(sequence),0) AS n FROM round_config WHERE requisition_id=%s",
            [req_id],
        )
        rounds_n = int((rounds_count_row or {}).get("n") or 0)
        if rounds_n == 0:
            next_stage = "documentation"
        else:
            extra_set = ", current_round=1"

    query(
        f"UPDATE application SET status=%s{extra_set} WHERE id=%s",
        [next_stage] + extra_params, fetch=False,
    )
    query(
        "INSERT INTO stage_event (application_id, from_status, to_status, actor_id) VALUES (%s,%s,%s,%s)",
        [app_id, current, next_stage, user["sub"]], fetch=False,
    )

    flags = {}
    if next_stage == "nexai_bot":
        flags["needs_nexai_invite"] = True
    elif next_stage == "documentation":
        flags["needs_offer"] = True

    sync_plan_on_advance(app_id, next_stage, current, req_id)
    return {"ok": True, "prev_stage": current, "new_stage": next_stage, **flags}


# ─── Manual screening decision ───────────────────────────────────────────────

class ScreenDecisionIn(BaseModel):
    decision: str   # "pass" | "hold" | "reject"
    notes: Optional[str] = None


@router.post("/applications/{app_id}/screen-decision")
def record_screen_decision(
    app_id: str,
    body: ScreenDecisionIn,
    user: dict = Depends(get_current_user),
):
    """Record recruiter's manual screening decision (pass/hold/reject) on an application."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    if body.decision not in ("pass", "hold", "reject"):
        raise HTTPException(400, "decision must be 'pass', 'hold', or 'reject'")

    row = query_one(
        """UPDATE application
           SET screening_decision=%s, screening_notes=%s,
               screened_by=%s, screened_at=now()
           WHERE id=%s
           RETURNING id, status, screening_decision""",
        [body.decision, body.notes, user["sub"], app_id],
    )
    if not row:
        raise HTTPException(404, "Application not found")

    # Reject decision immediately moves to terminal
    if body.decision == "reject":
        query("UPDATE application SET status='rejected' WHERE id=%s", [app_id], fetch=False)
        query(
            "INSERT INTO stage_event (application_id, from_status, to_status, actor_id, note) VALUES (%s,'screen','rejected',%s,'Rejected at screening')",
            [app_id, user["sub"]], fetch=False,
        )

    return row


# ─── Per-stage report ─────────────────────────────────────────────────────────

@router.get("/requisitions/{req_id}/stage/{stage}/report")
def stage_report(req_id: str, stage: str, user: dict = Depends(get_current_user)):
    """Return all candidates in a given stage for CSV export. Role-scoped."""
    role, uid = user["role"], user["sub"]
    if role == "recruiter":
        if not query_one(
            "SELECT 1 FROM requisition_recruiter WHERE requisition_id=%s AND recruiter_id=%s",
            [req_id, uid],
        ):
            raise HTTPException(403, "Not authorised")

    rows = query(
        """
        SELECT a.id AS app_id, c.full_name, c.email, c.gender,
               a.status, a.ai_fit_score, a.bot_score, a.combined_score,
               a.current_company, a.current_designation,
               a.notice_period_days, a.current_ctc_total, a.expected_ctc_total,
               a.applied_at,
               EXTRACT(EPOCH FROM (
                   now() - COALESCE(
                       (SELECT se.occurred_at FROM stage_event se
                        WHERE se.application_id=a.id AND se.to_status=a.status
                        ORDER BY se.occurred_at DESC LIMIT 1),
                       a.applied_at
                   )
               ))/86400.0 AS days_in_stage
        FROM application a
        JOIN candidate c ON c.id=a.candidate_id
        WHERE a.requisition_id=%s AND a.status=%s
        ORDER BY a.combined_score DESC NULLS LAST
        """,
        [req_id, stage],
    ) or []

    req = query_one("SELECT title, req_code FROM requisition WHERE id=%s", [req_id])

    return {
        "requisition": dict(req) if req else {},
        "stage": stage,
        "stage_label": PIPELINE_STAGE_LABELS.get(stage, stage),
        "count": len(rows),
        "rows": [dict(r) for r in rows],
    }
