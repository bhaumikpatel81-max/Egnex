"""
Self-service password flows — first-time set + forgot/reset.
All emails sent from hr@amnex.com (SMTP). Tokens are single-use & expiring.
"""
import hashlib
import os
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import hash_password, require_admin
from ..services.connectors import send_email, _load_email_cfg

router = APIRouter(prefix="/api/auth", tags=["password"])

_TOKEN_TTL_HOURS = 24
# Only these roles may use self-service password set/reset
_SELF_SERVICE_ROLES = {"admin", "ta_manager", "recruiter"}


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _base_url() -> str:
    cfg = _load_email_cfg()
    return (cfg.get("base_url") or os.environ.get("APP_BASE_URL", "")).rstrip("/")


def _issue_token(user_id: str, purpose: str) -> str:
    """Create a single-use token, store its hash, return the raw token."""
    raw = secrets.token_urlsafe(32)
    query(
        """INSERT INTO password_reset_token (user_id, token_hash, purpose, expires_at)
           VALUES (%s, %s, %s, %s)""",
        [user_id, _hash_token(raw), purpose,
         datetime.utcnow() + timedelta(hours=_TOKEN_TTL_HOURS)],
        fetch=False,
    )
    return raw


def _send_link_email(to_email: str, full_name: str, raw_token: str, purpose: str):
    link = f"{_base_url()}/set-password?token={raw_token}"
    if purpose == "invite":
        subject = "Set your Egnex password"
        intro = (
            f"Hi {full_name or ''},\n\n"
            "An account has been created for you on Egnex (Amnex Talent Acquisition).\n"
            f"Your username is your email: {to_email}\n\n"
            "Set your password using the secure link below:"
        )
    else:
        subject = "Reset your Egnex password"
        intro = (
            f"Hi {full_name or ''},\n\n"
            "We received a request to reset your Egnex password.\n"
            "If you didn't request this, you can ignore this email.\n\n"
            "Reset your password using the secure link below:"
        )
    body = (
        f"{intro}\n\n{link}\n\n"
        f"This link expires in {_TOKEN_TTL_HOURS} hours and can be used once.\n\n"
        "— Amnex Talent Acquisition"
    )
    send_email(to_email, subject, body)


# ── Admin: send a set-password invite to a user ───────────────────────────────

class InviteIn(BaseModel):
    email: str


@router.post("/send-setup-link")
def send_setup_link(body: InviteIn, admin=Depends(require_admin)):
    """Admin triggers a first-time 'set your password' email to a user."""
    user = query_one(
        "SELECT id, full_name, email, role, is_active FROM app_user WHERE email=%s",
        [body.email.lower().strip()],
    )
    if not user or not user["is_active"]:
        raise HTTPException(404, "Active user with that email not found")
    if user["role"] not in _SELF_SERVICE_ROLES:
        raise HTTPException(400, "Self-service password is only for admin / TA manager / recruiter")
    raw = _issue_token(str(user["id"]), "invite")
    _send_link_email(user["email"], user["full_name"], raw, "invite")
    return {"ok": True, "sent_to": user["email"]}


# ── Public: forgot password ───────────────────────────────────────────────────

class ForgotIn(BaseModel):
    email: str


@router.post("/forgot-password")
def forgot_password(body: ForgotIn):
    """
    Public. Always returns ok (don't reveal whether an email exists).
    Sends a reset link only if the email maps to an eligible active user.
    """
    user = query_one(
        "SELECT id, full_name, email, role, is_active FROM app_user WHERE email=%s",
        [body.email.lower().strip()],
    )
    if user and user["is_active"] and user["role"] in _SELF_SERVICE_ROLES:
        raw = _issue_token(str(user["id"]), "reset")
        try:
            _send_link_email(user["email"], user["full_name"], raw, "reset")
        except Exception as exc:
            print(f"[password] reset email failed for {user['email']}: {exc}")
    return {"ok": True, "message": "If that account exists, a reset link has been sent."}


# ── Public: validate token (for the set-password page) ────────────────────────

@router.get("/reset-token/validate")
def validate_token(token: str):
    row = query_one(
        "SELECT user_id, expires_at, used_at FROM password_reset_token WHERE token_hash=%s",
        [_hash_token(token)],
    )
    if not row or row["used_at"] is not None:
        return {"valid": False}
    if row["expires_at"].replace(tzinfo=None) < datetime.utcnow():
        return {"valid": False}
    return {"valid": True}


# ── Public: submit new password ───────────────────────────────────────────────

class ResetSubmitIn(BaseModel):
    token: str
    new_password: str


@router.post("/reset-password")
def reset_password(body: ResetSubmitIn):
    if len(body.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    row = query_one(
        """SELECT id, user_id, expires_at, used_at
           FROM password_reset_token WHERE token_hash=%s""",
        [_hash_token(body.token)],
    )
    if not row or row["used_at"] is not None:
        raise HTTPException(400, "Invalid or already-used link")
    if row["expires_at"].replace(tzinfo=None) < datetime.utcnow():
        raise HTTPException(400, "This link has expired — request a new one")
    query(
        "UPDATE app_user SET password_hash=%s WHERE id=%s",
        [hash_password(body.new_password), row["user_id"]],
        fetch=False,
    )
    query(
        "UPDATE password_reset_token SET used_at=now() WHERE id=%s",
        [row["id"]], fetch=False,
    )
    # Invalidate any other outstanding tokens for this user
    query(
        "UPDATE password_reset_token SET used_at=now() WHERE user_id=%s AND used_at IS NULL",
        [row["user_id"]], fetch=False,
    )
    return {"ok": True}
