"""
Meeting transcript service — reads Google Meet transcripts from Drive, summarizes
with Groq, stores in interview_notes, and emails the recruiter.

Flow (triggered by recruiter clicking "Fetch & Summarize"):
  1. Get recruiter's Google credentials (refresh if needed)
  2. [optional] Fetch meeting title from Calendar API using gcal_event_id
  3. Search Drive for a Google Doc transcript created ±4 hours of the meeting start
  4. If not found: mark fetch_failed + clear error message
  5. Read the Doc's text content via Drive export (plain text)
  6. Save raw transcript to interview_notes
  7. Summarize with Groq (sync openai client — runs in background thread)
  8. Save summary JSON + set fetch_status='done'
  9. Fire email to recruiter via Feature-6 email system (render_template + send_email)

All work happens inside process_transcript() which is called from a FastAPI
BackgroundTask thread.  Failures at each step are caught, logged, and recorded
in interview_notes.fetch_error so they surface clearly in the UI.

Drive scope required: https://www.googleapis.com/auth/drive.readonly
If that scope is absent from the recruiter's stored token this function raises
DriveScopeMissingError so the caller can prompt for reconnect.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import requests as _requests

from ..db import query, query_one
from .connectors import _load_email_cfg, send_email
from .email_templates import render_template

# ── Custom exception ──────────────────────────────────────────────────────────

class DriveScopeMissingError(Exception):
    """Recruiter has not granted Google Drive read access."""


# ── Scope check ───────────────────────────────────────────────────────────────

_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

def has_drive_scope(recruiter_id: str) -> bool:
    """
    Return True if the recruiter's stored Google token includes the Drive
    read-only scope.  Returns False if no token or scope missing.
    """
    row = query_one(
        "SELECT scope FROM recruiter_google_token WHERE user_id = %s",
        [recruiter_id],
    )
    if not row or not row.get("scope"):
        return False
    return _DRIVE_SCOPE in (row["scope"] or "").split()


# ── Google service builder (mirrors connectors._get_calendar_service) ─────────

def _build_google_service(recruiter_id: str, service_name: str, version: str):
    """
    Load recruiter's stored OAuth tokens, refresh if expired, and return
    a Google API service object.

    Raises DriveScopeMissingError if the stored scope does not include Drive.
    Raises ValueError if no token is stored.
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as _GReq
    from googleapiclient.discovery import build

    row = query_one(
        "SELECT * FROM recruiter_google_token WHERE user_id = %s",
        [recruiter_id],
    )
    if not row:
        raise ValueError(
            "No Google account connected for this recruiter. "
            "Connect Google Calendar & Drive from the Interviews screen first."
        )

    # Scope guard — Drive service requires drive.readonly
    if service_name == "drive" and not has_drive_scope(recruiter_id):
        raise DriveScopeMissingError(
            "Drive access not granted. Reconnect Google account to include Drive permission."
        )

    expiry = row["token_expiry"]
    if expiry is not None and getattr(expiry, "tzinfo", None) is not None:
        expiry = expiry.replace(tzinfo=None)

    creds = Credentials(
        token=row["access_token"],
        refresh_token=row["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=(row["scope"] or "").split() or None,
        expiry=expiry,
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(_GReq())
        query(
            """UPDATE recruiter_google_token
                 SET access_token = %s, token_expiry = %s, updated_at = now()
               WHERE user_id = %s""",
            [creds.token, creds.expiry, recruiter_id],
            fetch=False,
        )

    return build(service_name, version, credentials=creds, cache_discovery=False)


# ── Calendar helper: get event summary (title) ────────────────────────────────

def _get_calendar_event_title(recruiter_id: str, gcal_event_id: str) -> str | None:
    """
    Fetch the Google Calendar event title so we can do a better transcript search.
    Returns None on any failure (Drive search still continues, just less precise).
    """
    try:
        from googleapiclient.errors import HttpError
        cal = _build_google_service(recruiter_id, "calendar", "v3")
        event = cal.events().get(calendarId="primary", eventId=gcal_event_id).execute()
        return event.get("summary") or None
    except Exception as exc:
        print(f"[transcript] Could not fetch calendar event title: {exc}")
        return None


# ── Drive: find the transcript file ───────────────────────────────────────────

def find_meet_transcript(
    recruiter_id: str,
    scheduled_at: datetime,
    gcal_event_id: str | None = None,
    meeting_title: str | None = None,
) -> dict | None:
    """
    Search the recruiter's Google Drive for the Meet transcript Google Doc
    corresponding to this interview.

    Google Meet saves transcripts as Google Docs with "transcript" in the name,
    typically in a "Meet Recordings" folder.  They appear a few minutes after
    the meeting ends.

    Returns a dict {id, name, createdTime, webViewLink} or None.

    Search strategy:
      1. mimeType = Google Doc
      2. name contains 'transcript'
      3. createdTime >= (scheduled_at - 30 min)   [buffer for early calls]
      4. createdTime <= (scheduled_at + 4 hours)  [generous end window]
      5. trashed = false
      6. Optional: if meeting_title is known, score results by title similarity
         and return the best match.
    """
    drive = _build_google_service(recruiter_id, "drive", "v3")

    # ── Compute time window ────────────────────────────────────────────────────
    # Ensure UTC-aware for Drive query
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)

    time_from = (scheduled_at - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_to   = (scheduled_at + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")

    q = (
        "mimeType = 'application/vnd.google-apps.document' "
        f"and name contains 'transcript' "
        f"and createdTime >= '{time_from}' "
        f"and createdTime <= '{time_to}' "
        "and trashed = false"
    )

    from googleapiclient.errors import HttpError
    try:
        result = drive.files().list(
            q=q,
            orderBy="createdTime desc",
            pageSize=20,
            fields="files(id, name, createdTime, webViewLink)",
            spaces="drive",
        ).execute()
    except HttpError as exc:
        raise RuntimeError(f"Drive API error while searching for transcript: {exc}") from exc

    files = result.get("files", [])
    if not files:
        return None

    # ── Score by title match if we have meeting title ──────────────────────────
    if meeting_title:
        title_lower = meeting_title.lower()
        def _score(f):
            n = f["name"].lower()
            # Exact title match is best; partial word overlap also scores
            if title_lower in n:
                return 2
            words = [w for w in title_lower.split() if len(w) > 3]
            return sum(1 for w in words if w in n)
        files.sort(key=_score, reverse=True)

    return files[0]


# ── Drive: read the transcript Doc as plain text ──────────────────────────────

def read_drive_doc_text(recruiter_id: str, file_id: str) -> str:
    """
    Export a Google Doc as plain text and return the content.
    Raises RuntimeError on failure.
    """
    from googleapiclient.errors import HttpError
    drive = _build_google_service(recruiter_id, "drive", "v3")
    try:
        content = drive.files().export_media(
            fileId=file_id,
            mimeType="text/plain",
        ).execute()
    except HttpError as exc:
        raise RuntimeError(f"Drive API error reading transcript doc: {exc}") from exc

    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return str(content)


# ── Groq: summarize transcript ────────────────────────────────────────────────

_SUMMARY_PROMPT = """\
You are a professional recruiting coordinator summarizing a job interview transcript.
Produce a STRICT JSON object with exactly these four keys:
  discussion_points : list of 3-7 short strings covering the main topics discussed
  strengths         : 2-4 sentence paragraph on the candidate's strong points
  concerns          : 2-4 sentence paragraph on gaps or concerns (write "None noted." if absent)
  overall_note      : 1-2 sentence overall impression for the recruiter

Return ONLY the JSON. No prose, no markdown fences.

CANDIDATE: {candidate_name}
ROLE: {job_title}

TRANSCRIPT:
{transcript}
"""


def summarize_transcript(
    transcript_text: str,
    candidate_name: str,
    job_title: str,
) -> dict:
    """
    Call Groq (sync) to produce a structured interview summary.

    Returns {"discussion_points": [...], "strengths": "...",
             "concerns": "...", "overall_note": "..."}.

    Raises RuntimeError if GROQ_API_KEY is not set or the API call fails.
    """
    import openai

    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Add it to .env.prod to enable summarization."
        )

    client = openai.OpenAI(
        api_key=api_key,
        base_url=os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
    )

    # Truncate very long transcripts to stay within token limits
    truncated = transcript_text[:12000]
    if len(transcript_text) > 12000:
        truncated += "\n\n[Transcript truncated to fit token limit]"

    prompt = _SUMMARY_PROMPT.format(
        candidate_name=candidate_name,
        job_title=job_title,
        transcript=truncated,
    )

    response = client.chat.completions.create(
        model=os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile"),
        max_tokens=800,
        messages=[
            {"role": "system", "content": "You are a professional recruiting coordinator. Return only JSON."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
    )

    raw = (response.choices[0].message.content or "").strip()
    # Belt-and-suspenders: strip markdown fences if present
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

    parsed = json.loads(raw)

    # Normalise: ensure all four keys exist
    return {
        "discussion_points": parsed.get("discussion_points") or [],
        "strengths":         parsed.get("strengths")         or "—",
        "concerns":          parsed.get("concerns")          or "—",
        "overall_note":      parsed.get("overall_note")      or "—",
    }


# ── Email helper ──────────────────────────────────────────────────────────────

def _send_summary_email(
    recruiter_email: str,
    recruiter_name: str,
    candidate_name: str,
    job_title: str,
    interview_date: str,
    summary: dict,
) -> None:
    """Fire-and-forget summary email; logged but never crashes the pipeline."""
    try:
        disc = "\n• ".join(summary.get("discussion_points") or [])
        if disc:
            disc = "• " + disc
        subject, body = render_template("meeting_summary", {
            "candidate_name":    candidate_name,
            "job_title":         job_title,
            "recruiter_name":    recruiter_name,
            "interview_date":    interview_date,
            "discussion_points": disc or "—",
            "strengths":         summary.get("strengths")    or "—",
            "concerns":          summary.get("concerns")     or "—",
            "overall_note":      summary.get("overall_note") or "—",
        })
        send_email(recruiter_email, subject, body)
    except Exception as exc:
        print(f"[transcript] summary email failed: {exc}")


# ── Main pipeline (runs in background thread) ─────────────────────────────────

def _upsert_notes(interview_id: str, application_id: str, **fields) -> None:
    """
    Insert or update interview_notes row for this interview.
    `fields` maps column names → values.
    """
    # Build SET clause for UPDATE
    set_pairs = [(k, v) for k, v in fields.items()]
    if not set_pairs:
        return

    set_clause = ", ".join(f"{k} = %s" for k, _ in set_pairs)
    vals       = [v for _, v in set_pairs]

    # Try update first
    updated = query(
        f"UPDATE interview_notes SET {set_clause}, updated_at=now() WHERE interview_id = %s RETURNING id",
        vals + [interview_id],
    )
    if not updated:
        # First row for this interview
        col_names = ", ".join(k for k, _ in set_pairs)
        placeholders = ", ".join("%s" for _ in set_pairs)
        query(
            f"""INSERT INTO interview_notes (interview_id, application_id, {col_names})
                VALUES (%s, %s, {placeholders})
                ON CONFLICT (interview_id) DO UPDATE
                SET {set_clause}, updated_at = now()""",
            [interview_id, application_id] + vals + vals,
            fetch=False,
        )


def process_transcript(interview_id: str, recruiter_id: str) -> None:
    """
    Full transcript pipeline.  Designed to run in a FastAPI BackgroundTask thread.
    Never raises — all failures are recorded in interview_notes and logged.

    Steps:
      1. Load interview details
      2. [optional] Get meeting title from Calendar
      3. Search Drive for transcript file
      4. Read file content
      5. Summarize with Groq
      6. Save to interview_notes
      7. Email recruiter
    """
    # ── Step 1: load interview ─────────────────────────────────────────────────
    iv = query_one(
        """SELECT i.id, i.gcal_event_id, i.scheduled_at, i.application_id,
                  c.full_name AS candidate_name, c.email AS candidate_email,
                  r.title     AS job_title,
                  u.full_name AS recruiter_name, u.email AS recruiter_email
           FROM interview  i
           JOIN application a  ON a.id = i.application_id
           JOIN candidate   c  ON c.id = a.candidate_id
           JOIN requisition r  ON r.id = a.requisition_id
           JOIN requisition_recruiter rr ON rr.requisition_id = r.id AND rr.recruiter_id = %s
           JOIN app_user    u  ON u.id = rr.recruiter_id
           WHERE i.id = %s
           LIMIT 1""",
        [recruiter_id, interview_id],
    )

    if not iv:
        # Fallback: load without recruiter constraint (ta_manager/admin case)
        iv = query_one(
            """SELECT i.id, i.gcal_event_id, i.scheduled_at, i.application_id,
                      c.full_name AS candidate_name, c.email AS candidate_email,
                      r.title     AS job_title
               FROM interview  i
               JOIN application a  ON a.id = i.application_id
               JOIN candidate   c  ON c.id = a.candidate_id
               JOIN requisition r  ON r.id = a.requisition_id
               WHERE i.id = %s""",
            [interview_id],
        )
        rec_row = query_one(
            "SELECT full_name, email FROM app_user WHERE id = %s", [recruiter_id]
        )
        if iv and rec_row:
            iv = dict(iv)
            iv["recruiter_name"]  = rec_row["full_name"]
            iv["recruiter_email"] = rec_row["email"]

    if not iv:
        print(f"[transcript] interview {interview_id} not found — aborting")
        return

    app_id        = str(iv["application_id"])
    candidate     = iv["candidate_name"]    or "Candidate"
    job_title     = iv["job_title"]         or "the position"
    rec_name      = iv.get("recruiter_name")  or "Recruiter"
    rec_email     = iv.get("recruiter_email") or ""
    scheduled_at  = iv["scheduled_at"]
    gcal_event_id = iv.get("gcal_event_id")

    interview_date = (
        scheduled_at.strftime("%d %b %Y at %I:%M %p UTC")
        if scheduled_at else "—"
    )

    # ── Step 2: get meeting title for better matching ──────────────────────────
    meeting_title = None
    if gcal_event_id:
        meeting_title = _get_calendar_event_title(recruiter_id, gcal_event_id)

    # ── Step 3: search Drive for transcript ────────────────────────────────────
    _upsert_notes(interview_id, app_id, fetch_status="fetching")

    try:
        file_info = find_meet_transcript(
            recruiter_id=recruiter_id,
            scheduled_at=scheduled_at or datetime.now(tz=timezone.utc),
            gcal_event_id=gcal_event_id,
            meeting_title=meeting_title,
        )
    except DriveScopeMissingError as exc:
        _upsert_notes(interview_id, app_id,
                      fetch_status="fetch_failed",
                      fetch_error=str(exc))
        print(f"[transcript] {interview_id}: Drive scope missing")
        return
    except Exception as exc:
        _upsert_notes(interview_id, app_id,
                      fetch_status="fetch_failed",
                      fetch_error=f"Drive search error: {exc}")
        print(f"[transcript] {interview_id}: Drive search failed: {exc}")
        return

    if not file_info:
        _upsert_notes(interview_id, app_id,
                      fetch_status="fetch_failed",
                      fetch_error=(
                          "No Meet transcript found in Google Drive for this meeting. "
                          "Ensure that: (1) Google Meet transcripts are enabled in your "
                          "Google Workspace admin console, (2) transcript was turned on "
                          "during the meeting, (3) the meeting has ended. "
                          "Transcripts can take 5–10 minutes to appear in Drive after the call ends."
                      ))
        print(f"[transcript] {interview_id}: no transcript file found in Drive")
        return

    # ── Step 4: read transcript content ───────────────────────────────────────
    try:
        transcript_text = read_drive_doc_text(recruiter_id, file_info["id"])
    except Exception as exc:
        _upsert_notes(interview_id, app_id,
                      fetch_status="fetch_failed",
                      drive_file_id=file_info["id"],
                      drive_file_name=file_info.get("name"),
                      fetch_error=f"Could not read transcript doc: {exc}")
        print(f"[transcript] {interview_id}: failed to read doc: {exc}")
        return

    if not transcript_text or not transcript_text.strip():
        _upsert_notes(interview_id, app_id,
                      fetch_status="fetch_failed",
                      drive_file_id=file_info["id"],
                      drive_file_name=file_info.get("name"),
                      fetch_error=(
                          "Transcript document was found but appears to be empty. "
                          "The transcript may not have been fully saved yet — try again in a few minutes."
                      ))
        return

    # Save transcript and transition to summarizing
    _upsert_notes(interview_id, app_id,
                  fetch_status="summarizing",
                  drive_file_id=file_info["id"],
                  drive_file_name=file_info.get("name"),
                  transcript_text=transcript_text,
                  fetch_error=None)

    # ── Step 5: summarize with Groq ────────────────────────────────────────────
    try:
        summary = summarize_transcript(transcript_text, candidate, job_title)
        _upsert_notes(interview_id, app_id,
                      fetch_status="done",
                      summary=json.dumps(summary),
                      fetch_error=None)
    except Exception as exc:
        # Save transcript even if Groq fails — recruiter can still read raw text
        _upsert_notes(interview_id, app_id,
                      fetch_status="summary_failed",
                      fetch_error=f"Groq summarization failed: {exc}")
        print(f"[transcript] {interview_id}: Groq failed: {exc}")
        # Still send email but with a clean message — never expose raw exception to recruiter
        summary = {
            "discussion_points": [],
            "strengths":    "Automatic summary could not be generated. The full transcript is available in Egnex for your review.",
            "concerns":     "—",
            "overall_note": "Automatic summary could not be generated; please review the full transcript in Egnex.",
        }

    # ── Step 6: email recruiter ────────────────────────────────────────────────
    if rec_email:
        _send_summary_email(rec_email, rec_name, candidate, job_title, interview_date, summary)

    print(
        f"[transcript] {interview_id}: done — file='{file_info.get('name')}' "
        f"groq={'ok' if summary.get('discussion_points') else 'fallback'}"
    )
