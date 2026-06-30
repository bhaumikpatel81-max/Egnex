# `.env.prod` — keys these changes need

You already have `.env.prod`. Add / confirm these keys.

## SMTP (Step 1 + 2 + 4 — all email from hr@amnex.com)
```
SMTP_USER=hr@amnex.com
SMTP_PASSWORD=your_16_char_app_password      # spaces are stripped automatically
SMTP_HOST=smtp.gmail.com                      # or smtp.office365.com if not Workspace
SMTP_PORT=587
SMTP_FROM_NAME=Amnex Talent Acquisition
SENDGRID_API_KEY=                             # leave blank — SMTP-only now
APP_BASE_URL=https://your-prod-domain         # used in password + invite links — MUST be the real public URL
```

## Security (Step 1)
```
ENV=prod
JWT_SECRET=long_random_string                 # python -c "import secrets;print(secrets.token_urlsafe(48))"
```

## OpenAI bot brain (Step 3A)
Either reuse the existing GROQ_* var names:
```
GROQ_API_KEY=sk-your-openai-key
GROQ_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```
Or, if you applied the optional rename in Step 3A:
```
OPENAI_API_KEY=sk-your-openai-key
OPENAI_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

## Whisper STT (Step 3B)
```
OPENAI_API_KEY=sk-your-openai-key             # same key; required for /invite/transcribe
WHISPER_MODEL=whisper-1
```

## Notes
- `APP_BASE_URL` is critical for Steps 2 & 4 — password/invite links and any absolute URLs are built from it. If it's `http://localhost:8000`, emailed links won't work for real users.
- If `hr@amnex.com` is Office 365, set `SMTP_HOST=smtp.office365.com`, `SMTP_PORT=587`. App-password mechanics are the same.
- The app reads settings from the `system_settings` DB table FIRST, then env. If old SMTP/SendGrid values are saved in Settings, clear or overwrite them in the Settings screen so env/`hr@amnex.com` wins.
