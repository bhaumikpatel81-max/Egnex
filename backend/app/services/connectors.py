"""
External connectors.

Google Calendar + Meet (Phase 3): real implementation.
  schedule_meeting uses the acting recruiter's stored OAuth token to check
  free/busy, create a Calendar event with a Meet link, and refresh the token
  automatically when it expires. Raises ValueError if the recruiter has not
  linked their Google account — the caller surfaces that as a 400.

Gmail, AI interview bot, Darwinbox: clearly-marked stubs for later phases.
"""
import os
import uuid
from datetime import datetime, timedelta

from ..db import query, query_one


# ------------------------------------------------------------------ #
#  GOOGLE CALENDAR + MEET  (Phase 3 — real)                           #
# ------------------------------------------------------------------ #

def _get_calendar_service(recruiter_id: str):
    """
    Load the recruiter's stored OAuth tokens, refresh if expired, and
    return an authorised Google Calendar API service object.

    Returns (service, None) on success.
    Returns (None, error_message) if the recruiter has not linked their account.
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as _GRequest
    from googleapiclient.discovery import build

    row = query_one(
        "SELECT * FROM recruiter_google_token WHERE user_id = %s",
        [recruiter_id],
    )
    if not row:
        return None, (
            "This recruiter has not connected Google Calendar. "
            "Open the Google Calendar card on the dashboard and click "
            "'Connect Google Calendar' first."
        )

    expiry = row["token_expiry"]
    # psycopg2 returns TIMESTAMPTZ as tz-aware; google-auth Credentials.expired
    # compares against datetime.utcnow() which is naive, so strip the tzinfo.
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
        creds.refresh(_GRequest())
        query(
            """UPDATE recruiter_google_token
                 SET access_token = %s, token_expiry = %s, updated_at = now()
               WHERE user_id = %s""",
            [creds.token, creds.expiry, recruiter_id],
            fetch=False,
        )

    return build("calendar", "v3", credentials=creds, cache_discovery=False), None


def schedule_meeting(recruiter_id: str, candidate_email: str,
                     panel_emails: list, start_time: datetime,
                     duration_min: int = 45) -> dict:
    """
    Create a Google Calendar event on the recruiter's primary calendar
    with a Google Meet link. Invites all panel members and the candidate.

    Checks panel free/busy first (best-effort: only works for calendars the
    recruiter has permission to view; conflicts are returned, not blocking).

    Refreshes the token automatically if it has expired.

    Raises ValueError if the recruiter has not linked Google Calendar.
    """
    from googleapiclient.errors import HttpError

    service, err = _get_calendar_service(recruiter_id)
    if service is None:
        raise ValueError(err)

    end_time   = start_time + timedelta(minutes=duration_min)
    all_emails = list({candidate_email} | set(panel_emails))

    # Best-effort free/busy check
    conflicts = []
    if panel_emails:
        try:
            fb = service.freebusy().query(body={
                "timeMin": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "timeMax": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "items":   [{"id": e} for e in panel_emails],
            }).execute()
            for cal_id, cal_data in fb.get("calendars", {}).items():
                if cal_data.get("busy"):
                    conflicts.append(cal_id)
        except HttpError:
            pass  # can't see their calendar — proceed anyway

    event_body = {
        "summary":   f"Interview – {candidate_email}",
        "start":     {"dateTime": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"), "timeZone": "UTC"},
        "end":       {"dateTime": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),   "timeZone": "UTC"},
        "attendees": [{"email": e} for e in all_emails],
        "conferenceData": {
            "createRequest": {
                "requestId":             uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "reminders": {"useDefault": True},
    }

    try:
        event = service.events().insert(
            calendarId="primary",
            body=event_body,
            conferenceDataVersion=1,
            sendUpdates="all",
        ).execute()
    except HttpError as exc:
        raise ValueError(f"Google Calendar API error: {exc}") from exc

    entry_points = event.get("conferenceData", {}).get("entryPoints") or []
    meet_link = next(
        (ep["uri"] for ep in entry_points if ep.get("entryPointType") == "video"),
        event.get("hangoutLink", ""),
    )

    return {
        "gcal_event_id": event["id"],
        "meet_link":     meet_link,
        "scheduled_at":  start_time.isoformat(),
        "conflicts":     conflicts,
    }


# ------------------------------------------------------------------ #
#  GMAIL  (stub — Phase 4)                                             #
# ------------------------------------------------------------------ #

def send_email(to_email: str, subject: str, body: str) -> dict:
    """STUB: replace body with Gmail API call."""
    return {"sent": True, "to": to_email, "subject": subject}


# ------------------------------------------------------------------ #
#  AI INTERVIEW BOT  (stub — Phase 5)                                  #
# ------------------------------------------------------------------ #

def run_bot_interview(candidate_id: str, job_description: str) -> dict:
    """
    STUB: replace body with real AI bot call.
    Assistive only — bot scores and ranks, but a human makes every advance/reject.
    """
    seed  = sum(ord(c) for c in str(candidate_id))
    score = 50 + (seed % 50)
    return {
        "bot_score": float(score),
        "summary":   "Stubbed bot interview summary (replace in production).",
    }


# ------------------------------------------------------------------ #
#  DARWINBOX  (stub — Phase 6)                                         #
# ------------------------------------------------------------------ #

def push_offer_to_darwin(offer: dict) -> dict:
    """STUB: replace body with Darwinbox API call."""
    return {"darwin_pushed": True, "darwin_ref": f"DRW-{uuid.uuid4().hex[:8]}"}
