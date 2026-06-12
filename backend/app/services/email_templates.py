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
            "Please use the link below to attend your interview at your convenience:\n"
            "  {{interview_link}}\n\n"
            "The interview takes approximately 25–30 minutes. "
            "You will need a microphone and a quiet environment.\n\n"
            "Important:\n"
            "- Once you start, you have 48 hours to complete the interview\n"
            "- You can close and re-open the link within that window if needed\n"
            "- NexAI never auto-rejects — all scores are reviewed by a human recruiter\n\n"
            "Best regards,\n"
            "Egnex Hiring Team | {{company_name}}"
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

    # ── Meeting Notetaker email template ─────────────────────────────────────

    "meeting_summary": {
        "name":    "Interview Transcript Summary (Recruiter)",
        "subject": "Interview summary: {{candidate_name}} — {{job_title}} ({{interview_date}})",
        "body": (
            "Hi {{recruiter_name}},\n\n"
            "Here is the AI-generated summary for your interview with {{candidate_name}} "
            "for the position of {{job_title}} on {{interview_date}}.\n\n"
            "── DISCUSSION POINTS ──\n{{discussion_points}}\n\n"
            "── STRENGTHS ──\n{{strengths}}\n\n"
            "── CONCERNS / GAPS ──\n{{concerns}}\n\n"
            "── OVERALL NOTE ──\n{{overall_note}}\n\n"
            "The full transcript is available in Egnex (Interviews → Transcript).\n\n"
            "Regards,\nEgnex Hiring System"
        ),
        "valid_placeholders": [
            "recruiter_name", "candidate_name", "job_title", "interview_date",
            "discussion_points", "strengths", "concerns", "overall_note",
        ],
        "category": "panel",
    },

    # ── Offer & Approvals email templates ─────────────────────────────────────

    "offer_awaiting_approval": {
        "name":    "Offer Awaiting Approval (Approver)",
        "subject": "Action required: Offer approval needed — {{candidate_name}} ({{job_title}})",
        "body": (
            "Hi {{approver_name}},\n\n"
            "An offer is awaiting your approval (step {{step_num}} of {{total_steps}}).\n\n"
            "Candidate:    {{candidate_name}}\n"
            "Role:         {{job_title}}\n"
            "Designation:  {{designation}}\n"
            "Total CTC:    {{total_ctc}}\n"
            "Joining Date: {{joining_date}}\n\n"
            "Please log in to Egnex and navigate to Offers & Approvals to approve or reject this offer.\n\n"
            "Regards,\nEgnex Hiring Team"
        ),
        "valid_placeholders": [
            "approver_name", "candidate_name", "job_title", "designation",
            "total_ctc", "joining_date", "step_num", "total_steps",
        ],
        "category": "panel",
    },

    "offer_step_approved": {
        "name":    "Offer Step Approved — Audit (Recruiter + TA Manager)",
        "subject": "Offer approved at step {{step_num}}/{{total_steps}} — {{candidate_name}} ({{job_title}})",
        "body": (
            "Offer Approval Audit\n\n"
            "Candidate:   {{candidate_name}}\n"
            "Role:        {{job_title}}\n"
            "Approved by: {{approver_name}}\n"
            "Step:        {{step_num}} of {{total_steps}}\n"
            "At:          {{approved_at}}\n"
            "Notes:       {{notes}}\n\n"
            "This is an automated audit notification. No action is required at this stage.\n\n"
            "Regards,\nEgnex Hiring System"
        ),
        "valid_placeholders": [
            "candidate_name", "job_title", "approver_name",
            "step_num", "total_steps", "approved_at", "notes",
        ],
        "category": "panel",
    },

    "offer_rejected": {
        "name":    "Offer Rejected — Action Required (Recruiter + TA Manager)",
        "subject": "Offer REJECTED at step {{step_num}} — {{candidate_name}} ({{job_title}})",
        "body": (
            "Offer Rejected\n\n"
            "Candidate:   {{candidate_name}}\n"
            "Role:        {{job_title}}\n"
            "Rejected by: {{approver_name}}\n"
            "Step:        {{step_num}}\n"
            "Reason:      {{notes}}\n"
            "At:          {{rejected_at}}\n\n"
            "The offer is now in 'Revising' state. The recruiter must update the offer details "
            "and resubmit it to restart the approval chain.\n\n"
            "Regards,\nEgnex Hiring System"
        ),
        "valid_placeholders": [
            "candidate_name", "job_title", "approver_name",
            "step_num", "notes", "rejected_at",
        ],
        "category": "panel",
    },

    # ── HM Requisition Approval email templates ──────────────────────────────

    "hm_req_approval_request": {
        "name":    "HM Requisition Approval Request (TA Manager)",
        "subject": "Approval required: New requisition '{{req_title}}' from {{hm_name}}",
        "body": (
            "Hi TA Team,\n\n"
            "{{hm_name}} has created a new requisition that requires your approval "
            "before it becomes active in the pipeline.\n\n"
            "Requisition: {{req_title}}\n"
            "Submitted by: {{hm_name}} (Hiring Manager)\n\n"
            "Please log in to Egnex and navigate to 'Req Approvals' to approve or "
            "reject this requisition.\n\n"
            "Regards,\nEgnex Hiring System"
        ),
        "valid_placeholders": ["hm_name", "req_title"],
        "category": "panel",
    },
    "hm_req_approved": {
        "name":    "HM Requisition Approved (Hiring Manager Notification)",
        "subject": "Your requisition '{{req_title}}' has been approved",
        "body": (
            "Hi {{hm_name}},\n\n"
            "Your requisition '{{req_title}}' has been approved by the TA team "
            "and is now active in the pipeline.\n\n"
            "Candidates can now be received and processed for this position.\n\n"
            "Regards,\nEgnex Hiring System"
        ),
        "valid_placeholders": ["hm_name", "req_title"],
        "category": "panel",
    },
    "hm_req_rejected": {
        "name":    "HM Requisition Rejected (Hiring Manager Notification)",
        "subject": "Your requisition '{{req_title}}' was not approved",
        "body": (
            "Hi {{hm_name}},\n\n"
            "Your requisition '{{req_title}}' could not be approved at this time.\n\n"
            "Reason: {{reason}}\n\n"
            "Please contact the TA team if you have questions or wish to revise "
            "and resubmit.\n\n"
            "Regards,\nEgnex Hiring System"
        ),
        "valid_placeholders": ["hm_name", "req_title", "reason"],
        "category": "panel",
    },

    "application_received_jd": {
        "name":    "Application Received — Job Description (Candidate Confirmation)",
        "subject": "Your application for {{job_title}} has been received",
        "body": (
            "Hi {{candidate_name}},\n\n"
            "Thank you for applying — your application for {{job_title}} "
            "({{location}}) has been submitted successfully.\n\n"
            "Please find the job description below:\n\n"
            "──────────────────────────────\n"
            "Job Title:      {{job_title}}\n"
            "Location:       {{location}}\n"
            "Experience:     {{experience}}\n"
            "Qualification:  {{qualification}}\n"
            "──────────────────────────────\n\n"
            "{{jd_body}}\n\n"
            "About Amnex:\n{{about_company}}\n\n"
            "Our recruitment team will review your profile and be in touch.\n\n"
            "Regards,\nEgnex Hiring System"
        ),
        "valid_placeholders": [
            "candidate_name", "job_title", "location",
            "experience", "qualification", "jd_body", "about_company",
        ],
        "category": "candidate",
    },

    "offer_approved_darwinbox": {
        "name":    "Offer Fully Approved — Sent to Darwinbox (Recruiter + TA Manager)",
        "subject": "Offer fully approved — {{candidate_name}} sent to Darwinbox (Ref: {{darwin_ref}})",
        "body": (
            "Offer Fully Approved\n\n"
            "Candidate:    {{candidate_name}}\n"
            "Role:         {{job_title}}\n"
            "Designation:  {{designation}}\n"
            "Total CTC:    {{total_ctc}}\n"
            "Joining Date: {{joining_date}}\n"
            "Darwinbox Ref: {{darwin_ref}}\n"
            "Approved At:  {{approved_at}}\n\n"
            "The offer has cleared all approval steps and has been handed off to Darwinbox "
            "for letter generation and onboarding initiation.\n\n"
            "Regards,\nEgnex Hiring System"
        ),
        "valid_placeholders": [
            "candidate_name", "job_title", "designation", "total_ctc",
            "joining_date", "darwin_ref", "approved_at",
        ],
        "category": "panel",
    },
}

# Placeholders guaranteed to be fillable for ANY application (used by custom templates)
CUSTOM_PLACEHOLDERS: list[str] = [
    "candidate_name", "job_title", "company_name", "recruiter_name",
]

# Keys of all built-in templates (used to guard against deletion)
BUILTIN_KEYS: frozenset[str] = frozenset(DEFAULTS)

# ── Sample data for live preview ──────────────────────────────────────────────
SAMPLE_VALUES: dict[str, str] = {
    "candidate_name":     "Rimjhim Rai",
    "job_title":          "Account Manager – Sales",
    "company_name":       "Amnex Infotechnologies Pvt Ltd",
    "interview_link":     "https://egnex.amnex.com/nexai-interview?token=preview_sample",
    "ai_score":           "78/100",
    "strengths":          "Strong communication, relevant industry experience, clear articulation of achievements.",
    "concerns":           "Limited enterprise CRM experience — probe on technical sales cycle management.",
    "interview_time":     "Thursday, 12 June 2026 at 11:00 AM IST",
    "meet_link":          "https://meet.google.com/abc-defg-hij",
    "recruiter_name":     "Priya Sharma",
    # meeting_summary placeholders
    "interview_date":     "12 June 2026",
    "discussion_points":  "Candidate's sales experience, key accounts managed, CRM tools used.",
    "overall_note":       "Strong candidate — recommend advancing to panel interview.",
    # offer email placeholders
    "approver_name":      "Rajesh Mehta",
    "designation":        "Senior Account Manager",
    "total_ctc":          "₹14,00,000",
    "joining_date":       "01 August 2026",
    "step_num":           "1",
    "total_steps":        "3",
    "approved_at":        "12 June 2026 at 2:30 PM",
    "rejected_at":        "12 June 2026 at 2:30 PM",
    "notes":              "All requirements met.",
    "darwin_ref":         "STUB-DRW-2026001",
    # hm req approval placeholders
    "hm_name":   "Bhaumik Patel",
    "req_title": "Senior Software Engineer",
    "reason":    "Budget not approved for this quarter.",
    # application_received_jd placeholders
    "location":       "Ahmedabad, India",
    "experience":     "3–5 years",
    "qualification":  "B.E. / B.Tech in Computer Science or equivalent",
    "jd_body":        "We are looking for a motivated engineer to join our team...",
    "about_company":  "Amnex Infotechnologies Pvt. Ltd. is a leading technology company.",
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
                       (name, subject, body, category, template_key, valid_placeholders, is_builtin)
                       VALUES (%s, %s, %s, %s, %s, %s::jsonb, TRUE)""",
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
        else:
            # Ensure is_builtin is stamped on rows that pre-date this migration
            try:
                query(
                    "UPDATE email_template SET is_builtin = TRUE WHERE template_key = %s AND is_builtin IS DISTINCT FROM TRUE",
                    [key],
                    fetch=False,
                )
            except Exception:
                pass


def get_template(key: str) -> dict:
    """
    Return the template for *key*.

    Priority:
      1. DB row with matching template_key (most-recently edited by admin)
      2. Built-in default (guarantees email can always be sent)
    """
    try:
        row = query_one(
            "SELECT name, subject, body, valid_placeholders, category "
            "FROM email_template WHERE template_key = %s AND is_active = TRUE LIMIT 1",
            [key],
        )
    except Exception:
        row = None  # DB schema not yet migrated — fall through to built-in default
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
    if key in DEFAULTS:
        valid = set(DEFAULTS[key].get("valid_placeholders", []))
    else:
        # Custom template: derive valid set from DB row
        row = query_one(
            "SELECT valid_placeholders FROM email_template WHERE template_key = %s LIMIT 1",
            [key],
        )
        vp = row["valid_placeholders"] if row else []
        if isinstance(vp, str):
            try:
                vp = json.loads(vp)
            except Exception:
                vp = []
        valid = set(vp or CUSTOM_PLACEHOLDERS)
    used    = _find_placeholders(subject) | _find_placeholders(body)
    unknown = used - valid
    return [f"Unknown placeholder: {{{{{p}}}}}" for p in sorted(unknown)]


def get_custom_templates() -> list[dict]:
    """Return all active custom (non-builtin) templates from the DB.
    Returns [] gracefully if migration 26 hasn't been applied yet."""
    try:
        rows = query(
            """SELECT template_key, name, category, valid_placeholders
               FROM email_template
               WHERE is_builtin = FALSE AND is_active = TRUE
               ORDER BY name""",
        )
    except Exception:
        return []
    result = []
    for r in (rows or []):
        vp = r["valid_placeholders"]
        if isinstance(vp, str):
            try:
                vp = json.loads(vp)
            except Exception:
                vp = []
        result.append({
            "template_key":       r["template_key"],
            "name":               r["name"],
            "category":           r.get("category", "custom"),
            "valid_placeholders": vp or CUSTOM_PLACEHOLDERS,
            "is_builtin":         False,
        })
    return result
