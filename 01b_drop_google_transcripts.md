# Step 1b (OPTIONAL) — Drop the Google Meet transcript feature entirely

Do this ONLY if you chose option (A) fully-Google-free. Skip if you chose (B).

The bot interview's own transcript+score email to the recruiter is **separate** and is NOT removed by this step — that keeps working.

## Backend — `backend/app/main.py`

### FIND:
```python
from .routers.google_oauth import router as _google_oauth_router
```
DELETE that line.

### FIND:
```python
from .routers.transcript_api import router as _transcript_router
```
DELETE that line.

### FIND:
```python
app.include_router(_google_oauth_router)
```
DELETE that line.

### FIND:
```python
app.include_router(_transcript_router)
```
DELETE that line.

> The `interview_notes` migration in the startup block can stay (idempotent, harmless) or be removed — your call. Leaving it is safer.

## Delete these files
- `backend/app/routers/google_oauth.py`
- `backend/app/services/transcript_service.py`
- `backend/app/routers/transcript_api.py`
- `backend/app/services/notetaker.py` (already a deprecated stub)

## Frontend — `frontend/index.html`
Remove the Google/Drive UI so nothing 404s:
- The `#gcal-indicator` element (~line 399) and its CSS (~lines 36–38, 228).
- The "Google Calendar & Drive" card in the Interviews screen (~lines 2849–2858).
- The Drive banner (~lines 2782–2789).
- Functions: `refreshGCal` (~1237), `connectGCal` (~2865), `disconnectGCal` (~2866), `fetchTranscript` (~2870), the transcript modal renderer (~2900–2970), and the `recruiter_google_token`/`/api/google/...` fetches.
- The query-param handlers at ~8606–8607 (`connected=1`, `gcal_error`).
- The `refreshGCal().catch(()=>{})` call at ~9224.
- "Transcript" column header + cell in the interviews table (~2861).

> Easiest method for Claude Code: search the file for `gcal`, `Google`, `transcript`, `/api/google`, `connectGCal`, `fetchTranscript` and remove each matched block, then load the page and fix any JS console errors.

## requirements.txt
Remove (only if dropping Google):
```
google-auth==2.29.0
google-auth-oauthlib==1.2.0
google-api-python-client==2.127.0
google-cloud-storage>=2.16.0
```
KEEP `gtts` — it is a TTS fallback, not Google auth.

> If you keep the GCS-based avatar prerender (`prerender.py` / `avatar.py` import `google.cloud.storage`), keep `google-cloud-storage`. Check whether you use the GPU avatar path before removing it.

## VERIFY
App starts with no `ImportError`. Interviews screen renders with no Google card and no console errors.
