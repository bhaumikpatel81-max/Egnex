# Step 1 — Force SMTP-only, lock From to hr@amnex.com, harden JWT

## 1A. `backend/app/services/connectors.py` — make `send_email` SMTP-only

The function currently tries SendGrid first. We make SMTP the only path. From address is already `cfg["user"]` (= `hr@amnex.com` once your .env.prod sets `SMTP_USER=hr@amnex.com`), so no From change needed.

### FIND (the whole SendGrid branch inside `send_email`):
```python
    cfg = _load_email_cfg()

    # ── 1. SendGrid ──────────────────────────────────────────────────────────
    if cfg["sendgrid_api_key"]:
        from_email = cfg["user"] or "noreply@egnex.io"
        _send_via_sendgrid(
            cfg["sendgrid_api_key"], from_email, cfg["from_name"],
            to_email, subject, body, html, reply_to=reply_to,
        )
        return {"sent": True, "to": to_email, "via": "sendgrid"}

    # ── 2. SMTP ───────────────────────────────────────────────────────────────
    if cfg["user"] and cfg["password"]:
```

### REPLACE WITH:
```python
    cfg = _load_email_cfg()

    # ── SMTP only (all mail sent from SMTP_USER, i.e. hr@amnex.com) ───────────
    if cfg["user"] and cfg["password"]:
```

> Leave the rest of the SMTP block and the stub fallback exactly as-is.
> You can optionally delete `_send_via_sendgrid` and the `sendgrid_api_key` line in `_load_email_cfg`, but it's harmless to leave them unused. Cleanest: remove them.

## 1B. `backend/app/routers/admin_users.py` — Settings test-email: SMTP only

### FIND the SendGrid block in the test-email endpoint (around line 295–314):
```python
        if resp.status_code in (200, 202):
            return {"ok": True, "sent_to": to_addr, "method": "SendGrid"}
        ...
        raise HTTPException(400, f"SendGrid error {resp.status_code}: {resp.text[:300]}")
```
Delete the entire SendGrid attempt (the `if sendgrid_key:` block that precedes the `# ── SMTP fallback ──` comment). Keep everything from `# ── SMTP fallback ──` onward — that's now the only path. Also remove the now-unused SendGrid imports/variables in that function if any.

## 1C. Lock the From address to hr@amnex.com (defensive)

In `connectors.py` inside `send_email`, the SMTP `From` line is:
```python
        msg["From"]    = f"{cfg['from_name']} <{cfg['user']}>"
```
This already uses `SMTP_USER`. As long as `.env.prod` has `SMTP_USER=hr@amnex.com`, every email is from hr@amnex.com. No change needed — just confirm your `.env.prod`.

> NOTE on Gmail/Workspace: an app password authenticates as the mailbox it belongs to. If the app password is for `hr@amnex.com`, Gmail will only let you send *as* `hr@amnex.com` (or verified aliases). So the From is enforced by Google too.

## 1D. Harden the JWT secret — `backend/app/auth_utils.py`

Today it falls back to a hardcoded dev secret. In prod every deploy would share the same signing key. Require the env var.

### FIND:
```python
SECRET_KEY = os.environ.get("JWT_SECRET", "egnex-dev-secret-change-in-prod")
```

### REPLACE WITH:
```python
SECRET_KEY = os.environ.get("JWT_SECRET", "").strip()
if not SECRET_KEY:
    # Allow a dev default ONLY when not in production.
    if os.environ.get("ENV", "").lower() in ("prod", "production"):
        raise RuntimeError(
            "JWT_SECRET is not set. Add a long random JWT_SECRET to .env.prod "
            "before starting in production."
        )
    SECRET_KEY = "egnex-dev-secret-change-in-prod"
```

Make sure `.env.prod` contains `ENV=prod` and a real `JWT_SECRET` (generate with `python -c "import secrets;print(secrets.token_urlsafe(48))"`).

## VERIFY Step 1
1. Rebuild + start. Logs should show no SendGrid references.
2. Settings → Send test email → confirm it arrives **from hr@amnex.com**.
3. Trigger a NexAI invite → candidate email arrives; logs show `[email] SMTP sent TO: ...`.
4. Log in works (JWT signing still fine with the env secret).
