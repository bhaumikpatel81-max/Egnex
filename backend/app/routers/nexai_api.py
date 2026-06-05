"""
NexAI — voice-first interview bot (14a).

Question generation is rule-based (JD + key skills).
Scoring is keyword + depth + communication weighted model.
The face/avatar (14b) is intentionally NOT built here.
"""
import io
import json
import os
import secrets
import tempfile
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import get_current_user
from ..services import avatar as _avatar_svc
from ..services.connectors import send_email

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
               rec.full_name AS recruiter_name
        FROM nexai_session ns
        JOIN application  a   ON a.id  = ns.application_id
        JOIN candidate    c   ON c.id  = a.candidate_id
        JOIN requisition  r   ON r.id  = ns.requisition_id
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

    rows = query(
        f"""
        SELECT
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
        ORDER BY ni.invited_at DESC
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

@router.post("/invite/{app_id}", status_code=201)
def create_nexai_invite(app_id: str, user: dict = Depends(get_current_user)):
    """Recruiter sends an AI interview invite link to the candidate's email."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    app_row = query_one(
        """SELECT a.id, a.status, c.full_name, c.email,
                  r.title AS job_title, gc.name AS company
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
        f"The interview takes approximately 10-15 minutes. "
        f"You will need a microphone and a quiet environment.\n\n"
        f"Important:\n"
        f"- This link is valid for 7 days\n"
        f"- It can only be used once\n"
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
        <tr><td style="padding:4px 0;font-size:14px;color:#444;line-height:1.5">⏱&nbsp; Takes approximately <strong>10–15 minutes</strong></td></tr>
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
        <tr><td style="padding:3px 0;font-size:12px;color:#9b9893">⏳&nbsp; This link is valid for <strong>7 days</strong> and can only be used <strong>once</strong>.</td></tr>
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
def resend_nexai_invite(app_id: str, user: dict = Depends(get_current_user)):
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
    # Delegate to the main invite creator
    return create_nexai_invite(app_id, user)


@router.get("/invite/validate")
def validate_invite(token: str):
    """Public — validate a candidate interview token before showing the interview page."""
    row = query_one(
        """SELECT ni.id, ni.expires_at, ni.used_at,
                  c.full_name, r.title AS job_title, gc.name AS company,
                  ni.application_id
           FROM nexai_invite ni
           JOIN application  a  ON a.id  = ni.application_id
           JOIN candidate    c  ON c.id  = a.candidate_id
           JOIN requisition  r  ON r.id  = a.requisition_id
           JOIN business_unit bu ON bu.id = r.bu_id
           JOIN group_company gc ON gc.id = bu.company_id
           WHERE ni.token = %s""",
        [token],
    )
    if not row:
        return {"valid": False, "reason": "This interview link is invalid."}
    if row["used_at"]:
        return {"valid": False, "reason": "This interview link has already been used."}
    exp = row["expires_at"]
    if exp and exp.replace(tzinfo=None) < datetime.utcnow():
        return {"valid": False, "reason": "This interview link has expired."}
    return {
        "valid": True,
        "candidate_name": row["full_name"],
        "job_title": row["job_title"],
        "company": row["company"],
        "application_id": str(row["application_id"]),
    }


@router.post("/invite/start")
def start_invited_session(token: str):
    """Public — candidate starts the NexAI session using their invite token."""
    invite = query_one(
        """SELECT ni.id, ni.application_id, ni.expires_at, ni.used_at
           FROM nexai_invite ni WHERE ni.token = %s""",
        [token],
    )
    if not invite:
        raise HTTPException(400, "Invalid invite token")
    if invite["used_at"]:
        raise HTTPException(400, "This interview link has already been used")
    exp = invite["expires_at"]
    if exp and exp.replace(tzinfo=None) < datetime.utcnow():
        raise HTTPException(400, "This interview link has expired")

    # Mark token as used
    query(
        "UPDATE nexai_invite SET used_at = now() WHERE id = %s",
        [invite["id"]], fetch=False,
    )

    app_row = query_one(
        """SELECT a.id, a.requisition_id, r.key_skills, r.job_description
           FROM application a JOIN requisition r ON r.id = a.requisition_id
           WHERE a.id = %s""",
        [invite["application_id"]],
    )
    if not app_row:
        raise HTTPException(404, "Application not found")

    questions = _generate_questions(app_row["key_skills"] or [], app_row["job_description"] or "")

    existing = query_one(
        "SELECT id FROM nexai_session WHERE application_id = %s",
        [invite["application_id"]],
    )
    if existing:
        query(
            """UPDATE nexai_session
               SET questions = %s::jsonb, status = 'in_progress',
                   started_at = now(), transcript = NULL,
                   raw_score = NULL, score_detail = NULL
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


@router.post("/invite/submit/{session_id}")
def submit_invited_session(session_id: str, body: SubmitSessionIn):
    """Public — candidate submits completed interview transcript."""
    sess = query_one(
        "SELECT id, application_id, questions FROM nexai_session WHERE id = %s",
        [session_id],
    )
    if not sess:
        raise HTTPException(404, "Session not found")

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
def render_question(body: RenderQuestionIn, _user: dict = Depends(get_current_user)):
    """
    STEP A3 — Generate TTS audio for a question and render a lip-sync video
    using the configured avatar provider (sadtalker / wav2lip / vendor).

    For 'orb' provider: returns {video_url: null} immediately (frontend uses orb).
    For GPU providers: generates audio via gTTS, sends to GPU service, returns video_url.
    Falls back to orb cleanly if GPU service is unreachable.
    """
    provider = _avatar_svc.PROVIDER
    if provider == "orb":
        return {"video_url": None, "provider": "orb", "fallback": False}

    # Generate TTS audio file for GPU rendering
    try:
        from gtts import gTTS
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            audio_path = tf.name
        gTTS(text=body.question_text, lang="en", tld="co.in").save(audio_path)
    except ImportError:
        return {"video_url": None, "provider": "orb", "fallback": True,
                "reason": "gTTS not installed — pip install gtts"}
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
