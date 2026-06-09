"""
NexAI — voice-first interview bot (14a).

Question generation is rule-based (JD + key skills).
Scoring is keyword + depth + communication weighted model.
The face/avatar (14b) is intentionally NOT built here.
"""
import html
import io
import json
import os
import secrets
import tempfile
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import get_current_user
from ..services import avatar as _avatar_svc
from ..services import tts as _tts_svc
from ..services import prerender as _prerender_svc
from ..services.connectors import send_email
from ..services import interviewer_llm as _llm_svc

router = APIRouter(prefix="/api/nexai", tags=["nexai"])

# ── Question templates ────────────────────────────────────────────────────────

_SKILL_Q = [
    "Describe a project where you applied {skill} and what you achieved.",
    "What are the most common challenges you face with {skill}, and how do you overcome them?",
    "How do you stay current with developments in {skill}?",
    "Rate your experience level with {skill} and walk me through how you've used it.",
    "Give me a concrete example of a problem you solved using {skill}.",
]

_GENERIC_Q = [
    "Tell me about yourself and the experience most relevant to this role.",
    "Describe a time you handled a tight deadline or competing priorities.",
    "What is your biggest professional achievement in the last two years?",
    "Where do you see your career heading in the next two to three years?",
    "Why are you interested in this role specifically?",
]


def _generate_questions(key_skills: list, job_description: str) -> list:
    questions = []

    # Opening generic question
    questions.append({
        "seq": 1,
        "text": _GENERIC_Q[0],
        "expected_keywords": ["experience", "background", "role", "work", "team"],
    })

    # Skill-based questions (up to 4)
    for i, skill in enumerate(key_skills[:4]):
        tmpl = _SKILL_Q[i % len(_SKILL_Q)]
        questions.append({
            "seq": len(questions) + 1,
            "text": tmpl.format(skill=skill),
            "expected_keywords": [w.lower() for w in skill.split()] + ["project", "used", "built", "implemented"],
        })

    # JD-derived context question
    if job_description:
        jd_words = [w for w in job_description.split() if len(w) > 5][:6]
        if jd_words:
            questions.append({
                "seq": len(questions) + 1,
                "text": f"Tell me about your experience relevant to: {', '.join(jd_words[:4])}.",
                "expected_keywords": [w.lower() for w in jd_words],
            })

    # Closing generic questions
    for gq in _GENERIC_Q[1:3]:
        questions.append({
            "seq": len(questions) + 1,
            "text": gq,
            "expected_keywords": ["deadline", "priority", "achievement", "result", "impact", "career"],
        })

    return questions[:8]  # cap at 8 questions


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score_transcript(questions: list, transcript: list) -> tuple:
    answer_map = {t["seq"]: t.get("answer", "") for t in transcript}
    per_q = []
    for q in questions:
        answer = answer_map.get(q["seq"], "").lower()
        keywords = q.get("expected_keywords", [])
        words = answer.split()
        hits = sum(1 for k in keywords if k in answer)
        relevance   = min(hits / max(len(keywords), 1), 1.0)
        depth       = min(len(words) / 50.0, 1.0)
        communication = 1.0 if len(words) >= 10 else (len(words) / 10.0)
        q_score = round((relevance * 0.5 + depth * 0.3 + communication * 0.2) * 10, 1)
        per_q.append(q_score)

    raw_score = round(sum(per_q) / max(len(per_q), 1) * 10, 1)
    detail = {
        "per_question": per_q,
        "questions_answered": len([t for t in transcript if t.get("answer", "").strip()]),
        "total_questions": len(questions),
    }
    return min(raw_score, 100.0), detail


# ── Pydantic models ───────────────────────────────────────────────────────────

class StartSessionIn(BaseModel):
    application_id: str


class TranscriptEntry(BaseModel):
    seq: int
    question: str
    answer: str


class SubmitSessionIn(BaseModel):
    transcript: list[TranscriptEntry]


class QuestionIn(BaseModel):
    seq: int
    text: str
    expected_keywords: list[str] = []


class RequisitionQuestionsIn(BaseModel):
    questions: list[QuestionIn]


class ConverseIn(BaseModel):
    candidate_text: Optional[str] = None


class TerminateSessionIn(BaseModel):
    token: str
    strike_count: int
    reason: str = ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _nexai_mode() -> str:
    return os.environ.get("NEXAI_MODE", "scripted").lower()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/sessions", status_code=201)
def start_session(body: StartSessionIn, _user: dict = Depends(get_current_user)):
    app_row = query_one(
        """SELECT a.id, a.requisition_id, r.key_skills, r.job_description
           FROM application a JOIN requisition r ON r.id = a.requisition_id
           WHERE a.id = %s""",
        [body.application_id],
    )
    if not app_row:
        raise HTTPException(404, "Application not found")

    key_skills = app_row["key_skills"] or []
    jd = app_row["job_description"] or ""
    questions = _generate_questions(key_skills, jd)

    # Upsert session (one per application)
    existing = query_one(
        "SELECT id FROM nexai_session WHERE application_id = %s",
        [body.application_id],
    )
    if existing:
        query(
            """UPDATE nexai_session
               SET questions = %s::jsonb, status = 'in_progress',
                   started_at = now(), transcript = NULL,
                   raw_score = NULL, score_detail = NULL
               WHERE id = %s""",
            [json.dumps(questions), existing["id"]],
            fetch=False,
        )
        session_id = existing["id"]
    else:
        row = query_one(
            """INSERT INTO nexai_session
               (application_id, requisition_id, questions, status, started_at)
               VALUES (%s, %s, %s::jsonb, 'in_progress', now())
               RETURNING id""",
            [body.application_id, app_row["requisition_id"], json.dumps(questions)],
        )
        session_id = row["id"]

    return {"session_id": session_id, "questions": questions}


@router.post("/sessions/{session_id}/submit")
def submit_session(
    session_id: str,
    body: SubmitSessionIn,
    _user: dict = Depends(get_current_user),
):
    sess = query_one(
        "SELECT id, application_id, questions FROM nexai_session WHERE id = %s",
        [session_id],
    )
    if not sess:
        raise HTTPException(404, "Session not found")

    questions = sess["questions"] if isinstance(sess["questions"], list) else []
    transcript = [t.dict() for t in body.transcript]
    raw_score, detail = _score_transcript(questions, transcript)

    query(
        """UPDATE nexai_session
           SET transcript = %s::jsonb, raw_score = %s, score_detail = %s::jsonb,
               status = 'completed', completed_at = now()
           WHERE id = %s""",
        [json.dumps(transcript), raw_score, json.dumps(detail), session_id],
        fetch=False,
    )

    # Update application bot_score and combined_score
    app_row = query_one(
        "SELECT match_score FROM application WHERE id = %s",
        [sess["application_id"]],
    )
    match = float(app_row["match_score"] or 0) if app_row else 0
    combined = round(0.4 * match + 0.6 * raw_score, 1)
    query(
        "UPDATE application SET bot_score = %s, combined_score = %s WHERE id = %s",
        [raw_score, combined, sess["application_id"]],
        fetch=False,
    )

    return {"session_id": session_id, "raw_score": raw_score, "score_detail": detail}


@router.get("/sessions/{session_id}")
def get_session(session_id: str, _user: dict = Depends(get_current_user)):
    row = query_one("SELECT * FROM nexai_session WHERE id = %s", [session_id])
    if not row:
        raise HTTPException(404, "Session not found")
    return row


@router.get("/sessions/{session_id}/render-status")
def get_render_status(session_id: str, _user: dict = Depends(get_current_user)):
    """
    Return avatar pre-render status and per-question video URLs for a session.
    Frontend polls this before the candidate starts to determine if MP4s are ready.
    render_status values: pending | rendering | ready | partial | failed
    A 'failed' or 'partial' status is not an error — the orb takes over for any
    question whose video_url is null or status is 'failed'.
    """
    row = query_one(
        "SELECT render_status, question_videos FROM nexai_session WHERE id = %s",
        [session_id],
    )
    if not row:
        raise HTTPException(404, "Session not found")
    return {
        "session_id": session_id,
        "render_status": row.get("render_status") or "pending",
        "question_videos": row.get("question_videos") or [],
    }


@router.get("/sessions")
def list_sessions(
    user: dict = Depends(get_current_user),
    status: Optional[str] = None,
    score_min: Optional[float] = None,
    score_max: Optional[float] = None,
):
    """Role-scoped list of NexAI sessions with candidate info. Filterable."""
    role = user["role"]
    uid  = user["sub"]

    join_parts  = []
    where_parts = []
    params: list = []

    # Role scoping
    if role == "recruiter":
        join_parts.append(
            "JOIN requisition_recruiter rr_scope "
            "ON rr_scope.requisition_id = r.id AND rr_scope.recruiter_id = %s"
        )
        params.append(uid)
    elif role == "hiring_manager":
        where_parts.append("r.hiring_manager_id = %s")
        params.append(uid)
    # ta_manager / admin: sees all

    # Optional filters
    if status == "pending":
        where_parts.append("ns.status IN ('pending','in_progress')")
    elif status:
        where_parts.append("ns.status = %s")
        params.append(status)

    if score_min is not None:
        where_parts.append("ns.raw_score >= %s")
        params.append(score_min)

    if score_max is not None:
        where_parts.append("ns.raw_score <= %s")
        params.append(score_max)

    join_sql  = "\n    ".join(join_parts)
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    return query(
        f"""
        SELECT ns.id, ns.status, ns.raw_score, ns.created_at,
               ns.started_at, ns.completed_at,
               ROUND(
                 EXTRACT(EPOCH FROM (ns.completed_at - ns.started_at)) / 60.0
                 ::numeric, 1
               ) AS duration_min,
               c.full_name   AS candidate_name,
               c.email       AS candidate_email,
               r.title       AS req_title,
               r.id          AS req_id,
               a.id          AS app_id,
               rec.full_name AS recruiter_name,
               ps.id         AS proctoring_session_id,
               ps.flag_count AS proctor_flag_count,
               (ps.id IS NOT NULL) AS has_proctoring
        FROM nexai_session ns
        JOIN application  a   ON a.id  = ns.application_id
        JOIN candidate    c   ON c.id  = a.candidate_id
        JOIN requisition  r   ON r.id  = ns.requisition_id
        LEFT JOIN proctoring_session ps ON ps.nexai_session_id = ns.id
        {join_sql}
        LEFT JOIN LATERAL (
            SELECT u2.full_name
            FROM requisition_recruiter rr2
            JOIN app_user u2 ON u2.id = rr2.recruiter_id
            WHERE rr2.requisition_id = r.id
            ORDER BY rr2.is_owner DESC NULLS LAST LIMIT 1
        ) rec ON true
        {where_sql}
        ORDER BY ns.created_at DESC
        LIMIT 200
        """,
        params,
    )


# ── Per-Requisition Question Editor ──────────────────────────────────────────

@router.get("/requisitions/{req_id}/questions")
def get_req_questions(
    req_id: str,
    defaults: bool = False,
    user: dict = Depends(get_current_user),
):
    """
    Return the question set for a requisition.
    - defaults=False (default): return the saved custom set if one exists (saved=True),
      otherwise return auto-generated defaults without persisting (saved=False).
    - defaults=True: always return auto-generated defaults regardless of any saved set.
    """
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    req = query_one(
        "SELECT key_skills, job_description FROM requisition WHERE id = %s",
        [req_id],
    )
    if not req:
        raise HTTPException(404, "Requisition not found")

    if not defaults:
        saved = query_one(
            "SELECT questions, updated_at FROM requisition_questions WHERE requisition_id = %s",
            [req_id],
        )
        if saved:
            return {
                "saved": True,
                "questions": saved["questions"],
                "updated_at": saved["updated_at"].isoformat() if saved["updated_at"] else None,
            }

    auto = _generate_questions(
        req.get("key_skills") or [], req.get("job_description") or ""
    )
    return {"saved": False, "questions": auto, "updated_at": None}


@router.put("/requisitions/{req_id}/questions")
def save_req_questions(
    req_id: str,
    body: RequisitionQuestionsIn,
    user: dict = Depends(get_current_user),
):
    """Upsert the custom question set for a requisition."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    if not query_one("SELECT id FROM requisition WHERE id = %s", [req_id]):
        raise HTTPException(404, "Requisition not found")

    if not body.questions:
        raise HTTPException(400, "At least one question is required")

    bad = [i + 1 for i, q in enumerate(body.questions) if not q.text.strip()]
    if bad:
        raise HTTPException(400, f"Question(s) {bad} have empty text")

    questions = [
        {"seq": i + 1, "text": q.text.strip(), "expected_keywords": q.expected_keywords}
        for i, q in enumerate(body.questions)
    ]
    query(
        """INSERT INTO requisition_questions (requisition_id, questions, updated_at, updated_by)
           VALUES (%s, %s::jsonb, now(), %s)
           ON CONFLICT (requisition_id)
           DO UPDATE SET questions   = EXCLUDED.questions,
                         updated_at = now(),
                         updated_by = EXCLUDED.updated_by""",
        [req_id, json.dumps(questions), user["sub"]],
        fetch=False,
    )
    return {"saved": True, "questions": questions}


@router.delete("/requisitions/{req_id}/questions")
def delete_req_questions(req_id: str, user: dict = Depends(get_current_user)):
    """Remove the saved question set — future invites revert to auto-generation."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    query(
        "DELETE FROM requisition_questions WHERE requisition_id = %s",
        [req_id],
        fetch=False,
    )
    return {"ok": True}


# ── Session Transcript (recruiter read-only) ─────────────────────────────────

@router.get("/sessions/{session_id}/transcript")
def get_session_transcript(session_id: str, user: dict = Depends(get_current_user)):
    """
    Return the full transcript or conversation for a completed NexAI session.
    Recruiter JWT required. Recruiters may only access sessions on their requisitions;
    TA managers and admins see all.
    """
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    scope_join = ""
    params: list = []
    if user["role"] == "recruiter":
        scope_join = (
            "JOIN requisition_recruiter rr "
            "  ON rr.requisition_id = r.id AND rr.recruiter_id = %s"
        )
        params.append(user["sub"])
    params.append(session_id)

    row = query_one(
        f"""
        SELECT ns.id, ns.transcript, ns.conversation,
               ns.raw_score, ns.score_detail, ns.status, ns.completed_at,
               c.full_name  AS candidate_name,
               c.email      AS candidate_email,
               r.title      AS requisition,
               r.id         AS requisition_id
        FROM nexai_session ns
        JOIN application a  ON a.id = ns.application_id
        JOIN candidate   c  ON c.id = a.candidate_id
        JOIN requisition r  ON r.id = a.requisition_id
        {scope_join}
        WHERE ns.id = %s
        """,
        params,
    )
    if not row:
        raise HTTPException(404, "Session not found or not accessible")

    # Infer mode from which data column is populated
    mode = "conversational" if row.get("conversation") else "scripted"

    return {
        "session_id":     str(row["id"]),
        "mode":           mode,
        "status":         row["status"],
        "completed_at":   row["completed_at"].isoformat() if row["completed_at"] else None,
        "candidate_name": row["candidate_name"],
        "candidate_email":row["candidate_email"],
        "requisition":    row["requisition"],
        "requisition_id": str(row["requisition_id"]),
        "raw_score":      float(row["raw_score"]) if row["raw_score"] is not None else None,
        "score_detail":   row["score_detail"] or {},
        "transcript":     row["transcript"]   or [],
        "conversation":   row["conversation"] or [],
    }


# ── NexAI Invite Tracker ─────────────────────────────────────────────────────

@router.get("/invite-tracker")
def invite_tracker(user: dict = Depends(get_current_user)):
    """
    Returns all NexAI invites with status breakdown.
    Recruiters see only their requisitions; TA managers / admins see all.
    """
    role = user["role"]
    uid  = user["sub"]

    scope_join  = ""
    scope_where = ""
    params: list = []

    if role == "recruiter":
        scope_join  = "JOIN requisition_recruiter rr_s ON rr_s.requisition_id = r.id AND rr_s.recruiter_id = %s"
        params.append(uid)
    elif role not in ("ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    # DISTINCT ON (a.id) keeps only the most-recently-sent invite per application,
    # so re-sending an invite never inflates the tracker row count.
    rows = query(
        f"""
        SELECT * FROM (
            SELECT DISTINCT ON (a.id)
                ni.id            AS invite_id,
                ni.invited_at,
                ni.expires_at,
                ni.used_at,
                c.full_name      AS candidate_name,
                c.email          AS candidate_email,
                r.id             AS req_id,
                r.title          AS requisition,
                a.id             AS app_id,
                ns.id            AS session_id,
                ns.status        AS session_status,
                ns.started_at,
                ns.completed_at,
                ns.raw_score,
                ub.full_name     AS invited_by,
                CASE
                  WHEN ns.status = 'completed'    THEN 'completed'
                  WHEN ni.used_at IS NOT NULL      THEN 'in_progress'
                  WHEN ni.expires_at < now()       THEN 'expired'
                  ELSE 'pending'
                END              AS invite_status
            FROM nexai_invite ni
            JOIN application  a  ON a.id  = ni.application_id
            JOIN candidate    c  ON c.id  = a.candidate_id
            JOIN requisition  r  ON r.id  = a.requisition_id
            {scope_join}
            LEFT JOIN nexai_session  ns ON ns.application_id = a.id
            LEFT JOIN app_user       ub ON ub.id = ni.created_by
            ORDER BY a.id, ni.invited_at DESC
        ) latest_invite
        ORDER BY invited_at DESC
        """,
        params,
    )

    # Build summary counts
    counts = {"total": 0, "pending": 0, "in_progress": 0, "completed": 0, "expired": 0}
    for r in (rows or []):
        counts["total"] += 1
        s = r.get("invite_status", "pending")
        if s in counts:
            counts[s] += 1

    return {"summary": counts, "invites": rows or []}


# ── Candidate Invite Flow ─────────────────────────────────────────────────────

@router.post("/invite/send/{app_id}", status_code=201)
def create_nexai_invite(
    app_id: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """Recruiter sends an AI interview invite link to the candidate's email."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    app_row = query_one(
        """SELECT a.id, a.status, c.full_name, c.email,
                  r.id AS requisition_id, r.title AS job_title,
                  r.key_skills, r.job_description,
                  gc.name AS company
           FROM application a
           JOIN candidate   c  ON c.id = a.candidate_id
           JOIN requisition r  ON r.id = a.requisition_id
           JOIN business_unit bu ON bu.id = r.bu_id
           JOIN group_company gc ON gc.id = bu.company_id
           WHERE a.id = %s""",
        [app_id],
    )
    if not app_row:
        raise HTTPException(404, "Application not found")
    if not app_row["email"]:
        raise HTTPException(400, "Candidate has no email address on record")

    token = secrets.token_urlsafe(32)
    query(
        """INSERT INTO nexai_invite (application_id, token, created_by)
           VALUES (%s, %s, %s)""",
        [app_id, token, user["sub"]],
        fetch=False,
    )

    # Create the nexai_session now (if not already present) so avatar videos can
    # be pre-rendered before the candidate opens their link.
    # start_invited_session preserves these questions, keeping video URLs valid.
    #
    # Question source priority:
    #   1. Saved custom set on requisition_questions (recruiter has edited it).
    #   2. Auto-generation from key_skills + job_description (original behaviour,
    #      used for every requisition that has never been edited).
    _saved_qs = query_one(
        "SELECT questions FROM requisition_questions WHERE requisition_id = %s",
        [app_row["requisition_id"]],
    )
    _questions = (
        list(_saved_qs["questions"]) if _saved_qs
        else _generate_questions(app_row.get("key_skills") or [], app_row.get("job_description") or "")
    )
    _existing_sess = query_one(
        "SELECT id FROM nexai_session WHERE application_id = %s", [app_id]
    )
    if _existing_sess:
        _prerender_session_id = _existing_sess["id"]
    else:
        _sess_row = query_one(
            """INSERT INTO nexai_session
               (application_id, requisition_id, questions, status)
               VALUES (%s, %s, %s::jsonb, 'pending') RETURNING id""",
            [app_id, app_row["requisition_id"], json.dumps(_questions)],
        )
        _prerender_session_id = _sess_row["id"]

    # Fire avatar pre-render as a background task.
    # Completely safe when GPU is not deployed — pipeline logs a warning and exits,
    # leaving all question_videos as failed so the frontend orb takes over.
    # TODO: replace FastAPI BackgroundTasks with Celery/RQ for production reliability.
    background_tasks.add_task(
        _prerender_svc.prerender_interview_videos, _prerender_session_id
    )

    # Read base_url from DB Settings (Admin → Settings → App Base URL)
    # Falls back to env var, then to localhost:8080 default
    from ..services.connectors import _load_email_cfg
    base_url = (_load_email_cfg().get("base_url") or
                os.environ.get("APP_BASE_URL", "http://localhost:8080")).rstrip("/")
    invite_url = f"{base_url}/nexai-interview?token={token}"

    name    = app_row["full_name"]
    job     = app_row["job_title"]
    company = app_row["company"]

    plain = (
        f"Hi {name},\n\n"
        f"Congratulations! You have been shortlisted for an AI Screening Interview "
        f"for the position of {job} at {company}.\n\n"
        f"Please use the link below to attend your interview at your convenience:\n\n"
        f"  {invite_url}\n\n"
        f"The interview takes approximately 25-30 minutes. "
        f"You will need a microphone and a quiet environment.\n\n"
        f"Important:\n"
        f"- Once you start, you have 48 hours to complete the interview\n"
        f"- You can close and re-open the link within that window if needed\n"
        f"- NexAI never auto-rejects — all scores are reviewed by a human recruiter\n\n"
        f"Best regards,\nEgnex Hiring Team | {company}"
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f4f1;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f4f1;padding:32px 16px">
  <tr><td align="center">
  <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 16px rgba(0,0,0,0.08);max-width:600px;width:100%">

    <!-- Header -->
    <tr><td style="background:#1a1a1a;padding:22px 32px">
      <span style="font-size:24px;font-weight:800;color:#ffffff;letter-spacing:-0.5px">Egnex</span>
      <span style="font-size:24px;font-weight:800;color:#f15a22">.</span>
      <span style="font-size:12px;color:#9b9893;margin-left:12px;vertical-align:middle">One Click Hire</span>
    </td></tr>

    <!-- Orange accent bar -->
    <tr><td style="background:#f15a22;height:4px;font-size:0">&nbsp;</td></tr>

    <!-- Body -->
    <tr><td style="padding:36px 32px">
      <p style="font-size:15px;color:#1a1a1a;margin:0 0 6px">Hi <strong>{name}</strong>,</p>
      <p style="font-size:15px;color:#444444;line-height:1.6;margin:0 0 24px">
        Congratulations! You have been shortlisted for an <strong>AI Screening Interview</strong>.
        Please find the details below:
      </p>

      <!-- Job card -->
      <table width="100%" cellpadding="0" cellspacing="0" style="background:#faf9f6;border:1px solid #e5e3de;border-radius:8px;margin-bottom:28px">
        <tr><td style="padding:16px 20px">
          <p style="font-size:18px;font-weight:700;color:#1a1a1a;margin:0 0 4px">{job}</p>
          <p style="font-size:13px;color:#6b6760;margin:0">{company}</p>
        </td></tr>
      </table>

      <!-- What to expect -->
      <p style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#f15a22;margin:0 0 10px">What to expect</p>
      <table cellpadding="0" cellspacing="0" style="margin-bottom:28px">
        <tr><td style="padding:4px 0;font-size:14px;color:#444;line-height:1.5">🎤&nbsp; Up to 8 spoken questions about your experience &amp; skills</td></tr>
        <tr><td style="padding:4px 0;font-size:14px;color:#444;line-height:1.5">⏱&nbsp; Takes approximately <strong>25–30 minutes</strong></td></tr>
        <tr><td style="padding:4px 0;font-size:14px;color:#444;line-height:1.5">🔇&nbsp; Find a quiet place with a working microphone</td></tr>
        <tr><td style="padding:4px 0;font-size:14px;color:#444;line-height:1.5">✅&nbsp; NexAI <strong>never auto-rejects</strong> — all scores reviewed by a human recruiter</td></tr>
      </table>

      <!-- CTA Button -->
      <table width="100%" cellpadding="0" cellspacing="0" style="margin:32px 0">
        <tr><td align="center">
          <a href="{invite_url}"
             style="display:inline-block;background:#f15a22;color:#ffffff;padding:16px 48px;border-radius:8px;text-decoration:none;font-size:16px;font-weight:700;letter-spacing:.3px">
            Start My AI Interview
          </a>
        </td></tr>
        <tr><td align="center" style="padding-top:14px">
          <span style="font-size:12px;color:#9b9893">Or paste this link in your browser:</span><br>
          <a href="{invite_url}" style="font-size:12px;color:#f15a22;word-break:break-all">{invite_url}</a>
        </td></tr>
      </table>

      <hr style="border:none;border-top:1px solid #e5e3de;margin:28px 0">

      <!-- Notice -->
      <table cellpadding="0" cellspacing="0">
        <tr><td style="padding:3px 0;font-size:12px;color:#9b9893">⏳&nbsp; Once you start, you have <strong>48 hours</strong> to complete — you can close and re-open the link within that window.</td></tr>
        <tr><td style="padding:3px 0;font-size:12px;color:#9b9893">📧&nbsp; Reply to this email if you have any questions.</td></tr>
      </table>
    </td></tr>

    <!-- Footer -->
    <tr><td style="background:#f5f4f1;padding:16px 32px;text-align:center;border-top:1px solid #e5e3de">
      <p style="font-size:11px;color:#9b9893;margin:0">
        Powered by <strong>Egnex One Click Hire</strong> &nbsp;·&nbsp; {company}<br>
        This is an automated message — please do not reply directly to this address.
      </p>
    </td></tr>

  </table>
  </td></tr>
</table>
</body>
</html>"""

    try:
        send_email(
            app_row["email"],
            f"AI Interview Invitation: {job} — {company}",
            plain,
            html=html,
        )
        email_sent = True
        email_error = None
    except Exception as exc:
        email_sent = False
        email_error = str(exc)
        print(f"[nexai-invite] Email delivery failed: {exc}")

    return {
        "invite_url": invite_url,
        "sent_to": app_row["email"],
        "email_sent": email_sent,
        "email_error": email_error if not email_sent else None,
        "candidate_name": name,
        "job_title": job,
    }


@router.post("/resend-invite/{app_id}", status_code=201)
def resend_nexai_invite(
    app_id: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """
    Creates a fresh invite token for an application and resends the email.
    Used for expired or pending-too-long invites.
    """
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    # Expire any previous unused invite for this application
    query(
        """UPDATE nexai_invite SET expires_at = now() - interval '1 second'
           WHERE application_id = %s AND used_at IS NULL""",
        [app_id], fetch=False,
    )
    # Delegate to the main invite creator (re-triggers prerender; cache hits are instant)
    return create_nexai_invite(app_id, background_tasks, user)


@router.get("/invite/validate")
def validate_invite(token: str):
    """Public — validate a candidate interview token before showing the interview page."""
    row = query_one(
        """SELECT ni.id, ni.expires_at, ni.used_at,
                  c.full_name, r.title AS job_title, gc.name AS company,
                  ni.application_id, ns.status AS session_status
           FROM nexai_invite ni
           JOIN application  a  ON a.id  = ni.application_id
           JOIN candidate    c  ON c.id  = a.candidate_id
           JOIN requisition  r  ON r.id  = a.requisition_id
           JOIN business_unit bu ON bu.id = r.bu_id
           JOIN group_company gc ON gc.id = bu.company_id
           LEFT JOIN nexai_session ns ON ns.application_id = ni.application_id
           WHERE ni.token = %s""",
        [token],
    )
    if not row:
        return {"valid": False, "reason": "This interview link is invalid."}
    # Permanently closed after completion or proctoring termination
    if row["session_status"] in ("completed", "terminated_proctoring"):
        return {"valid": False, "reason": "already_completed"}
    exp = row["expires_at"]
    if exp and exp.replace(tzinfo=None) < datetime.utcnow():
        return {"valid": False, "reason": "This interview link has expired."}
    return {
        "valid": True,
        "candidate_name": row["full_name"],
        "job_title": row["job_title"],
        "company": row["company"],
        "application_id": str(row["application_id"]),
        "mode": _nexai_mode(),
    }


@router.post("/invite/begin")
def start_invited_session(token: str):
    """Public — candidate starts (or re-enters) a NexAI session.

    Policy:
    - First entry: marks token used_at and sets a 48-hour completion window.
    - Re-entry within 48 h: allowed — session is reset so candidate starts fresh.
    - Permanently blocked only if session status = 'completed' (interview submitted).
    """
    invite = query_one(
        """SELECT ni.id, ni.application_id, ni.expires_at, ni.used_at
           FROM nexai_invite ni WHERE ni.token = %s""",
        [token],
    )
    if not invite:
        raise HTTPException(400, "Invalid invite token")
    exp = invite["expires_at"]
    if exp and exp.replace(tzinfo=None) < datetime.utcnow():
        raise HTTPException(400, "This interview link has expired")

    app_row = query_one(
        """SELECT a.id, a.requisition_id, r.key_skills, r.job_description
           FROM application a JOIN requisition r ON r.id = a.requisition_id
           WHERE a.id = %s""",
        [invite["application_id"]],
    )
    if not app_row:
        raise HTTPException(404, "Application not found")

    existing = query_one(
        "SELECT id, status, questions FROM nexai_session WHERE application_id = %s",
        [invite["application_id"]],
    )
    # Permanently closed only once the interview is submitted
    if existing and existing["status"] == "completed":
        raise HTTPException(400, "This interview has already been completed")

    # First entry: stamp used_at and shrink the expiry to a 48-hour window
    if not invite["used_at"]:
        query(
            """UPDATE nexai_invite
               SET used_at = now(), expires_at = now() + INTERVAL '48 hours'
               WHERE id = %s""",
            [invite["id"]], fetch=False,
        )

    questions = _generate_questions(app_row["key_skills"] or [], app_row["job_description"] or "")

    if existing:
        existing_qs = existing.get("questions") or []
        if existing_qs:
            # Preserve questions so any pre-rendered video URLs remain valid
            query(
                """UPDATE nexai_session
                   SET status = 'in_progress', started_at = now(),
                       transcript = NULL, raw_score = NULL, score_detail = NULL,
                       conversation = NULL
                   WHERE id = %s""",
                [existing["id"]], fetch=False,
            )
            questions = existing_qs
        else:
            query(
                """UPDATE nexai_session
                   SET questions = %s::jsonb, status = 'in_progress',
                       started_at = now(), transcript = NULL,
                       raw_score = NULL, score_detail = NULL,
                       conversation = NULL
                   WHERE id = %s""",
                [json.dumps(questions), existing["id"]], fetch=False,
            )
        session_id = existing["id"]
    else:
        row = query_one(
            """INSERT INTO nexai_session
               (application_id, requisition_id, questions, status, started_at)
               VALUES (%s, %s, %s::jsonb, 'in_progress', now()) RETURNING id""",
            [invite["application_id"], app_row["requisition_id"], json.dumps(questions)],
        )
        session_id = row["id"]

    return {"session_id": session_id, "questions": questions}


@router.get("/invite/render-status")
def get_invite_render_status(token: str):
    """Public — candidate polls avatar pre-render status using their invite token."""
    inv = query_one(
        """SELECT ni.application_id
             FROM nexai_invite ni
            WHERE ni.token = %s AND ni.used_at IS NOT NULL""",
        [token],
    )
    if not inv:
        raise HTTPException(404, "Session not found")
    row = query_one(
        """SELECT id, render_status, question_videos
             FROM nexai_session
            WHERE application_id = %s""",
        [inv["application_id"]],
    )
    if not row:
        raise HTTPException(404, "Session not found")
    return {
        "session_id": str(row["id"]),
        "render_status": row.get("render_status") or "pending",
        "question_videos": row.get("question_videos") or [],
    }


# ── Completion email helpers ──────────────────────────────────────────────────

def _esc(s: str) -> str:
    return html.escape(str(s or ""))


def _build_completion_email_html(
    candidate_name: str,
    requisition_title: str,
    raw_score,
    score_detail: dict,
    transcript: list,
    conversation: list,
) -> str:
    sd   = score_detail or {}
    mode = "conversational" if conversation else "scripted"

    detail_html = ""
    if sd.get("strengths"):
        detail_html += (
            "<h3 style='margin:16px 0 4px;color:#1a7f37'>Strengths</h3>"
            f"<p style='margin:0 0 12px;line-height:1.5'>{_esc(sd['strengths'])}</p>"
        )
    if sd.get("concerns"):
        detail_html += (
            "<h3 style='margin:16px 0 4px;color:#b55c00'>Areas to Probe</h3>"
            f"<p style='margin:0 0 12px;line-height:1.5'>{_esc(sd['concerns'])}</p>"
        )
    if mode == "conversational" and isinstance(sd.get("per_dimension"), dict):
        pd = sd["per_dimension"]
        dim_rows = "".join(
            f"<tr><td style='padding:4px 12px 4px 0;color:#555'>{dim.title()}</td>"
            f"<td><span style='display:inline-block;width:{int(pd.get(dim, 0)) * 10}%;"
            f"max-width:120px;height:8px;background:#2d8cf0;border-radius:2px;min-width:2px'>"
            f"</span>&nbsp;<span style='font-size:12px;color:#555'>"
            f"{pd.get(dim, 0)}/10</span></td></tr>"
            for dim in ("relevance", "depth", "communication", "fit")
        )
        detail_html += (
            f"<h3 style='margin:16px 0 6px'>Dimension Scores</h3>"
            f"<table style='border-spacing:0'>{dim_rows}</table>"
        )
    elif mode == "scripted" and sd.get("questions_answered") is not None:
        detail_html += (
            f"<p style='margin:4px 0'><b>Questions answered:</b> "
            f"{sd['questions_answered']} / {sd.get('total_questions', '?')}</p>"
        )

    if mode == "conversational":
        turn_rows = ""
        for turn in (conversation or []):
            spk   = turn.get("speaker", "")
            label = "NexAI" if spk == "bot" else "Candidate"
            color = "#2d8cf0" if spk == "bot" else "#444"
            turn_rows += (
                f"<tr style='border-bottom:1px solid #f0f0f0'>"
                f"<td style='padding:7px 14px 7px 0;font-weight:600;color:{color};"
                f"white-space:nowrap;vertical-align:top'>{label}</td>"
                f"<td style='padding:7px 0;line-height:1.5;color:#222'>"
                f"{_esc(turn.get('text', ''))}</td></tr>"
            )
        transcript_html = (
            f"<table style='width:100%;border-collapse:collapse'>{turn_rows}</table>"
        )
    else:
        qa_blocks = ""
        for i, qa in enumerate(transcript or [], 1):
            qa_blocks += (
                f"<div style='margin-bottom:16px'>"
                f"<p style='margin:0 0 4px;font-weight:600;color:#222'>"
                f"Q{i}: {_esc(qa.get('question', ''))}</p>"
                f"<p style='margin:0;color:#444;line-height:1.5;padding-left:12px;"
                f"border-left:3px solid #ddd'>{_esc(qa.get('answer', ''))}</p></div>"
            )
        transcript_html = qa_blocks or "<p style='color:#888'>No transcript recorded.</p>"

    score_val   = int(raw_score) if raw_score is not None else None
    score_str   = f"{score_val}/100" if score_val is not None else "N/A"
    score_color = (
        "#1a7f37" if (score_val or 0) >= 70
        else "#b55c00" if (score_val or 0) >= 50
        else "#cf222e"
    )

    return (
        "<!DOCTYPE html><html><body style='font-family:Arial,sans-serif;"
        "max-width:700px;margin:0 auto;padding:24px;color:#222'>"
        "<h2 style='margin:0 0 4px;color:#111'>NexAI Interview Completed</h2>"
        "<p style='margin:0 0 20px;color:#888;font-size:12px'>"
        "Powered by Egnex · One Click Hire</p>"
        "<table style='border-collapse:collapse;margin-bottom:20px'>"
        f"<tr><td style='padding:4px 16px 4px 0;color:#555;font-weight:600'>Candidate</td>"
        f"<td>{_esc(candidate_name)}</td></tr>"
        f"<tr><td style='padding:4px 16px 4px 0;color:#555;font-weight:600'>Role</td>"
        f"<td>{_esc(requisition_title)}</td></tr>"
        f"<tr><td style='padding:4px 16px 4px 0;color:#555;font-weight:600'>AI Score</td>"
        f"<td><b style='color:{score_color};font-size:18px'>{score_str}</b></td></tr>"
        "</table>"
        f"{detail_html}"
        "<hr style='border:none;border-top:1px solid #eee;margin:20px 0'>"
        "<h3 style='margin:0 0 12px'>Full Interview Transcript</h3>"
        f"{transcript_html}"
        "<hr style='border:none;border-top:1px solid #eee;margin:24px 0 12px'>"
        "<p style='margin:0;font-size:11px;color:#aaa'>"
        "This email was sent automatically by NexAI. Do not reply.</p>"
        "</body></html>"
    )


def _fire_completion_email(session_id: str) -> None:
    """Background task — resolve recruiter, guard on email_sent, send, mark sent."""
    try:
        row = query_one(
            """SELECT u.email        AS recruiter_email,
                      c.full_name    AS candidate_name,
                      r.title        AS requisition_title,
                      ns.raw_score, ns.score_detail,
                      ns.transcript, ns.conversation,
                      ns.email_sent
               FROM nexai_session  ns
               JOIN application    a  ON a.id  = ns.application_id
               JOIN candidate      c  ON c.id  = a.candidate_id
               JOIN requisition    r  ON r.id  = a.requisition_id
               JOIN nexai_invite   ni ON ni.application_id = ns.application_id
               JOIN app_user       u  ON u.id  = ni.created_by
               WHERE ns.id = %s
               ORDER BY ni.invited_at DESC
               LIMIT 1""",
            [session_id],
        )
        if not row or row["email_sent"]:
            return

        sd   = row["score_detail"] or {}
        conv = row["conversation"] or []
        txn  = row["transcript"]   or []

        html_body = _build_completion_email_html(
            candidate_name=row["candidate_name"],
            requisition_title=row["requisition_title"],
            raw_score=row["raw_score"],
            score_detail=sd,
            transcript=txn,
            conversation=conv,
        )
        score_display = (
            f"{int(row['raw_score'])}/100"
            if row["raw_score"] is not None
            else "N/A"
        )
        plain = (
            f"NexAI Interview Completed\n\n"
            f"Candidate: {row['candidate_name']}\n"
            f"Role: {row['requisition_title']}\n"
            f"AI Score: {score_display}\n\n"
            f"Strengths:\n{sd.get('strengths', '—')}\n\n"
            f"Areas to Probe:\n{sd.get('concerns', '—')}\n"
        )
        send_email(
            to_email=row["recruiter_email"],
            subject=(
                f"NexAI interview completed — "
                f"{row['candidate_name']} — {row['requisition_title']}"
            ),
            body=plain,
            html=html_body,
        )
        query(
            "UPDATE nexai_session SET email_sent = TRUE WHERE id = %s",
            [session_id],
            fetch=False,
        )
    except Exception as exc:
        print(f"[nexai_email] completion email failed for session {session_id}: {exc}")


@router.post("/invite/converse")
async def converse_invite(token: str, body: ConverseIn, background_tasks: BackgroundTasks):
    """
    Public — drive one turn of a conversational (LLM-led) NexAI interview.

    Call with an empty/absent candidate_text on the very first turn to get the
    bot's opening question. Subsequent calls should include the candidate's spoken
    response. The endpoint returns the bot's next reply and signals when the
    interview is complete (is_complete=true), at which point the session is scored
    and written to the database exactly as the scripted submit flow does.

    Only active when NEXAI_MODE=conversational.
    """
    if _nexai_mode() != "conversational":
        raise HTTPException(400, "Conversational mode is not enabled (NEXAI_MODE=scripted)")

    # ── Token validation (mirrors start_invited_session) ─────────────────────
    invite = query_one(
        "SELECT id, application_id, expires_at, used_at FROM nexai_invite WHERE token = %s",
        [token],
    )
    if not invite:
        raise HTTPException(400, "Invalid invite token")
    exp = invite["expires_at"]
    if exp and exp.replace(tzinfo=None) < datetime.utcnow():
        raise HTTPException(400, "This interview link has expired")

    # ── Load session + role context ───────────────────────────────────────────
    sess = query_one(
        """SELECT ns.id, ns.status, ns.conversation, ns.application_id,
                  r.title, r.key_skills, r.job_description,
                  c.full_name AS candidate_name,
                  gc.name     AS company
           FROM nexai_session ns
           JOIN application   a  ON a.id  = ns.application_id
           JOIN candidate      c  ON c.id  = a.candidate_id
           JOIN requisition    r  ON r.id  = a.requisition_id
           JOIN business_unit  bu ON bu.id = r.bu_id
           JOIN group_company  gc ON gc.id = bu.company_id
           WHERE ns.application_id = %s""",
        [invite["application_id"]],
    )
    if not sess:
        raise HTTPException(404, "Session not found — call /api/nexai/invite/begin first")
    if sess["status"] in ("completed", "terminated_proctoring"):
        raise HTTPException(400, "This interview has already been completed")

    turns = list(sess["conversation"] or [])
    candidate_text = (body.candidate_text or "").strip()

    # ── Honest duration estimate spoken in the opening line ──────────────────
    _CONV_DURATION_ESTIMATE = "25 to 30 minutes"  # edit here to change the spoken estimate

    # ── First call: return hardcoded intro without hitting the LLM ───────────
    if not turns and not candidate_text:
        first_name = (sess["candidate_name"] or "").split()[0]
        greeting   = f"Hello, {first_name}!" if first_name else "Hello!"
        job_title  = sess["title"]  or "this role"
        company    = sess["company"] or "the company"
        intro = (
            f"{greeting} I'm NexAI, an AI interviewer from Egnex. "
            f"Thank you for applying for the {job_title} position at {company}. "
            f"I'll be conducting a brief screening interview today — it should take around "
            f"{_CONV_DURATION_ESTIMATE}, and I'll ask you a few questions about your experience and skills. "
            f"Just answer naturally, as you would with a human interviewer. "
            f"Whenever you're ready to begin, simply say yes."
        )
        turns.append({"speaker": "bot", "text": intro})
        query(
            "UPDATE nexai_session SET conversation = %s::jsonb WHERE id = %s",
            [json.dumps(turns), sess["id"]],
            fetch=False,
        )
        return {"reply": intro, "is_complete": False}

    # Append candidate's reply for subsequent turns
    if candidate_text:
        turns.append({"speaker": "candidate", "text": candidate_text})

    role_context = {
        "title": sess["title"] or "",
        "key_skills": sess["key_skills"] or [],
        "job_description": sess["job_description"] or "",
    }
    conversation_state = {"role_context": role_context, "turns": turns}

    # ── Get bot's next reply ──────────────────────────────────────────────────
    result = await _llm_svc.next_turn(conversation_state)
    reply       = result["reply"]
    is_complete = result["is_complete"]

    turns.append({"speaker": "bot", "text": reply})

    # ── If interview is done: score and write final results ───────────────────
    if is_complete:
        conversation_state["turns"] = turns
        score_result = await _llm_svc.score_transcript(conversation_state)
        raw_score = score_result["raw_score"]
        detail    = score_result["score_detail"]

        query(
            """UPDATE nexai_session
               SET conversation = %s::jsonb,
                   raw_score = %s, score_detail = %s::jsonb,
                   status = 'completed', completed_at = now()
               WHERE id = %s""",
            [json.dumps(turns), raw_score, json.dumps(detail), sess["id"]],
            fetch=False,
        )

        app_row = query_one(
            "SELECT match_score FROM application WHERE id = %s",
            [sess["application_id"]],
        )
        match    = float((app_row or {}).get("match_score") or 0)
        combined = round(0.4 * match + 0.6 * raw_score, 1)
        query(
            "UPDATE application SET bot_score = %s, combined_score = %s, status = 'screen_passed' WHERE id = %s",
            [raw_score, combined, sess["application_id"]],
            fetch=False,
        )
        background_tasks.add_task(_fire_completion_email, str(sess["id"]))
    else:
        query(
            "UPDATE nexai_session SET conversation = %s::jsonb WHERE id = %s",
            [json.dumps(turns), sess["id"]],
            fetch=False,
        )

    return {"reply": reply, "is_complete": is_complete}


@router.post("/invite/terminate")
async def terminate_invite_session(body: TerminateSessionIn, background_tasks: BackgroundTasks):
    """
    Public — called by the candidate's browser when 3 proctoring strikes are reached.

    Scores the partial transcript (LLM, with rule-based fallback) and writes the
    session as 'terminated_proctoring' so it cannot be resumed.  The recruiter
    dashboard will display the partial score alongside a termination indicator.
    """
    if _nexai_mode() != "conversational":
        raise HTTPException(400, "Conversational mode is not enabled")

    invite = query_one(
        "SELECT id, application_id, expires_at FROM nexai_invite WHERE token = %s",
        [body.token],
    )
    if not invite:
        raise HTTPException(400, "Invalid invite token")

    sess = query_one(
        """SELECT ns.id, ns.status, ns.conversation, ns.application_id,
                  r.title, r.key_skills, r.job_description
           FROM nexai_session ns
           JOIN application  a ON a.id = ns.application_id
           JOIN requisition  r ON r.id = a.requisition_id
           WHERE ns.application_id = %s""",
        [invite["application_id"]],
    )
    if not sess:
        raise HTTPException(404, "Session not found")
    if sess["status"] in ("completed", "terminated_proctoring"):
        return {"ok": True, "already_closed": True}

    turns = list(sess["conversation"] or [])

    # Score whatever partial transcript we have (best effort)
    role_ctx = {
        "title":           sess["title"],
        "key_skills":      sess["key_skills"] or [],
        "job_description": sess["job_description"] or "",
    }
    score_result = await _llm_svc.score_transcript({"role_context": role_ctx, "turns": turns})
    raw_score = score_result["raw_score"]
    detail    = score_result["score_detail"]
    detail["terminated_by_proctoring"] = True
    detail["strike_count"] = body.strike_count

    reason_text = body.reason or f"Auto-terminated after {body.strike_count} proctoring strikes"

    query(
        """UPDATE nexai_session
               SET status = 'terminated_proctoring',
                   raw_score = %s, score_detail = %s::jsonb,
                   termination_reason = %s,
                   completed_at = now()
             WHERE id = %s""",
        [raw_score, json.dumps(detail), reason_text, sess["id"]],
        fetch=False,
    )

    # Update application score (partial) — status stays at its current value
    # rather than 'screen_passed'; recruiters can filter by session status.
    app_row = query_one("SELECT match_score FROM application WHERE id = %s", [sess["application_id"]])
    match    = float((app_row or {}).get("match_score") or 0)
    combined = round(0.4 * match + 0.6 * raw_score, 1)
    query(
        "UPDATE application SET bot_score = %s, combined_score = %s WHERE id = %s",
        [raw_score, combined, sess["application_id"]],
        fetch=False,
    )

    return {"ok": True, "raw_score": raw_score}


@router.post("/invite/submit/{session_id}")
async def submit_invited_session(session_id: str, body: SubmitSessionIn, background_tasks: BackgroundTasks):
    """Public — candidate submits completed interview transcript.

    In scripted mode: scores the supplied transcript using the rule-based model.
    In conversational mode: if the converse endpoint already scored the session,
    returns that score immediately; otherwise runs LLM scoring on the stored
    conversation (edge-case safety valve).
    """
    sess = query_one(
        """SELECT id, application_id, questions, conversation,
                  raw_score, score_detail, status
           FROM nexai_session WHERE id = %s""",
        [session_id],
    )
    if not sess:
        raise HTTPException(404, "Session not found")

    # ── Conversational mode ───────────────────────────────────────────────────
    if _nexai_mode() == "conversational":
        # Already fully scored by the converse endpoint
        if sess["status"] == "completed" and sess["raw_score"] is not None:
            return {
                "session_id": session_id,
                "raw_score": float(sess["raw_score"]),
                "score_detail": sess["score_detail"] or {},
            }

        # Safety valve: score the stored conversation if converse didn't complete
        stored_turns = list(sess["conversation"] or [])
        app_meta = query_one(
            """SELECT a.match_score, r.title, r.key_skills, r.job_description
               FROM application a JOIN requisition r ON r.id = a.requisition_id
               WHERE a.id = %s""",
            [sess["application_id"]],
        )
        role_ctx = {
            "title": (app_meta or {}).get("title", ""),
            "key_skills": (app_meta or {}).get("key_skills") or [],
            "job_description": (app_meta or {}).get("job_description") or "",
        }
        score_result = await _llm_svc.score_transcript(
            {"role_context": role_ctx, "turns": stored_turns}
        )
        raw_score = score_result["raw_score"]
        detail    = score_result["score_detail"]

        query(
            """UPDATE nexai_session
               SET raw_score = %s, score_detail = %s::jsonb,
                   status = 'completed', completed_at = now()
               WHERE id = %s""",
            [raw_score, json.dumps(detail), session_id],
            fetch=False,
        )
        match    = float((app_meta or {}).get("match_score") or 0)
        combined = round(0.4 * match + 0.6 * raw_score, 1)
        query(
            "UPDATE application SET bot_score = %s, combined_score = %s, status = 'screen_passed' WHERE id = %s",
            [raw_score, combined, sess["application_id"]], fetch=False,
        )
        background_tasks.add_task(_fire_completion_email, session_id)
        return {"session_id": session_id, "raw_score": raw_score, "score_detail": detail}

    # ── Scripted mode (unchanged behaviour) ──────────────────────────────────
    questions  = sess["questions"] if isinstance(sess["questions"], list) else []
    transcript = [t.dict() for t in body.transcript]
    raw_score, detail = _score_transcript(questions, transcript)

    query(
        """UPDATE nexai_session
           SET transcript = %s::jsonb, raw_score = %s, score_detail = %s::jsonb,
               status = 'completed', completed_at = now()
           WHERE id = %s""",
        [json.dumps(transcript), raw_score, json.dumps(detail), session_id],
        fetch=False,
    )

    app_row = query_one(
        "SELECT match_score FROM application WHERE id = %s",
        [sess["application_id"]],
    )
    match    = float(app_row["match_score"] or 0) if app_row else 0
    combined = round(0.4 * match + 0.6 * raw_score, 1)
    query(
        "UPDATE application SET bot_score = %s, combined_score = %s, status = 'screen_passed' WHERE id = %s",
        [raw_score, combined, sess["application_id"]], fetch=False,
    )
    background_tasks.add_task(_fire_completion_email, session_id)

    return {"session_id": session_id, "raw_score": raw_score, "score_detail": detail}


@router.get("/health")
def nexai_health(user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin only")
    totals = query_one(
        """SELECT
             COUNT(*) AS total,
             COUNT(*) FILTER (WHERE status = 'completed') AS completed,
             COUNT(*) FILTER (WHERE status = 'failed')    AS failed,
             COUNT(*) FILTER (WHERE status = 'in_progress') AS in_progress,
             ROUND(AVG(raw_score) FILTER (WHERE status = 'completed')::numeric, 1) AS avg_score,
             COUNT(*) FILTER (WHERE created_at::date = CURRENT_DATE) AS today
           FROM nexai_session""",
        [],
    )
    recent = query(
        """SELECT id, application_id, status, raw_score, completed_at, started_at
           FROM nexai_session
           ORDER BY created_at DESC LIMIT 20""",
        [],
    )
    return {
        "bot_name": "NexAI",
        "version": "v1.0 — voice-first (14a)",
        "model": "Rule-based Q&A + keyword scoring",
        "status": "active",
        "avatar": _avatar_svc.get_config(),
        "total_sessions":     int(totals["total"])       if totals else 0,
        "completed_sessions": int(totals["completed"])   if totals else 0,
        "failed_sessions":    int(totals["failed"])      if totals else 0,
        "in_progress":        int(totals["in_progress"]) if totals else 0,
        "avg_score":          float(totals["avg_score"]) if totals and totals["avg_score"] else None,
        "sessions_today":     int(totals["today"])       if totals else 0,
        "recent_sessions":    recent,
    }


# ── A2: Avatar config endpoint ────────────────────────────────────────────────

@router.get("/avatar/config")
def avatar_config(_user: dict = Depends(get_current_user)):
    """Return current avatar provider config (A2 — swappable interface)."""
    return _avatar_svc.get_config()


# ── A3: Render question as speaking clip (GPU providers) ─────────────────────

class RenderQuestionIn(BaseModel):
    question_text: str
    face_id: str = "nexai-female"
    session_id: Optional[str] = None


@router.post("/render-question")
async def render_question(body: RenderQuestionIn, _user: dict = Depends(get_current_user)):
    """
    STEP A3 — Generate TTS audio for a question and render a lip-sync video
    using the configured avatar provider (sadtalker / wav2lip / vendor).

    For 'orb' provider: returns {video_url: null} immediately (frontend uses orb).
    For GPU providers: generates audio via edge-tts (neural, falls back to gTTS),
    sends to GPU service, returns video_url.
    Falls back to orb cleanly if TTS or GPU service fails.
    """
    provider = _avatar_svc.PROVIDER
    if provider == "orb":
        return {"video_url": None, "provider": "orb", "fallback": False}

    # Generate TTS audio file for GPU rendering
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            audio_path = tf.name
        await _tts_svc.synthesize_speech(body.question_text, audio_path)
    except Exception as exc:
        return {"video_url": None, "provider": "orb", "fallback": True, "reason": str(exc)}

    try:
        result = _avatar_svc.render_speaking_clip(body.face_id, audio_path)
    finally:
        try:
            os.unlink(audio_path)
        except Exception:
            pass
    return result
