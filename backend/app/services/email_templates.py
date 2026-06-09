"""
Email template service.

Loads templates from the email_template table (keyed by template_key).
Falls back to built-in defaults so email never silently fails if the DB row
is missing.  Validates and substitutes {{placeholder}} tokens at send time —
NEVER sends a message containing raw {{ }} braces.
"""
import json
import re
from typing import Optional

from ..db import query, query_one

# ── Placeholder regex ─────────────────────────────────────────────────────────
_PH_RE = re.compile(r'\{\{(\w+)\}\}')

# ── Built-in defaults ─────────────────────────────────────────────────────────
# These reproduce the CURRENT hardcoded emails exactly.
# They are inserted into the DB if the row is absent (idempotent via template_key).

DEFAULTS: dict[str, dict] = {
    "nexai_invite": {
        "name":    "NexAI Interview Invite (Candidate)",
        "subject": "AI Interview Invitation: {{job_title}} — {{company_name}}",
        "body": (
            "Hi {{candidate_name}},\n\n"
            "Congratulations! You have been shortlisted for an AI Screening Interview "
            "for the position of {{job_title}} at {{company_name}}.\n\n"
            "Please use the link below to attend your interview at your convenience:\n\n"
            "  {{interview_link}}\n\n"
            "The interview takes approximately 25–30 minutes. "
            "You will need a microphone and a quiet environment.\n\n"
            "Important:\n"
            "- Once you start, you have 48 hours to complete the interview\n"
            "- You can close and re-open the link within that window if needed\n"
            "- NexAI never auto-rejects — all scores are reviewed by a human recruiter\n\n"
            "Best regards,\nEgnex Hiring Team | {{company_name}}"
        ),
        "valid_placeholders": [
            "candidate_name", "job_title", "company_name", "interview_link",
        ],
        "category": "candidate",
    },
    "nexai_completion": {
        "name":    "NexAI Interview Completed (Recruiter)",
        "subject": "NexAI interview completed — {{candidate_name}} — {{job_title}}",
        "body": (
            "NexAI Interview Completed\n\n"
            "Candidate: {{candidate_name}}\n"
            "Role: {{job_title}}\n"
            "AI Score: {{ai_score}}\n\n"
            "Strengths:\n{{strengths}}\n\n"
            "Areas to Probe:\n{{concerns}}"
        ),
        "valid_placeholders": [
            "candidate_name", "job_title", "ai_score", "strengths", "concerns",
        ],
        "category": "panel",
    },
    "interview_scheduled": {
        "name":    "Interview Scheduled (Candidate)",
        "subject": "Interview scheduled: {{job_title}}",
        "body": (
            "Hi {{candidate_name}},\n\n"
            "Your interview for {{job_title}} has been scheduled.\n\n"
            "Date & Time: {{interview_time}}\n"
            "Meeting Link: {{meet_link}}\n\n"
            "Please join on time. If you have any questions, please reply to this email.\n\n"
            "Regards,\nEgnex Hiring Team"
        ),
        "valid_placeholders": [
            "candidate_name", "job_title", "interview_time", "meet_link",
        ],
        "category": "candidate",
    },
}

# ── Sample data for live preview ──────────────────────────────────────────────
SAMPLE_VALUES: dict[str, str] = {
    "candidate_name":  "Rimjhim Rai",
    "job_title":       "Account Manager – Sales",
    "company_name":    "Amnex Infotechnologies Pvt Ltd",
    "interview_link":  "https://egnex.amnex.com/nexai-interview?token=preview_sample",
    "ai_score":        "78/100",
    "strengths":       "Strong communication, relevant industry experience, clear articulation of achievements.",
    "concerns":        "Limited enterprise CRM experience — probe on technical sales cycle management.",
    "interview_time":  "Thursday, 12 June 2026 at 11:00 AM IST",
    "meet_link":       "https://meet.google.com/abc-defg-hij",
    "recruiter_name":  "Priya Sharma",
}


# ── DB helpers ────────────────────────────────────────────────────────────────

def ensure_defaults() -> None:
    """
    Insert built-in default templates for any template_key not yet in the DB.
    Called once at application startup — idempotent.
    """
    for key, tmpl in DEFAULTS.items():
        existing = query_one(
            "SELECT id FROM email_template WHERE template_key = %s LIMIT 1",
            [key],
        )
        if not existing:
            try:
                query(
                    """INSERT INTO email_template
                       (name, subject, body, category, template_key, valid_placeholders)
                       VALUES (%s, %s, %s, %s, %s, %s::jsonb)""",
                    [
                        tmpl["name"], tmpl["subject"], tmpl["body"],
                        tmpl.get("category", ""),
                        key,
                        json.dumps(tmpl["valid_placeholders"]),
                    ],
                    fetch=False,
                )
            except Exception as exc:
                print(f"[email_templates] Could not seed default '{key}': {exc}")


def get_template(key: str) -> dict:
    """
    Return the template for *key*.

    Priority:
      1. DB row with matching template_key (most-recently edited by admin)
      2. Built-in default (guarantees email can always be sent)
    """
    row = query_one(
        "SELECT name, subject, body, valid_placeholders, category "
        "FROM email_template WHERE template_key = %s AND is_active = TRUE LIMIT 1",
        [key],
    )
    if row:
        vp = row["valid_placeholders"]
        if isinstance(vp, str):
            try:
                vp = json.loads(vp)
            except Exception:
                vp = []
        vp = vp or []
        return {
            "template_key":       key,
            "name":               row["name"],
            "subject":            row["subject"],
            "body":               row["body"],
            "valid_placeholders": vp,
            "category":           row.get("category", ""),
            "source":             "db",
        }

    default = DEFAULTS.get(key)
    if default:
        return {
            "template_key":       key,
            "name":               default["name"],
            "subject":            default["subject"],
            "body":               default["body"],
            "valid_placeholders": default["valid_placeholders"],
            "category":           default.get("category", ""),
            "source":             "default",
        }

    raise KeyError(f"No email template found for key '{key}'")


def _find_placeholders(text: str) -> set[str]:
    return set(_PH_RE.findall(text or ""))


def _substitute(text: str, values: dict) -> str:
    return _PH_RE.sub(lambda m: str(values[m.group(1)]), text)


def render_template(key: str, values: dict) -> tuple[str, str]:
    """
    Load template for *key*, substitute {{placeholders}} with *values*.

    Returns (rendered_subject, rendered_body).

    Raises ValueError if any placeholder in subject or body cannot be filled —
    the caller must handle this and NEVER send a message containing raw braces.
    """
    tmpl = get_template(key)
    subject = tmpl["subject"] or ""
    body    = tmpl["body"]    or ""

    all_ph = _find_placeholders(subject) | _find_placeholders(body)
    missing = [p for p in all_ph if not values.get(p)]
    if missing:
        raise ValueError(
            f"placeholder(s) have no value: {', '.join(sorted(missing))}"
        )

    return _substitute(subject, values), _substitute(body, values)


def validate_placeholders(key: str, subject: str, body: str) -> list[str]:
    """
    Return a list of warning strings for placeholders in subject/body that are
    not in the valid_placeholders list for this template type.
    Empty list means everything is fine.
    """
    default = DEFAULTS.get(key, {})
    valid   = set(default.get("valid_placeholders", []))
    used    = _find_placeholders(subject) | _find_placeholders(body)
    unknown = used - valid
    return [f"Unknown placeholder: {{{{{p}}}}}" for p in sorted(unknown)]
