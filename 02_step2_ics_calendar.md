# Step 2 — Calendar invites via ICS over SMTP (no Google)

Goal: scheduling an interview emails the candidate + each panel member, from hr@amnex.com, a real `.ics` invite they can Accept/Decline. The meeting link is a plain URL the recruiter pastes (or blank). No OAuth, no Google API.

## 2A. Add ICS helpers to `backend/app/services/connectors.py`

Add these two functions near the bottom of the EMAIL section (after `send_email`, before the AI bot stub):

```python
# ------------------------------------------------------------------ #
#  CALENDAR INVITES  (ICS over SMTP — no Google API)                  #
# ------------------------------------------------------------------ #

def _ics_escape(text: str) -> str:
    """Escape a value per RFC 5545 (commas, semicolons, backslashes, newlines)."""
    if text is None:
        return ""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def build_ics(
    summary: str,
    description: str,
    start_dt_utc: datetime,
    duration_min: int,
    organizer_email: str,
    attendee_emails: list,
    location: str = "",
    uid: Optional[str] = None,
) -> str:
    """
    Build a minimal, valid VCALENDAR/VEVENT string (METHOD:REQUEST).
    start_dt_utc must be a UTC datetime (naive treated as UTC).
    """
    end_dt = start_dt_utc + timedelta(minutes=duration_min)
    fmt = "%Y%m%dT%H%M%SZ"
    stamp = datetime.utcnow().strftime(fmt)
    ev_uid = uid or f"{uuid.uuid4().hex}@egnex"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Amnex//Egnex//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{ev_uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{start_dt_utc.strftime(fmt)}",
        f"DTEND:{end_dt.strftime(fmt)}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        f"LOCATION:{_ics_escape(location)}",
        f"ORGANIZER;CN=Amnex Talent Acquisition:mailto:{organizer_email}",
        "STATUS:CONFIRMED",
        "SEQUENCE:0",
        "TRANSP:OPAQUE",
    ]
    for em in attendee_emails:
        if em:
            lines.append(
                f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{em}"
            )
    lines += [
        "BEGIN:VALARM",
        "TRIGGER:-PT30M",
        "ACTION:DISPLAY",
        "DESCRIPTION:Interview reminder",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines)


def send_calendar_invite(
    to_emails: list,
    subject: str,
    body_text: str,
    start_dt_utc: datetime,
    duration_min: int,
    location: str = "",
    reply_to: Optional[str] = None,
) -> dict:
    """
    Send a calendar invite (.ics attached) from hr@amnex.com to each recipient.
    One email per recipient so the candidate never sees the panel list.
    Falls back to a plain email if SMTP isn't configured (logged, never raises here).
    """
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders as _enc

    cfg = _load_email_cfg()
    if not (cfg["user"] and cfg["password"]):
        print(f"[calendar] SMTP not configured — invite NOT sent to {to_emails}")
        return {"sent": False, "stub": True, "to": to_emails}

    organizer = cfg["user"]  # hr@amnex.com
    shared_uid = f"{uuid.uuid4().hex}@egnex"
    ics_text = build_ics(
        summary=subject,
        description=body_text,
        start_dt_utc=start_dt_utc,
        duration_min=duration_min,
        organizer_email=organizer,
        attendee_emails=to_emails,
        location=location,
        uid=shared_uid,
    )

    sent_ok = []
    for to_email in to_emails:
        if not to_email:
            continue
        # multipart/mixed: text body + calendar part + .ics attachment (Outlook-friendly)
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"]    = f"{cfg['from_name']} <{organizer}>"
        msg["To"]      = to_email
        if reply_to:
            msg["Reply-To"] = reply_to

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_text, "plain", "utf-8"))
        cal_part = MIMEText(ics_text, "calendar", "utf-8")
        cal_part.add_header("Content-Type", 'text/calendar; method=REQUEST; charset="utf-8"')
        alt.attach(cal_part)
        msg.attach(alt)

        ics_attach = MIMEBase("application", "ics")
        ics_attach.set_payload(ics_text.encode("utf-8"))
        _enc.encode_base64(ics_attach)
        ics_attach.add_header("Content-Disposition", 'attachment; filename="invite.ics"')
        msg.attach(ics_attach)

        try:
            _send_smtp(cfg, to_email, msg)
            print(f"[calendar] invite sent TO: {to_email}")
            sent_ok.append(to_email)
        except Exception as exc:
            print(f"[calendar] invite FAILED to {to_email}: {exc}")

    return {"sent": bool(sent_ok), "to": sent_ok, "via": "smtp_ics"}
```

## 2B. Rewrite `schedule_meeting` in `connectors.py` (drop Google)

### FIND the entire `def schedule_meeting(...)` function (the Google Calendar one) and REPLACE WITH:
```python
def schedule_meeting(organizer_email: str, candidate_email: str,
                     panel_emails: list, start_time: datetime,
                     duration_min: int = 45, meet_link: str = "",
                     candidate_name: str = "Candidate",
                     job_title: str = "the role") -> dict:
    """
    Create a calendar invite (.ics over SMTP) from hr@amnex.com and email it
    to the candidate + each panel member. No Google API.

    meet_link: optional video URL (Jitsi/Teams/Zoom/Meet) pasted by the recruiter.
    Returns the same shape callers already expect (gcal_event_id is None now).
    """
    when = start_time.strftime("%A, %d %B %Y at %I:%M %p UTC")
    location = meet_link or "To be confirmed"
    body = (
        f"You are invited to an interview for {job_title}.\n\n"
        f"When: {when}\n"
        f"Duration: {duration_min} minutes\n"
        f"Join link: {meet_link or 'will be shared separately'}\n\n"
        f"Please accept this invite to confirm.\n\n"
        f"— Amnex Talent Acquisition"
    )
    all_emails = list({candidate_email} | set(panel_emails or []))
    send_calendar_invite(
        to_emails=all_emails,
        subject=f"Interview – {candidate_name} – {job_title}",
        body_text=body,
        start_dt_utc=start_time,
        duration_min=duration_min,
        location=location,
        reply_to=organizer_email,
    )
    return {
        "gcal_event_id": None,
        "meet_link":     meet_link,
        "scheduled_at":  start_time.isoformat(),
        "conflicts":     [],
    }
```

> You can also delete `_get_calendar_service` (the Google helper) — nothing else uses it once `schedule_meeting` is rewritten (verify with a grep).

## 2C. Update `/api/schedule` route in `backend/app/main.py`

### FIND the `ScheduleIn` model:
```python
class ScheduleIn(BaseModel):
    application_id: str
    recruiter_id: str
    panel_emails: list[str] = []
    start_in_hours: int = 24
    duration_min: int = 45
```
### REPLACE WITH:
```python
class ScheduleIn(BaseModel):
    application_id: str
    panel_emails: list[str] = []
    start_in_hours: int = 24
    duration_min: int = 45
    meet_link: str = ""
```

### FIND inside `def schedule(...)` the meeting call:
```python
    start = datetime.utcnow() + timedelta(hours=payload.start_in_hours)
    try:
        meeting = connectors.schedule_meeting(
            payload.recruiter_id, app_row["email"], payload.panel_emails,
            start, payload.duration_min,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
```
### REPLACE WITH:
```python
    start = datetime.utcnow() + timedelta(hours=payload.start_in_hours)
    organizer_email = request.state.user.get("email") or ""
    meeting = connectors.schedule_meeting(
        organizer_email=organizer_email,
        candidate_email=app_row["email"],
        panel_emails=payload.panel_emails,
        start_time=start,
        duration_min=payload.duration_min,
        meet_link=payload.meet_link,
        candidate_name=app_row.get("full_name") or "Candidate",
        job_title=app_row.get("job_title") or "the role",
    )
```

> The `interview` INSERT below already stores `meeting["meet_link"]` and `meeting["gcal_event_id"]` (now None) — leave it. The follow-up `interview_scheduled` email block can stay; it's now redundant with the ICS body but harmless. Optional: delete it to avoid two emails.

## 2D. (Optional) Add a "Schedule Interview" button in the frontend
`/api/schedule` currently has NO caller. To actually use it, add a small modal on the pipeline/interviews screen that collects: panel emails (comma-sep), start-in-hours (or a datetime), duration, and an optional **meeting link** text box, then `POST /api/schedule`. If you'd rather wire this later, the endpoint is callable now via API and the dev team can build UI post-launch.

## VERIFY Step 2
1. Call `POST /api/schedule` (Postman or the new button) with a real candidate email + a meet_link.
2. Confirm an email from hr@amnex.com arrives with an **invite.ics** that adds to Google/Outlook/Apple calendar and shows Accept/Decline.
3. Logs show `[calendar] invite sent TO: ...`.
