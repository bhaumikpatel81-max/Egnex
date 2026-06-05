from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import hash_password, require_admin, get_current_user

router = APIRouter(prefix="/api/admin", tags=["admin"])

_VALID_ROLES = {
    "admin", "ta_manager", "recruiter",
    "hiring_manager", "bu_head", "director", "interviewer",
}

_USER_COLS = "id, full_name, email, role, is_active, created_at"


class CreateUserIn(BaseModel):
    full_name: str
    email: str
    role: str
    password: str


class UpdateUserIn(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


class ResetPasswordIn(BaseModel):
    new_password: str


@router.get("/users")
def list_users(admin=Depends(require_admin)):
    return query(
        f"SELECT {_USER_COLS} FROM app_user ORDER BY created_at DESC"
    )


@router.post("/users", status_code=201)
def create_user(body: CreateUserIn, admin=Depends(require_admin)):
    if body.role not in _VALID_ROLES:
        raise HTTPException(400, f"Invalid role. Choose from: {sorted(_VALID_ROLES)}")
    if len(body.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if query_one("SELECT id FROM app_user WHERE email = %s", [body.email.lower()]):
        raise HTTPException(400, "A user with that email already exists")
    row = query_one(
        f"""INSERT INTO app_user (full_name, email, role, password_hash)
            VALUES (%s, %s, %s, %s) RETURNING {_USER_COLS}""",
        [body.full_name, body.email.lower(), body.role, hash_password(body.password)],
    )
    return row


@router.patch("/users/{user_id}")
def update_user(user_id: str, body: UpdateUserIn, admin=Depends(require_admin)):
    if body.role and body.role not in _VALID_ROLES:
        raise HTTPException(400, f"Invalid role. Choose from: {sorted(_VALID_ROLES)}")
    sets, params = [], []
    if body.full_name is not None:
        sets.append("full_name = %s"); params.append(body.full_name)
    if body.role is not None:
        sets.append("role = %s"); params.append(body.role)
    if body.is_active is not None:
        sets.append("is_active = %s"); params.append(body.is_active)
    if not sets:
        raise HTTPException(400, "Nothing to update")
    params.append(user_id)
    row = query_one(
        f"UPDATE app_user SET {', '.join(sets)} WHERE id = %s RETURNING {_USER_COLS}",
        params,
    )
    if not row:
        raise HTTPException(404, "User not found")
    return row


@router.delete("/users/{user_id}")
def deactivate_user(user_id: str, admin=Depends(require_admin)):
    row = query_one(
        "UPDATE app_user SET is_active = false WHERE id = %s RETURNING id", [user_id]
    )
    if not row:
        raise HTTPException(404, "User not found")
    return {"deactivated": True}


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: str, body: ResetPasswordIn, admin=Depends(require_admin)):
    if len(body.new_password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    row = query_one(
        "UPDATE app_user SET password_hash = %s WHERE id = %s RETURNING id",
        [hash_password(body.new_password), user_id],
    )
    if not row:
        raise HTTPException(404, "User not found")
    return {"ok": True}


# ── System Settings (admin / ta_manager only) ─────────────────────────────────

# Keys that hold sensitive values — shown masked in GET response
_SECRET_KEYS = {"smtp_password", "sendgrid_api_key"}

# All recognised setting keys with their defaults
_SETTING_DEFAULTS = {
    "sendgrid_api_key": "",
    "smtp_user":        "",
    "smtp_password":    "",
    "smtp_host":        "smtp.gmail.com",
    "smtp_port":        "587",
    "smtp_from_name":   "Egnex Hiring",
    "app_base_url":     "http://localhost:8000",
}


def _require_settings_access(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") not in ("admin", "ta_manager"):
        raise HTTPException(403, "Admin or TA Manager access required")
    return user


@router.get("/settings")
def get_settings(user: dict = Depends(_require_settings_access)):
    rows = query("SELECT key, value, updated_at FROM system_settings")
    stored = {r["key"]: r["value"] for r in (rows or [])}
    result = {}
    for k, default in _SETTING_DEFAULTS.items():
        val = stored.get(k, default)
        result[k] = "••••••••" if k in _SECRET_KEYS and val else val
    result["smtp_password_set"] = bool(stored.get("smtp_password", ""))
    return result


class SaveSettingsIn(BaseModel):
    sendgrid_api_key: Optional[str] = None
    smtp_user:        Optional[str] = None
    smtp_password:    Optional[str] = None
    smtp_host:        Optional[str] = None
    smtp_port:        Optional[str] = None
    smtp_from_name:   Optional[str] = None
    app_base_url:     Optional[str] = None


@router.post("/settings")
def save_settings(body: SaveSettingsIn, user: dict = Depends(_require_settings_access)):
    updates = {
        "smtp_user":      body.smtp_user,
        "smtp_host":      body.smtp_host,
        "smtp_port":      body.smtp_port,
        "smtp_from_name": body.smtp_from_name,
        "app_base_url":   body.app_base_url,
    }
    if body.smtp_password and body.smtp_password not in ("", "••••••••"):
        updates["smtp_password"] = body.smtp_password
    if body.sendgrid_api_key and body.sendgrid_api_key not in ("", "••••••••"):
        updates["sendgrid_api_key"] = body.sendgrid_api_key

    for k, v in updates.items():
        if v is None:
            continue
        query(
            """INSERT INTO system_settings (key, value, updated_by)
               VALUES (%s, %s, %s)
               ON CONFLICT (key) DO UPDATE
                 SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
            [k, v.strip(), user["sub"]],
            fetch=False,
        )
    return {"ok": True}


@router.post("/settings/test-email")
async def test_email(user: dict = Depends(_require_settings_access)):
    """
    Verify SMTP credentials and send a test email.
    Runs async with a hard 10-second timeout so the browser never hangs.
    """
    import asyncio, smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    # Read all settings directly from DB
    rows = query("SELECT key, value FROM system_settings")
    cfg  = {r["key"]: (r["value"] or "").strip() for r in (rows or [])}

    sg_key    = cfg.get("sendgrid_api_key", "")
    smtp_user = cfg.get("smtp_user", "")
    smtp_pass = cfg.get("smtp_password", "").replace(" ", "")
    smtp_host = cfg.get("smtp_host", "smtp.gmail.com") or "smtp.gmail.com"
    smtp_port = int(cfg.get("smtp_port", "587") or "587")
    from_name = cfg.get("smtp_from_name", "Egnex Hiring") or "Egnex Hiring"
    from_email = smtp_user or "noreply@egnex.io"

    if not sg_key and not smtp_user:
        raise HTTPException(400,
            "No email method configured. "
            "Add a SendGrid API key (recommended) or Gmail SMTP credentials."
        )

    subject   = "Egnex — Email configuration test"
    body_text = (
        "This test confirms your Egnex email is working. "
        "AI interview invites will be delivered automatically to candidates."
    )
    to_addr   = from_email

    # ── SendGrid (preferred — uses HTTPS, works on all networks) ─────────────
    if sg_key:
        import requests as _req
        payload = {
            "personalizations": [{"to": [{"email": to_addr}]}],
            "from": {"email": from_email, "name": from_name},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body_text}],
        }
        loop = asyncio.get_event_loop()
        try:
            resp = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: _req.post(
                        "https://api.sendgrid.com/v3/mail/send",
                        json=payload,
                        headers={"Authorization": f"Bearer {sg_key}"},
                        timeout=10,
                    )
                ),
                timeout=12,
            )
        except asyncio.TimeoutError:
            raise HTTPException(400, "SendGrid request timed out. Check your internet connection.")
        if resp.status_code in (200, 202):
            return {"ok": True, "sent_to": to_addr, "method": "SendGrid"}
        if resp.status_code == 401:
            raise HTTPException(400,
                "SendGrid API key rejected (401). "
                "Generate a new key at app.sendgrid.com → Settings → API Keys."
            )
        if resp.status_code == 403:
            raise HTTPException(400,
                "SendGrid key doesn't have Mail Send permission (403). "
                "Create a new key with 'Mail Send' access enabled."
            )
        raise HTTPException(400, f"SendGrid error {resp.status_code}: {resp.text[:300]}")

    # ── SMTP fallback ─────────────────────────────────────────────────────────
    if not smtp_pass:
        raise HTTPException(400, "SMTP password is empty — enter your App Password and save.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{from_name} <{smtp_user}>"
    msg["To"]      = smtp_user
    msg.attach(MIMEText(body_text, "plain", "utf-8"))

    def _do_smtp():
        errs = []
        for use_ssl, port in [(False, smtp_port), (True, 465)]:
            try:
                if use_ssl:
                    conn = smtplib.SMTP_SSL(smtp_host, port, timeout=5)
                else:
                    conn = smtplib.SMTP(smtp_host, port, timeout=5)
                with conn as s:
                    s.ehlo()
                    if not use_ssl:
                        s.starttls(); s.ehlo()
                    s.login(smtp_user, smtp_pass)
                    s.sendmail(smtp_user, [smtp_user], msg.as_string())
                return ("ok", f"SMTP {'SSL' if use_ssl else 'TLS'} :{port}")
            except smtplib.SMTPAuthenticationError as e:
                return ("auth", str(e))
            except Exception as e:
                errs.append(f"port {port}: {e}")
        return ("fail", " | ".join(errs))

    loop = asyncio.get_event_loop()
    try:
        status, detail = await asyncio.wait_for(
            loop.run_in_executor(None, _do_smtp), timeout=15
        )
    except asyncio.TimeoutError:
        raise HTTPException(400,
            "SMTP timed out — your network is blocking outbound email ports. "
            "Use SendGrid instead (works via HTTPS on all networks)."
        )

    if status == "ok":
        return {"ok": True, "sent_to": smtp_user, "method": detail}
    if status == "auth":
        raise HTTPException(400,
            "Gmail rejected the App Password.\n"
            "1. Go to myaccount.google.com → Security\n"
            "2. Confirm 2-Step Verification is ON\n"
            "3. Search 'App passwords' → create one named Egnex\n"
            "4. Copy the 16 chars → paste into App password field → Save"
        )
    raise HTTPException(400,
        f"Cannot connect via SMTP ({detail}). "
        "Your network blocks SMTP. Use SendGrid instead."
    )
