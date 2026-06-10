"""
Transcript API — Meeting Notetaker (Feature 7).

Endpoints:
  POST /api/interviews/{interview_id}/fetch-transcript
    Trigger Drive search + Groq summarization (background task).
    Returns immediately with {"ok": true, "status": "fetching"}.
    Call GET /transcript to poll progress.

  GET  /api/interviews/{interview_id}/transcript
    Return current transcript notes + status for an interview.
    Role-scoped: recruiter → own reqs only; ta_manager/admin → all.

Role visibility:
  recruiter   — only interviews on their requisitions
  ta_manager  — all interviews
  admin       — all interviews
  (hiring_manager and interviewer cannot trigger fetch but CAN view
   completed notes if they are on the panel)
"""
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from ..db import query, query_one
from ..auth_utils import get_current_user
from ..services.transcript_service import (
    has_drive_scope,
    process_transcript,
    DriveScopeMissingError,
)

router = APIRouter(prefix="/api", tags=["transcript"])


# ── Visibility helper ─────────────────────────────────────────────────────────

def _assert_can_access_interview(interview_id: str, user: dict) -> dict:
    """
    Return the interview row if the caller is allowed to see it.
    Raises 403 or 404 otherwise.
    """
    role = user["role"]
    uid  = user["sub"]

    iv = query_one(
        """SELECT i.id, i.application_id, i.gcal_event_id, i.scheduled_at,
                  a.requisition_id,
                  c.full_name AS candidate_name,
                  r.title     AS job_title
           FROM interview  i
           JOIN application a ON a.id = i.application_id
           JOIN candidate   c ON c.id = a.candidate_id
           JOIN requisition r ON r.id = a.requisition_id
           WHERE i.id = %s""",
        [interview_id],
    )
    if not iv:
        raise HTTPException(404, "Interview not found")

    if role in ("admin", "ta_manager"):
        return iv

    if role == "recruiter":
        ok = query_one(
            "SELECT 1 FROM requisition_recruiter WHERE requisition_id=%s AND recruiter_id=%s",
            [str(iv["requisition_id"]), uid],
        )
        if ok:
            return iv

    if role == "hiring_manager":
        ok = query_one(
            "SELECT 1 FROM requisition WHERE id=%s AND hiring_manager_id=%s",
            [str(iv["requisition_id"]), uid],
        )
        if ok:
            return iv

    # Panel member can view completed notes
    ok = query_one(
        "SELECT 1 FROM interview_panel WHERE interview_id=%s AND interviewer_id=%s",
        [interview_id, uid],
    )
    if ok:
        return iv

    raise HTTPException(403, "Not authorised to access this interview's transcript")


# ── POST: trigger fetch ───────────────────────────────────────────────────────

@router.post("/interviews/{interview_id}/fetch-transcript", status_code=202)
def trigger_fetch_transcript(
    interview_id: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """
    Kick off background Drive search + Groq summarization for this interview.

    Returns 202 immediately.  Poll GET /api/interviews/{id}/transcript for progress.

    Error responses:
      400  — Drive scope not granted (drive_scope_missing=true in body)
      403  — caller cannot access this interview
      404  — interview not found
      409  — already in progress or done (re-fetch allowed if status is failed)
    """
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Only recruiters, TA managers, and admins can fetch transcripts")

    iv = _assert_can_access_interview(interview_id, user)

    # Drive scope check — must be the user whose credentials will be used
    recruiter_id = user["sub"]
    if not has_drive_scope(recruiter_id):
        raise HTTPException(
            400,
            detail={
                "message": (
                    "Google Drive access not granted. "
                    "Reconnect your Google account to include Drive permission, "
                    "then try again."
                ),
                "drive_scope_missing": True,
            },
        )

    # Idempotency: block re-trigger if already fetching/summarizing
    existing = query_one(
        "SELECT fetch_status FROM interview_notes WHERE interview_id = %s",
        [interview_id],
    )
    if existing and existing["fetch_status"] in ("fetching", "summarizing"):
        return {
            "ok":    True,
            "status": existing["fetch_status"],
            "message": "Already in progress — poll GET /transcript for updates.",
        }

    # Start background processing
    background_tasks.add_task(process_transcript, interview_id, recruiter_id)

    return {"ok": True, "status": "fetching", "message": "Transcript fetch started."}


# ── GET: status + content ─────────────────────────────────────────────────────

@router.get("/interviews/{interview_id}/transcript")
def get_transcript(
    interview_id: str,
    user: dict = Depends(get_current_user),
):
    """
    Return transcript notes for an interview.

    fetch_status values:
      none          — not yet requested
      fetching      — Drive search in progress
      fetch_failed  — no file found or read error (fetch_error has details)
      summarizing   — transcript found + saved, Groq running
      done          — transcript + summary both available
      summary_failed — transcript saved but Groq failed (fetch_error has reason)
    """
    _assert_can_access_interview(interview_id, user)

    notes = query_one(
        """SELECT id, interview_id, drive_file_id, drive_file_name,
                  transcript_text, summary, fetch_status, fetch_error,
                  created_at, updated_at
           FROM interview_notes
           WHERE interview_id = %s""",
        [interview_id],
    )

    if not notes:
        return {"fetch_status": "none", "transcript": None, "summary": None}

    # Parse summary JSON if stored as string
    summary = notes.get("summary")
    if isinstance(summary, str):
        try:
            import json
            summary = json.loads(summary)
        except Exception:
            summary = None

    return {
        "fetch_status":    notes["fetch_status"],
        "fetch_error":     notes.get("fetch_error"),
        "drive_file_id":   notes.get("drive_file_id"),
        "drive_file_name": notes.get("drive_file_name"),
        "transcript_text": notes.get("transcript_text"),
        "summary":         summary,
        "updated_at":      notes.get("updated_at"),
    }
