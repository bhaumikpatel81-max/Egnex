"""
PART B — Proctoring endpoints (consent-gated).

HARD LEGAL GATE: No real external candidate may be recorded until
legal sign-off is obtained. Testing with internal volunteers only.
All proctoring data must stay on company GCP. AI flags are assistive
only — reviewed by a human recruiter, never auto-reject.

Buildable in this router (B1-B7):
  B1  Consent + recording badge
  B2  Identity snapshot (webcam still stored per session)
  B3  Webcam video chunks
  B4  Screen recording chunks (flagged if declined)
  B5  Audio monitoring (part of webcam stream)
  B6  AI behaviour flags submitted from browser TF.js analysis
  B7  Flag review tool for human recruiter

Out of scope — specialist vendor or native app needed (NOT attempted here):
  - Lockdown browser (tab/copy/paste/print blocking) → needs installed native app
  - Secondary-device / hidden-phone detection via audio/Wi-Fi → needs native + hardware
  - Virtual machine blocking → needs native system access
  - Government-ID biometric face matching → specialist paid identity service
  - Keystroke-dynamics identity → low reliability; skip
"""
import io
import json
import os
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import get_current_user

router = APIRouter(prefix="/api/proctoring", tags=["proctoring"])

_UPLOADS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "proctoring_uploads")
)
os.makedirs(_UPLOADS_DIR, exist_ok=True)


# ── B1: Create session + record consent ───────────────────────────────────────

class CreateSessionIn(BaseModel):
    application_id: str
    nexai_session_id: Optional[str] = None


@router.post("/sessions", status_code=201)
def create_session(body: CreateSessionIn, _user: dict = Depends(get_current_user)):
    existing = query_one(
        "SELECT id FROM proctoring_session WHERE application_id = %s",
        [body.application_id],
    )
    if existing:
        # Update nexai_session_id if supplied (called after NexAI session is created)
        if body.nexai_session_id:
            query(
                "UPDATE proctoring_session SET nexai_session_id=%s WHERE id=%s",
                [body.nexai_session_id, existing["id"]],
                fetch=False,
            )
        return query_one("SELECT id, consent_granted, created_at FROM proctoring_session WHERE id=%s", [existing["id"]])
    row = query_one(
        """INSERT INTO proctoring_session (application_id, nexai_session_id)
           VALUES (%s, %s) RETURNING id, consent_granted, created_at""",
        [body.application_id, body.nexai_session_id],
    )
    return row


class ConsentIn(BaseModel):
    granted: bool
    consent_text: str = (
        "This NexAI interview session will be video recorded (webcam), screen recorded, "
        "and audio recorded. A photo will be taken at the start for identity purposes. "
        "AI behaviour analysis will run on the recording. All data is stored on Amnex GCP. "
        "AI flags are reviewed by a human recruiter and are never used to auto-reject."
    )
    retention_days: int = 90


@router.post("/sessions/{session_id}/consent")
def record_consent(
    session_id: str, body: ConsentIn, _user: dict = Depends(get_current_user)
):
    retention_until = datetime.utcnow() + timedelta(days=body.retention_days)
    row = query_one(
        """UPDATE proctoring_session
           SET consent_granted = %s,
               proctoring_declined = %s,
               consent_text = %s,
               consented_at = now(),
               retention_until = %s
           WHERE id = %s
           RETURNING id, consent_granted, proctoring_declined""",
        [body.granted, not body.granted, body.consent_text, retention_until, session_id],
    )
    if not row:
        raise HTTPException(404, "proctoring session not found")
    return row


# ── B2: Identity snapshot ─────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/identity")
async def save_identity_snapshot(
    session_id: str,
    snapshot: UploadFile = File(...),
    _user: dict = Depends(get_current_user),
):
    """
    Store a webcam still captured at interview start.
    NOTE: Automated biometric matching against a government ID is a specialist
    paid service. The identity_match_status column is scaffolded but
    matching is NOT implemented here. A human reviews the photo.
    """
    _assert_consented(session_id)
    ext = os.path.splitext(snapshot.filename or "")[1] or ".jpg"
    fname = f"{session_id}_identity{ext}"
    path = os.path.join(_UPLOADS_DIR, fname)
    with open(path, "wb") as f:
        f.write(await snapshot.read())
    # TODO: replace path with GCS upload when credentials are available
    query_one(
        "UPDATE proctoring_session SET identity_snapshot_path = %s WHERE id = %s RETURNING id",
        [path, session_id],
    )
    return {"saved": True, "path": fname}


# ── B3/B4/B5: Media chunk upload ─────────────────────────────────────────────

@router.post("/sessions/{session_id}/media-chunk")
async def upload_media_chunk(
    session_id: str,
    media_type: str = Form(...),   # "webcam" | "screen"
    chunk_index: int = Form(...),
    chunk: UploadFile = File(...),
    _user: dict = Depends(get_current_user),
):
    """
    Receive a video chunk (webcam or screen). Audio is included in the webcam stream.
    Chunks are appended to a session folder.
    TODO: swap the local write for a GCS resumable upload.
    """
    _assert_consented(session_id)
    if media_type not in ("webcam", "screen"):
        raise HTTPException(400, "media_type must be 'webcam' or 'screen'")
    folder = os.path.join(_UPLOADS_DIR, session_id, media_type)
    os.makedirs(folder, exist_ok=True)
    ext = os.path.splitext(chunk.filename or "")[1] or ".webm"
    fname = f"chunk_{chunk_index:05d}{ext}"
    with open(os.path.join(folder, fname), "wb") as f:
        f.write(await chunk.read())
    # Update path column on first chunk
    col = "webcam_video_path" if media_type == "webcam" else "screen_video_path"
    query_one(
        f"UPDATE proctoring_session SET {col} = %s WHERE id = %s RETURNING id",
        [os.path.join(_UPLOADS_DIR, session_id, media_type), session_id],
    )
    return {"saved": True, "chunk": chunk_index}


@router.post("/sessions/{session_id}/screen-declined")
def screen_declined(session_id: str, _user: dict = Depends(get_current_user)):
    """Flag that the candidate declined screen recording (session continues, just noted)."""
    row = query_one(
        "UPDATE proctoring_session SET screen_recording_declined = true WHERE id = %s RETURNING id",
        [session_id],
    )
    if not row:
        raise HTTPException(404, "session not found")
    return {"noted": True}


# ── B6: AI behaviour flags (from browser TF.js analysis) ─────────────────────

class FlagsIn(BaseModel):
    flags: list  # [{ts_ms: int, type: str, confidence: float, detail: str}]


@router.post("/sessions/{session_id}/flags")
def submit_flags(session_id: str, body: FlagsIn, _user: dict = Depends(get_current_user)):
    """
    Accept AI behaviour flags generated by TF.js (BlazeFace + COCO-SSD) in the browser.
    Flag types: multi_face | no_face | face_away | phone_detected | unknown_object
    These are assistive only — surfaced to a human reviewer, never auto-reject.
    """
    existing = query_one("SELECT flags FROM proctoring_session WHERE id = %s", [session_id])
    if not existing:
        raise HTTPException(404, "session not found")
    current = existing["flags"] if isinstance(existing["flags"], list) else []
    merged = current + body.flags
    query_one(
        "UPDATE proctoring_session SET flags = %s::jsonb, flag_count = %s WHERE id = %s RETURNING id",
        [json.dumps(merged), len(merged), session_id],
    )
    return {"flag_count": len(merged)}


@router.post("/sessions/{session_id}/complete")
def complete_session(session_id: str, _user: dict = Depends(get_current_user)):
    row = query_one(
        """UPDATE proctoring_session
           SET proctoring_complete = TRUE
           WHERE id = %s RETURNING id, flag_count""",
        [session_id],
    )
    if not row:
        raise HTTPException(404, "session not found")
    return row


# ── B7: Human flag review tool ────────────────────────────────────────────────

@router.get("/review")
def list_for_review(user: dict = Depends(get_current_user)):
    """List proctored sessions requiring human review."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    return query(
        """
        SELECT ps.id, ps.application_id, ps.consent_granted, ps.flag_count,
               ps.screen_recording_declined, ps.human_decision,
               ps.created_at, ps.reviewed_at,
               c.full_name AS candidate_name, r.title AS req_title
        FROM proctoring_session ps
        JOIN application a ON a.id = ps.application_id
        JOIN candidate  c ON c.id = a.candidate_id
        JOIN requisition r ON r.id = a.requisition_id
        ORDER BY ps.created_at DESC
        LIMIT 200
        """,
        [],
    )


@router.get("/review/{session_id}")
def get_review(session_id: str, user: dict = Depends(get_current_user)):
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    row = query_one(
        """
        SELECT ps.*, c.full_name AS candidate_name, r.title AS req_title
        FROM proctoring_session ps
        JOIN application a ON a.id = ps.application_id
        JOIN candidate  c ON c.id = a.candidate_id
        JOIN requisition r ON r.id = a.requisition_id
        WHERE ps.id = %s
        """,
        [session_id],
    )
    if not row:
        raise HTTPException(404, "session not found")
    return row


class ReviewIn(BaseModel):
    reviewer_notes: Optional[str] = None
    human_decision: Optional[str] = None


@router.patch("/review/{session_id}")
def update_review(
    session_id: str, body: ReviewIn, user: dict = Depends(get_current_user)
):
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    sets, params = [], []
    if body.reviewer_notes is not None:
        sets.append("reviewer_notes = %s"); params.append(body.reviewer_notes)
    if body.human_decision is not None:
        allowed = {"cleared", "flagged_minor", "flagged_major", "voided"}
        if body.human_decision not in allowed:
            raise HTTPException(400, f"human_decision must be one of {sorted(allowed)}")
        sets.append("human_decision = %s"); params.append(body.human_decision)
        sets.append("reviewed_by = %s");    params.append(user["sub"])
        sets.append("reviewed_at = now()")
    if not sets:
        raise HTTPException(400, "Nothing to update")
    params.append(session_id)
    row = query_one(
        f"UPDATE proctoring_session SET {', '.join(sets)} WHERE id = %s RETURNING id, human_decision",
        params,
    )
    if not row:
        raise HTTPException(404, "session not found")
    return row


@router.get("/review/{session_id}/summary")
def download_summary(session_id: str, user: dict = Depends(get_current_user)):
    """Download a CSV incident summary for the session (B7)."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")
    row = query_one(
        """SELECT ps.*, c.full_name, r.title
           FROM proctoring_session ps
           JOIN application a ON a.id = ps.application_id
           JOIN candidate c ON c.id = a.candidate_id
           JOIN requisition r ON r.id = a.requisition_id
           WHERE ps.id = %s""",
        [session_id],
    )
    if not row:
        raise HTTPException(404, "session not found")
    flags = row["flags"] if isinstance(row["flags"], list) else []
    lines = [
        "Egnex NexAI Proctoring — Incident Summary",
        f"Candidate: {row['full_name']}",
        f"Requisition: {row['title']}",
        f"Session ID: {row['id']}",
        f"Date: {row['created_at']}",
        f"Consent granted: {row['consent_granted']}",
        f"Screen recording declined: {row['screen_recording_declined']}",
        f"Total AI flags: {row['flag_count']}",
        f"Human decision: {row['human_decision'] or 'pending'}",
        f"Reviewer notes: {row['reviewer_notes'] or '—'}",
        "",
        "AI FLAG TIMELINE",
        "ts_ms,type,confidence,detail",
    ]
    for fl in flags:
        lines.append(f"{fl.get('ts_ms','')},{fl.get('type','')},{fl.get('confidence','')},{fl.get('detail','')}")
    lines += [
        "",
        "OUT OF SCOPE (requires specialist vendor / native app):",
        "  - Lockdown browser: needs installed native application",
        "  - Secondary-device detection: needs native + hardware signals",
        "  - VM blocking: needs native system access",
        "  - Government-ID face matching: specialist paid identity service",
        "  - Keystroke-dynamics identity: low reliability, skipped",
        "",
        "LEGAL NOTE: This report contains regulated personal/biometric data.",
        "Handle and retain per Amnex data protection policy.",
    ]
    content = "\n".join(lines)
    return StreamingResponse(
        io.BytesIO(content.encode()),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=proctor_summary_{session_id[:8]}.csv"},
    )


# ── Helper ────────────────────────────────────────────────────────────────────

def _assert_consented(session_id: str):
    row = query_one(
        "SELECT consent_granted FROM proctoring_session WHERE id = %s", [session_id]
    )
    if not row:
        raise HTTPException(404, "proctoring session not found")
    if not row["consent_granted"]:
        raise HTTPException(403, "Consent not granted — cannot store proctoring data")


# ── Candidate-facing endpoints (token-auth, no JWT required) ─────────────────
# These mirror the recruiter JWT endpoints above but authenticate via the
# candidate's invite token. Existing recruiter endpoints are unchanged.

def _get_invite_for_token(token: str) -> dict:
    invite = query_one(
        "SELECT id, application_id, expires_at FROM nexai_invite WHERE token = %s",
        [token],
    )
    if not invite:
        raise HTTPException(400, "Invalid interview token")
    exp = invite["expires_at"]
    if exp and exp.replace(tzinfo=None) < datetime.utcnow():
        raise HTTPException(400, "Interview link has expired")
    return invite


def _candidate_owns_session(token: str, session_id: str) -> dict:
    """Verify token owner's application matches the proctoring session. Returns session row."""
    invite = _get_invite_for_token(token)
    row = query_one(
        "SELECT id, application_id, consent_granted FROM proctoring_session WHERE id = %s",
        [session_id],
    )
    if not row or str(row["application_id"]) != str(invite["application_id"]):
        raise HTTPException(403, "Not authorised")
    return row


@router.post("/candidate/init")
def candidate_init_session(token: str):
    """Public — create or retrieve the proctoring session for this invite token."""
    invite = _get_invite_for_token(token)
    existing = query_one(
        "SELECT id, consent_granted FROM proctoring_session WHERE application_id = %s",
        [str(invite["application_id"])],
    )
    if existing:
        return {
            "proctoring_session_id": str(existing["id"]),
            "consent_granted": bool(existing["consent_granted"]),
        }
    row = query_one(
        "INSERT INTO proctoring_session (application_id) VALUES (%s) RETURNING id, consent_granted",
        [str(invite["application_id"])],
    )
    return {"proctoring_session_id": str(row["id"]), "consent_granted": False}


@router.post("/candidate/{session_id}/consent")
def candidate_record_consent(session_id: str, body: ConsentIn, token: str):
    """Public — record proctoring consent from the candidate page."""
    _candidate_owns_session(token, session_id)
    retention_until = datetime.utcnow() + timedelta(days=body.retention_days)
    row = query_one(
        """UPDATE proctoring_session
           SET consent_granted = %s,
               proctoring_declined = %s,
               consent_text = %s,
               consented_at = now(),
               retention_until = %s
           WHERE id = %s
           RETURNING id, consent_granted""",
        [body.granted, not body.granted, body.consent_text, retention_until, session_id],
    )
    if not row:
        raise HTTPException(404, "Session not found")
    return row


@router.post("/candidate/{session_id}/identity")
async def candidate_identity_snapshot(
    session_id: str,
    snapshot: UploadFile = File(...),
    token: str = Query(...),
):
    """Public — upload identity snapshot from the candidate page."""
    row = _candidate_owns_session(token, session_id)
    if not row["consent_granted"]:
        raise HTTPException(403, "Consent not granted")
    ext = os.path.splitext(snapshot.filename or "")[1] or ".jpg"
    path = os.path.join(_UPLOADS_DIR, f"{session_id}_identity{ext}")
    with open(path, "wb") as f:
        f.write(await snapshot.read())
    query_one(
        "UPDATE proctoring_session SET identity_snapshot_path = %s WHERE id = %s RETURNING id",
        [path, session_id],
    )
    return {"saved": True}


@router.post("/candidate/{session_id}/media-chunk")
async def candidate_media_chunk(
    session_id: str,
    media_type: str = Form(...),
    chunk_index: int = Form(...),
    chunk: UploadFile = File(...),
    token: str = Query(...),
):
    """Public — receive a webcam or screen chunk from the candidate page."""
    row = _candidate_owns_session(token, session_id)
    if not row["consent_granted"]:
        raise HTTPException(403, "Consent not granted")
    if media_type not in ("webcam", "screen"):
        raise HTTPException(400, "media_type must be 'webcam' or 'screen'")
    folder = os.path.join(_UPLOADS_DIR, session_id, media_type)
    os.makedirs(folder, exist_ok=True)
    ext = os.path.splitext(chunk.filename or "")[1] or ".webm"
    fname = f"chunk_{chunk_index:05d}{ext}"
    with open(os.path.join(folder, fname), "wb") as f:
        f.write(await chunk.read())
    col = "webcam_video_path" if media_type == "webcam" else "screen_video_path"
    query_one(
        f"UPDATE proctoring_session SET {col} = %s WHERE id = %s RETURNING id",
        [os.path.join(_UPLOADS_DIR, session_id, media_type), session_id],
    )
    return {"saved": True, "chunk": chunk_index}


@router.post("/candidate/{session_id}/flags")
def candidate_submit_flags(session_id: str, body: FlagsIn, token: str):
    """Public — submit AI behaviour flags from the candidate page."""
    row = _candidate_owns_session(token, session_id)
    if not row["consent_granted"]:
        raise HTTPException(403, "Consent not granted")
    existing = query_one("SELECT flags FROM proctoring_session WHERE id = %s", [session_id])
    current = existing["flags"] if isinstance(existing["flags"], list) else []
    merged = current + body.flags
    query_one(
        "UPDATE proctoring_session SET flags = %s::jsonb, flag_count = %s WHERE id = %s RETURNING id",
        [json.dumps(merged), len(merged), session_id],
    )
    return {"flag_count": len(merged)}


class _LinkSessionIn(BaseModel):
    nexai_session_id: str


@router.post("/candidate/{session_id}/link")
def candidate_link_session(session_id: str, body: _LinkSessionIn, token: str):
    """Public — link proctoring session to nexai_session_id after /invite/begin."""
    _candidate_owns_session(token, session_id)
    query(
        "UPDATE proctoring_session SET nexai_session_id = %s WHERE id = %s",
        [body.nexai_session_id, session_id],
        fetch=False,
    )
    return {"linked": True}


@router.post("/candidate/{session_id}/complete")
def candidate_complete_session(session_id: str, token: str):
    """Public — mark proctoring session complete when the interview ends."""
    _candidate_owns_session(token, session_id)
    row = query_one(
        """UPDATE proctoring_session
           SET proctoring_complete = TRUE
           WHERE id = %s RETURNING id, flag_count""",
        [session_id],
    )
    if not row:
        raise HTTPException(404, "Session not found")
    return row
