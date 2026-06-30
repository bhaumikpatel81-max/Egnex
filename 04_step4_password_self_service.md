# Step 4 — Self-service password: create (first-time) + forgot/reset, emailed from hr@amnex.com

Flow:
- Username = the user's official email (e.g. `recruiter@amnex.com`).
- When an admin creates a recruiter/TA-manager (or for an existing user), the system can send a "Set your password" email from hr@amnex.com containing a one-time link.
- "Forgot password" on the login page emails a reset link from hr@amnex.com.
- The link opens a Set-Password page; submitting it hashes + stores the password and consumes the token.

Tokens are single-use, time-limited, stored hashed in the DB.

## 4A. Migration — add a password-reset token table

Add to the `migrations` list in `backend/app/main.py` `_auto_migrate()` (append at the end, before the closing `]`):

```python
        # ── Password reset / first-time set-password tokens ─────────────
        """CREATE TABLE IF NOT EXISTS password_reset_token (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
            token_hash  TEXT NOT NULL UNIQUE,
            purpose     TEXT NOT NULL DEFAULT 'reset'
                        CHECK (purpose IN ('reset','invite')),
            expires_at  TIMESTAMPTZ NOT NULL,
            used_at     TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_prt_user ON password_reset_token(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_prt_hash ON password_reset_token(token_hash)",
```

## 4B. New router file: `backend/app/routers/password_api.py`

```python
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
```

## 4C. Register the router + make endpoints public — `backend/app/main.py`

### Add import near the other router imports:
```python
from .routers.password_api import router as _password_router
```
### Add include near the other includes:
```python
app.include_router(_password_router)
```

### Make the public ones bypass JWT. FIND the `_PUBLIC` set and add:
```python
_PUBLIC = {
    "/", "/login", "/api/health", "/api/auth/login",
    "/set-password",
    "/api/auth/forgot-password",
    "/api/auth/reset-password",
    "/api/auth/reset-token/validate",
    "/nexai-interview",
    "/api/nexai/invite/validate",
    "/api/nexai/invite/begin",
}
```
(`/api/auth/send-setup-link` stays admin-protected — do NOT add it.)

### Serve the set-password page. In the `if os.path.isdir(_FRONTEND_DIR):` block, add:
```python
    @app.get("/set-password", response_class=HTMLResponse)
    def set_password_page():
        with open(os.path.join(_FRONTEND_DIR, "set-password.html"), encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)
```

## 4D. Auto-invite on user creation (optional but matches your ask)
In `backend/app/routers/admin_users.py`, the `create_user` endpoint currently requires a password. To support "create user → they get an email to set their own password," either:

**Option 1 (recommended):** make password optional and auto-send the invite.
### FIND:
```python
class CreateUserIn(BaseModel):
    full_name: str
    email: str
    role: str
    password: str
```
### REPLACE WITH:
```python
class CreateUserIn(BaseModel):
    full_name: str
    email: str
    role: str
    password: Optional[str] = None       # if omitted, user sets it via emailed link
    send_setup_email: bool = True
```
### FIND the body of `create_user` after the duplicate-email check and REPLACE the INSERT + return with:
```python
    pwd_hash = hash_password(body.password) if body.password else None
    row = query_one(
        f"""INSERT INTO app_user (full_name, email, role, password_hash)
            VALUES (%s, %s, %s, %s) RETURNING {_USER_COLS}""",
        [body.full_name, body.email.lower(), body.role, pwd_hash],
    )
    # Auto-send first-time set-password email for self-service roles
    if body.send_setup_email and body.role in {"admin", "ta_manager", "recruiter"}:
        try:
            from .password_api import _issue_token, _send_link_email
            raw = _issue_token(str(row["id"]), "invite")
            _send_link_email(row["email"], row["full_name"], raw, "invite")
        except Exception as exc:
            print(f"[create_user] setup email failed: {exc}")
    return row
```
Also remove the hard `len(body.password) < 6` check, or guard it with `if body.password and len(body.password) < 6:`.

## 4E. Frontend — set-password page + "Forgot password?" link

### New file: `frontend/set-password.html`
A minimal standalone page (match login.html's style). It reads `?token=` from the URL, calls `GET /api/auth/reset-token/validate`, and if valid shows two password fields → `POST /api/auth/reset-password` → on success redirect to `/login`. Keep it dependency-free (vanilla JS, same look as login.html). Hand login.html to Claude Code as the style reference.

### `frontend/login.html` — add a "Forgot password?" link
Below the login button, add a link that prompts for email and calls `POST /api/auth/forgot-password`, then shows "If that account exists, a reset link has been sent." (Always show that message regardless of response, to avoid leaking which emails exist.)

### Settings / Users screen — add a "Send setup link" button (optional)
Next to each user, an admin button calling `POST /api/auth/send-setup-link {email}`.

## VERIFY Step 4
1. Admin creates a recruiter without a password → recruiter receives "Set your Egnex password" email **from hr@amnex.com** with a link.
2. Open the link → set-password page validates the token → set password → redirected to login → log in works.
3. On login page, "Forgot password?" → enter email → reset email arrives → reset works → old token no longer usable.
4. Reusing a consumed link shows "Invalid or already-used link."
