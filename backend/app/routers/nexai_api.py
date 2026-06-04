"""
NexAI — voice-first interview bot (14a).

Question generation is rule-based (JD + key skills).
Scoring is keyword + depth + communication weighted model.
The face/avatar (14b) is intentionally NOT built here.
"""
import io
import json
import os
import tempfile
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import get_current_user
from ..services import avatar as _avatar_svc

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
