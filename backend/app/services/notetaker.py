"""
Meeting notetaker -- records interviews, transcribes, generates notes, and
shares them with the recruiter and panel. Works for human rounds AND the AI
bot round.

DESIGN: one interface, multiple swappable providers. Egnex calls
`process_interview(...)`; underneath sits either the NATIVE pipeline or a
THIRD-PARTY service (Fireflies / Read.ai). If the preferred provider fails,
it falls back to the other automatically -- so you always have a backup.

LEGAL GATE: nothing records until consent is 'granted'. This is enforced in
`start_recording`, not left to the caller to remember.

All real external calls are stubbed for the prototype and clearly marked.
Recordings and transcripts are stored on company GCP (the url fields hold
GCP Cloud Storage object paths).
"""
import uuid
from ..db import query, query_one


# ---------------- CONSENT ----------------
def request_consent(interview_id, candidate_id, consent_text, region="IN"):
    """Create a pending consent record and (in production) show/send the
    candidate the recording notice. No recording may start until granted."""
    query(
        """INSERT INTO recording_consent
             (interview_id, candidate_id, consent_text, region, consent_state)
           VALUES (%s, %s, %s, %s, 'pending')
           ON CONFLICT (interview_id) DO UPDATE
             SET consent_text = EXCLUDED.consent_text, region = EXCLUDED.region""",
        [interview_id, candidate_id, consent_text, region], fetch=False,
    )
    return {"interview_id": interview_id, "consent_state": "pending"}


def record_consent_response(interview_id, granted: bool):
    state = "granted" if granted else "declined"
    query(
        """UPDATE recording_consent
             SET consent_state = %s, responded_at = now()
           WHERE interview_id = %s""",
        [state, interview_id], fetch=False,
    )
    return {"interview_id": interview_id, "consent_state": state}


def _consent_ok(interview_id) -> bool:
    row = query_one(
        "SELECT consent_state FROM recording_consent WHERE interview_id = %s",
        [interview_id],
    )
    return bool(row and row["consent_state"] == "granted")


# ---------------- PROVIDERS (stubbed) ----------------
def _native_record(interview_id) -> dict:
    """PRODUCTION: join the Google Meet as a bot participant, capture the
    stream, push video to GCP Cloud Storage, return the object path.
    PROTOTYPE: returns a fake GCP path."""
    return {
        "provider": "native",
        "video_url": f"gs://egnex-recordings/{interview_id}/native.mp4",
        "duration_sec": 1800,
    }


def _thirdparty_record(interview_id, provider="fireflies") -> dict:
    """PRODUCTION: call the Fireflies/Read.ai API to attach their bot to the
    meeting and later fetch the recording + transcript.
    PROTOTYPE: returns a fake path."""
    return {
        "provider": provider,
        "video_url": f"gs://egnex-recordings/{interview_id}/{provider}.mp4",
        "duration_sec": 1800,
    }


def _transcribe(video_url) -> dict:
    """PRODUCTION: send audio to a speech-to-text model (on GCP or managed).
    PROTOTYPE: returns placeholder transcript."""
    return {
        "full_text": "Stubbed transcript. Replace _transcribe in production.",
        "segments": [{"speaker": "panel", "ts": 0, "text": "Tell me about yourself."},
                     {"speaker": "candidate", "ts": 8, "text": "..."}],
    }


def _summarise(transcript_text, job_description="") -> dict:
    """PRODUCTION: send transcript to the notes model -> summary, key points,
    action items, and an ASSISTIVE suggested score (panel decides).
    PROTOTYPE: returns placeholder notes."""
    return {
        "summary": "Stubbed summary of the interview. Replace _summarise in production.",
        "key_points": ["Strong communication", "Relevant project experience"],
        "action_items": ["Schedule next round"],
        "suggested_score": 75.0,
    }


# ---------------- ORCHESTRATION ----------------
def start_recording(interview_id, preferred="native"):
    """
    Consent-gated entry point. Refuses to record without granted consent.
    Tries the preferred provider; on failure, falls back to the other.
    """
    if not _consent_ok(interview_id):
        return {"status": "blocked", "reason": "consent not granted"}

    order = ["native", "fireflies"] if preferred == "native" else ["fireflies", "native"]
    last_error = None
    for prov in order:
        try:
            rec = _native_record(interview_id) if prov == "native" \
                else _thirdparty_record(interview_id, prov)
            row = query_one(
                """INSERT INTO meeting_recording
                     (interview_id, provider, video_url, duration_sec, status)
                   VALUES (%s, %s, %s, %s, 'ready')
                   ON CONFLICT (interview_id, provider) DO UPDATE
                     SET video_url = EXCLUDED.video_url, status = 'ready'
                   RETURNING id, provider""",
                [interview_id, rec["provider"], rec["video_url"], rec["duration_sec"]],
            )
            return {"status": "ready", "recording_id": row["id"], "provider": row["provider"]}
        except Exception as e:                       # fall back to the next provider
            last_error = str(e)
            continue
    return {"status": "failed", "reason": last_error}


def process_interview(interview_id, job_description="", share_with=None):
    """
    Full pipeline for one interview: record (consent-gated) -> transcribe ->
    summarise -> store -> share. Returns the notes. Used for human rounds and
    the AI bot round alike.
    """
    rec = start_recording(interview_id)
    if rec.get("status") != "ready":
        return rec                                   # blocked or failed

    recording_id = rec["recording_id"]

    t = _transcribe(None)
    query(
        """INSERT INTO meeting_transcript (recording_id, full_text, segments)
           VALUES (%s, %s, %s::jsonb)""",
        [recording_id, t["full_text"], __import__("json").dumps(t["segments"])],
        fetch=False,
    )

    n = _summarise(t["full_text"], job_description)
    shared = share_with or []
    notes = query_one(
        """INSERT INTO meeting_notes
             (recording_id, summary, key_points, action_items, suggested_score, shared_with)
           VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb)
           RETURNING id, summary, suggested_score""",
        [recording_id, n["summary"],
         __import__("json").dumps(n["key_points"]),
         __import__("json").dumps(n["action_items"]),
         n["suggested_score"],
         __import__("json").dumps(shared)],
    )

    # PRODUCTION: email the notes + recording link to recruiter & panel here.
    return {
        "status": "shared",
        "provider": rec["provider"],
        "recording_id": recording_id,
        "notes_id": notes["id"],
        "summary": notes["summary"],
        "suggested_score": float(notes["suggested_score"]) if notes["suggested_score"] else None,
        "shared_with": shared,
    }
