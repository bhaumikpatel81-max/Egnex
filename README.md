# Egnex — One Click Hire (working prototype)

Egnex is an automated recruitment pipeline for Amnex. Applications arrive, get auto-screened, an assistive AI bot interviews shortlisted candidates and ranks them, the recruiter advances who they choose, interviews auto-schedule, and approved offers push to Darwinbox. Built to replace the 20+ tracking sheets with one source of truth.

This is a **working prototype**. The full pipeline runs end to end. The external integrations (Google Calendar/Meet, Gmail, the AI model, Darwinbox) are present as clearly marked stub functions in `backend/app/services/connectors.py` and `screening.py` — the only places that talk to the outside world. Your dev team replaces each stub body with the real API call; nothing else changes.

## What's inside

```
docker-compose.prod.yml   deployment definition (matches the hackathon pipeline)
ARCHITECTURE.md           plain-language design doc for your dev team
database/                 01_schema.sql, 02_seed.sql, 03_reports.sql (tested)
backend/                  FastAPI app: API + screening + pipeline + connectors
frontend/                 single-file dashboard to operate the pipeline
```

## Run it locally (the simplest way)

You need Docker installed. From the project root:

```
docker compose -f docker-compose.prod.yml --profile prod up --build
```

The database container loads the schema, seed, and reports automatically on first start. Then open the address shown for the backend in your browser. You'll see the dashboard: submit an application, watch it get scored, run the bot round, see the ranked chart, advance a candidate, and view every report.

## Run it without Docker (for development)

Start a PostgreSQL database and load the three SQL files in order (`01_schema.sql`, then `02_seed.sql`, then `03_reports.sql`). Then:

```
cd backend
pip install -r requirements.txt
export DB_HOST=localhost DB_PORT=5432 DB_NAME=oneclickhire DB_USER=postgres DB_PASSWORD=yourpassword
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Open http://localhost:8080.

## The pipeline, stage by stage

An application arrives at `/api/apply`. The system creates the candidate and auto-scores the resume against the requisition's key skills and experience (the screening engine). It never auto-rejects — low scorers sit in the queue for a recruiter to confirm. The assistive AI bot then interviews shortlisted candidates at `/api/applications/{id}/bot-round`, producing a score that blends with the screening score into one ranked chart. The recruiter views the chart, advances who they choose (the human gate), and the interview auto-schedules with a Google Meet link. Approved offers push to Darwinbox.

## Where the real integrations go

Every external call is isolated in two files so they're easy to find and replace:

`backend/app/services/connectors.py` holds `schedule_meeting` (Google Calendar + Meet), `send_email` (Gmail), `run_bot_interview` (the AI bot), and `push_offer_to_darwin` (Darwinbox). `backend/app/services/screening.py` holds `ai_screen`, the single place the screening model is called. `backend/app/services/notetaker.py` holds the recording, transcription, and summarisation providers (`_native_record`, `_thirdparty_record`, `_transcribe`, `_summarise`) — the native pipeline and the Fireflies/Read.ai fallback. Replace the body of each with the real API call. The function signatures stay the same, so the rest of the system keeps working unchanged.

---

# How to build this yourself — a guide for a non-technical owner

You asked how you can build this. Here's an honest, practical path. You do not need to become a programmer, but you do need to drive the project and supply the domain knowledge. Here's how the work splits and what each step involves.

## Your role versus the developer's role

Your job is product ownership: deciding how the pipeline should behave, supplying the real configuration (bands, business units, approval chains, email wording, scoring weights, interview-round structures), testing whether the system does what TA actually needs, and deciding when each piece is good enough to move on. The developer's job is wiring the real integrations, hardening the code, and deploying it. This prototype is the bridge — it gives the developer a working skeleton so they build *on* something rather than from a blank page.

## Step 1 — Get the prototype running and play with it

Before anything else, have your developer run this prototype using the Docker command above. Submit test applications, run the bot round, advance candidates, look at the reports. This is how you confirm the *behaviour* is right. Anything that feels wrong here is cheap to change now and expensive to change later. Write down every "it should actually do X" note.

## Step 2 — Replace the configuration with real Amnex data

The seed file already has your real bands, business units, and group companies. What it doesn't have yet is your real approval chains per band, your real email templates with the exact wording you use, and your real scoring weights. These all live as editable data. Sit with your developer and fill these in. This is pure domain knowledge — your work, not theirs.

## Step 3 — Wire the integrations, one at a time

Have the developer replace the stubs in priority order. Google Calendar and Meet first, because auto-scheduling kills the biggest pain point. Then Gmail for the candidate and panel emails. Then the AI screening model and the interview bot. Then Darwinbox last, since you only need it once a candidate reaches offer stage. Test each one in isolation before moving to the next. Replacing one stub at a time means if something breaks, you know exactly where.

## Step 4 — Pilot on one real requisition

Pick one live, low-risk role and run it entirely through the system alongside your current process. This tells you what's missing under real conditions without betting the whole function on it. Fix what the pilot surfaces.

## Step 5 — Roll out and add reporting cadence

Once the pilot is clean, widen to more requisitions and turn on the scheduled reports (daily/weekly/monthly). Keep the old sheets running in parallel for one cycle as a safety net, then retire them.

## A realistic note on effort and risk

The screening logic, the pipeline, the database, and the reports are the parts that are largely done here. The integrations are where the real engineering time goes — Google's and Darwinbox's APIs each have authentication, permissions, and error handling that take a developer real days, not hours. Budget for that. And the two things to be most careful about, because they carry legal and fairness risk rather than just technical risk: keep a human making every reject decision (never let the screening or the bot auto-reject), and get sign-off before turning on any proctoring or recording. The system is built to respect both of these, and it's worth protecting that as it grows.
