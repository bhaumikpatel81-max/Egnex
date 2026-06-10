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
#  EMAIL  (SMTP — reads SMTP_USER / SMTP_PASSWORD from env)            #
# ------------------------------------------------------------------ #

def _load_email_cfg() -> dict:
    """
    Load all email config from system_settings (DB first, env vars as fallback).
    Returns a dict with keys: sendgrid_api_key, user, password, host, port,
    from_name, base_url.
    """
    db: dict = {}
    try:
        from ..db import query as _q
        rows = _q("SELECT key, value FROM system_settings")
        db = {r["key"]: (r["value"] or "").strip() for r in (rows or [])}
    except Exception as exc:
        print(f"[email] WARNING: could not read system_settings: {exc}")

    def _g(key, env_key, default=""):
        return (db.get(key) or os.environ.get(env_key, default) or "").strip()

    raw_pass = _g("smtp_password", "SMTP_PASSWORD")
    return {
        "sendgrid_api_key": _g("sendgrid_api_key", "SENDGRID_API_KEY"),
        "user":      _g("smtp_user",      "SMTP_USER"),
        "password":  raw_pass.replace(" ", ""),   # strip spaces from App Passwords
        "host":      _g("smtp_host",      "SMTP_HOST",      "smtp.gmail.com"),
        "port":      int(_g("smtp_port",  "SMTP_PORT",      "587") or "587"),
        "from_name": _g("smtp_from_name", "SMTP_FROM_NAME", "Egnex Hiring"),
        "base_url":  _g("app_base_url",   "APP_BASE_URL",   "http://localhost:8000"),
    }

# keep old name as alias so existing callers don't break
_load_smtp_settings = _load_email_cfg


def _send_via_sendgrid(api_key: str, from_email: str, from_name: str,
                       to_email: str, subject: str, body: str, html: str = None):
    """Send transactional email via SendGrid HTTP API."""
    import requests as _req

    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from":     {"email": from_email, "name": from_name},
        "reply_to": {"email": from_email, "name": from_name},
        "subject":  subject,
        "content":  [{"type": "text/plain", "value": body}],
        # Mark as transactional — uses SendGrid's dedicated transactional IP pool
        # which has much better inbox placement than the shared marketing pool
        "categories": ["transactional", "interview_invite"],
        # Disable click/open tracking — tracked links look like phishing to spam filters
        "tracking_settings": {
            "click_tracking":    {"enable": False},
            "open_tracking":     {"enable": False},
            "subscription_tracking": {"enable": False},
        },
        "mail_settings": {
            "sandbox_mode": {"enable": False},
        },
    }
    if html:
        payload["content"].append({"type": "text/html", "value": html})

    resp = _req.post(
        "https://api.sendgrid.com/v3/mail/send",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15,
    )
    if resp.status_code not in (200, 202):
        raise RuntimeError(
            f"SendGrid returned {resp.status_code}: {resp.text[:300]}"
        )
    print(f"[email] SendGrid sent TO: {to_email} | status: {resp.status_code}")


def _send_smtp(cfg: dict, to_email: str, msg_obj) -> None:
    """Inner SMTP send — tries TLS (587) first, then SSL (465) as fallback."""
    import smtplib

    host = cfg["host"]
    port = cfg["port"]
    user = cfg["user"]
    pwd  = cfg["password"]
    tls_err_msg = ""

    # Primary: STARTTLS on port 587
    try:
        with smtplib.SMTP(host, port, timeout=8) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(user, pwd)
            s.sendmail(user, [to_email], msg_obj.as_string())
        return
    except smtplib.SMTPAuthenticationError:
        raise
    except Exception as exc:
        tls_err_msg = str(exc)
        print(f"[email] TLS port {port} failed ({exc}), trying SSL 465…")

    # Fallback: SSL on port 465
    try:
        with smtplib.SMTP_SSL(host, 465, timeout=8) as s:
            s.ehlo()
            s.login(user, pwd)
            s.sendmail(user, [to_email], msg_obj.as_string())
    except Exception as ssl_err:
        raise RuntimeError(
            f"TLS port {port}: {tls_err_msg} | SSL port 465: {ssl_err}"
        )


def send_email(to_email: str, subject: str, body: str, html: str = None) -> dict:
    """
    Send email.  Priority:
      1. SendGrid HTTP API  (set sendgrid_api_key in Settings — works on all networks)
      2. Gmail / SMTP       (set smtp_user + smtp_password in Settings)
      3. Stub               (logs to console only — neither is configured)
    """
    import smtplib

    cfg = _load_email_cfg()

    # ── 1. SendGrid ──────────────────────────────────────────────────────────
    if cfg["sendgrid_api_key"]:
        from_email = cfg["user"] or "noreply@egnex.io"
        _send_via_sendgrid(
            cfg["sendgrid_api_key"], from_email, cfg["from_name"],
            to_email, subject, body, html,
        )
        return {"sent": True, "to": to_email, "via": "sendgrid"}

    # ── 2. SMTP ───────────────────────────────────────────────────────────────
    if cfg["user"] and cfg["password"]:
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{cfg['from_name']} <{cfg['user']}>"
        msg["To"]      = to_email
        msg.attach(MIMEText(body, "plain", "utf-8"))
        if html:
            msg.attach(MIMEText(html, "html", "utf-8"))
        try:
            _send_smtp(cfg, to_email, msg)
            print(f"[email] SMTP sent TO: {to_email}")
            return {"sent": True, "to": to_email, "via": "smtp"}
        except smtplib.SMTPAuthenticationError:
            raise RuntimeError(
                "Gmail rejected the App Password — check Settings."
            )
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc

    # ── 3. Stub ───────────────────────────────────────────────────────────────
    print(f"[email] Not configured — skipping send TO: {to_email} | {subject}")
    return {"sent": False, "stub": True, "to": to_email}


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
    """
    STUB — Darwinbox offer handoff.  Replace the body of this function with the
    real Darwinbox REST API call when the integration is ready.

    ── What the dev team needs to wire this up ──────────────────────────────────

    1. API base URL
       Darwinbox provides a tenant-specific base URL, typically:
         https://<your-tenant>.darwinbox.in/apiv2/
       Obtain this from your Darwinbox implementation partner or admin portal.

    2. Authentication
       Darwinbox uses OAuth 2.0 client credentials for API access:
         POST /oauth/token
           grant_type    = client_credentials
           client_id     = <from Darwinbox admin panel>
           client_secret = <from Darwinbox admin panel>
       Store client_id and client_secret in system_settings or .env.prod,
       NOT hard-coded here.  Bearer token expires — implement token caching.

    3. Payload format (candidate offer)
       The exact field names depend on your Darwinbox module configuration.
       Typical fields for an offer record:
         {
           "employee_code":    "<auto-assigned or passed>",
           "first_name":       "<from candidate>",
           "last_name":        "<from candidate>",
           "email":            "<candidate email>",
           "designation":      offer["designation"],
           "date_of_joining":  offer["joining_date"],  // "YYYY-MM-DD"
           "cost_to_company":  offer["total_ctc"],      // annual, numeric
           "department":       "<from requisition BU>",
           "location":         "<from requisition>",
           "employment_type":  "full_time" | "contract",
         }
       Confirm exact keys with Darwinbox during integration testing.

    4. Endpoint
       POST /apiv2/employee/create  (or /apiv2/offer/create — verify with Darwinbox)
       Headers:
         Authorization: Bearer <access_token>
         Content-Type:  application/json

    5. Response
       On success Darwinbox returns an employee/offer ID — store that in
       offer.darwin_ref so you can look up the record later.

    6. Error handling
       - 401: token expired, refresh and retry once
       - 422: payload validation error — log the full response body
       - 5xx: transient — use exponential back-off (max 3 retries)

    7. Security note
       All Darwinbox credentials MUST live in system_settings or .env.prod.
       Never commit credentials to source control.

    ─────────────────────────────────────────────────────────────────────────────
    Until the integration is wired up, this function logs the payload and returns
    a synthetic reference so the rest of the approval workflow completes normally.
    The darwin_ref stored in the offer table will start with "STUB-" — the dev
    team can query `SELECT * FROM offer WHERE darwin_ref LIKE 'STUB-%'` to find
    all offers that still need real Darwinbox pushes after go-live.
    """
    stub_ref = f"STUB-DRW-{uuid.uuid4().hex[:8].upper()}"
    print(
        f"[darwinbox STUB] push_offer_to_darwin called — offer_id={offer.get('id')} "
        f"candidate='{offer.get('candidate')}' designation='{offer.get('designation')}' "
        f"total_ctc={offer.get('total_ctc')} joining_date={offer.get('joining_date')} "
        f"darwin_ref_assigned={stub_ref}"
    )
    return {
        "darwin_pushed": True,
        "darwin_ref":    stub_ref,
    }
