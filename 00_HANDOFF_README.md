# Egnex — Step-by-Step Change Handoff

Four changes, in safe order. Each step is self-contained: make the change, run, verify, then move on. Don't start a step until the previous one runs clean.

Files referenced below live alongside this README. Each contains a **FIND** (existing code) and **REPLACE WITH** (new code), or a full new file.

---

## What's realistic vs. not (read first)

| Your ask | Verdict | Notes |
|---|---|---|
| All email from `hr@amnex.com` via SMTP | ✅ Easy | SMTP already works; just force it on + lock From. |
| Auto **calendar invite** with a link | ✅ via ICS | hr@amnex.com sends a real `.ics` invite. The "link" is a **static meeting URL you paste** (Jitsi/Teams/Zoom/Meet) — ICS cannot auto-create a video room. |
| Auto **transcript of the panel interview** + report to recruiter | ⚠️ Partial | A live human-panel call can only be auto-transcribed via the meeting vendor (Google Meet → Drive, which is the Google dependency you're removing). **Realistic path:** keep auto-transcript+report for the **NexAI bot interview** (already built and emailed to the recruiter), and for panel rounds either keep Google Meet transcripts OR add a manual "upload recording → Whisper" button (Step 3 covers the Whisper engine you'd reuse). |
| OpenAI for the bot brain | ✅ Trivial | Bot already uses the OpenAI SDK pointed at Groq. Change 2 env vars. |
| OpenAI Whisper for candidate speech | ✅ New code | Today the browser transcribes for free. Whisper = server-side, better accuracy. Step 3 adds it. |
| Self-service password create/reset via email | ✅ New feature | Step 4: token table + 3 endpoints + 2 emails + 1 page. |

---

## Order of work

- **Step 1** — Force SMTP-only, lock From to hr@amnex.com, harden JWT secret. (Lowest risk, do first.)
- **Step 2** — Replace Google Calendar scheduling with ICS-over-SMTP invites.
- **Step 3** — Point the bot at OpenAI (env only) + add Whisper transcription endpoint.
- **Step 4** — Self-service password create / forgot / reset emails from hr@amnex.com.

After every step: `docker compose -f docker-compose.prod.yml --profile prod up --build`, hit `/api/health`, then the step's specific test.

---

## Decision you must make before Step 2/3

**The Meet-transcript feature (Google Drive).** Pick one:

- **(A) Drop it** — fully Google-free. The bot interview still emails its own transcript+score to the recruiter. Panel rounds get no auto-transcript (recruiters write scorecards manually, as the app already supports).
- **(B) Keep it** — leave `google_oauth.py` + `transcript_service.py` + `transcript_api.py` untouched; recruiters do a one-click Google connect just for pulling Meet transcripts. Email/scheduling/bot are unaffected.

Steps 1–4 work identically either way. If (A), also do `01b_drop_google_transcripts.md`.
