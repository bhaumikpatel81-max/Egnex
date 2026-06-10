"""
Email template CRUD endpoints.

Accessible by admin and ta_manager only (server-enforced).
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth_utils import get_current_user
from ..services.connectors import send_email
from ..db import query, query_one
from ..services.email_templates import (
    DEFAULTS,
    SAMPLE_VALUES,
    get_template,
    render_template,
    validate_placeholders,
)

router = APIRouter()


# ── Permission guard ──────────────────────────────────────────────────────────

def _require_template_access(user=Depends(get_current_user)):
    if user["role"] not in ("admin", "ta_manager"):
        raise HTTPException(403, "Email template management is restricted to admin and TA managers.")
    return user


# ── Schemas ───────────────────────────────────────────────────────────────────

class TemplateSavePayload(BaseModel):
    subject: str
    body: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/email-templates")
def list_templates(user=Depends(_require_template_access)):
    """List all known template keys with metadata."""
    result = []
    for key, dflt in DEFAULTS.items():
        row = query_one(
            "SELECT subject, body, updated_at FROM email_template "
            "WHERE template_key = %s AND is_active = TRUE LIMIT 1",
            [key],
        )
        result.append({
            "template_key":       key,
            "name":               dflt["name"],
            "category":           dflt.get("category", ""),
            "valid_placeholders": dflt["valid_placeholders"],
            "is_customised":      row is not None,
            "updated_at":         row["updated_at"].isoformat() if row and row.get("updated_at") else None,
        })
    return result


@router.get("/api/email-templates/{key}")
def get_one_template(key: str, user=Depends(_require_template_access)):
    """Return full template (subject + body) for editing."""
    if key not in DEFAULTS:
        raise HTTPException(404, f"Unknown template key '{key}'")
    tmpl = get_template(key)
    tmpl["sample_values"] = SAMPLE_VALUES
    return tmpl


@router.put("/api/email-templates/{key}")
def save_template(key: str, payload: TemplateSavePayload, user=Depends(_require_template_access)):
    """
    Save (upsert) a template.  Warns on unknown placeholders but does NOT block —
    the admin may intentionally extend the template.  The render step will block
    if a placeholder has no value at send time.
    """
    if key not in DEFAULTS:
        raise HTTPException(404, f"Unknown template key '{key}'")

    warnings = validate_placeholders(key, payload.subject, payload.body)

    dflt = DEFAULTS[key]
    existing = query_one(
        "SELECT id FROM email_template WHERE template_key = %s LIMIT 1", [key]
    )
    if existing:
        query(
            """UPDATE email_template
               SET subject = %s, body = %s, updated_at = now(), updated_by = %s
               WHERE template_key = %s""",
            [payload.subject, payload.body, user["sub"], key],
            fetch=False,
        )
    else:
        query(
            """INSERT INTO email_template
               (name, subject, body, category, template_key, valid_placeholders, created_by)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)""",
            [
                dflt["name"], payload.subject, payload.body,
                dflt.get("category", ""), key,
                json.dumps(dflt["valid_placeholders"]),
                user["sub"],
            ],
            fetch=False,
        )

    return {"ok": True, "warnings": warnings}


@router.post("/api/email-templates/{key}/reset")
def reset_template(key: str, user=Depends(_require_template_access)):
    """Reset template back to the built-in default."""
    if key not in DEFAULTS:
        raise HTTPException(404, f"Unknown template key '{key}'")

    dflt = DEFAULTS[key]
    query(
        """UPDATE email_template
           SET subject = %s, body = %s, updated_at = now(), updated_by = %s
           WHERE template_key = %s""",
        [dflt["subject"], dflt["body"], user["sub"], key],
        fetch=False,
    )
    return {"ok": True}


@router.post("/api/email-templates/{key}/test-send")
def test_send_template(key: str, user=Depends(_require_template_access)):
    """
    Render template with sample data and send to the current user's email.
    Raises 422 if any placeholder cannot be filled.
    """
    if key not in DEFAULTS:
        raise HTTPException(404, f"Unknown template key '{key}'")

    user_row = query_one("SELECT email, full_name FROM app_user WHERE id = %s", [user["sub"]])
    if not user_row:
        raise HTTPException(404, "User not found")

    try:
        subject, body = render_template(key, SAMPLE_VALUES)
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    subject = f"[TEST] {subject}"
    try:
        send_email(user_row["email"], subject, body)
    except Exception as exc:
        raise HTTPException(500, f"Email delivery failed: {exc}")

    return {"ok": True, "sent_to": user_row["email"]}
