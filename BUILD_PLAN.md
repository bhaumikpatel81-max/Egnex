# Egnex — Build Plan (execute in order, one step at a time)

This is the master sequence for finishing Egnex. It exists because an earlier all-at-once instruction caused the backend to be built but the frontend redesign to be skipped. To prevent that, **each step below must be completed and visibly verified in the browser before starting the next.** Do not batch steps. At the end of each step, tell the user exactly what to run, what URL to open, and what they should see.

## Current state (verified)

- Backend: auth (JWT + bcrypt), login page, admin user CRUD, Google OAuth (per-recruiter) — all built and working.
- Backend: pipeline, screening (keyword + experience + AI stub), notetaker (consent-gated) — built.
- Frontend: STILL the original single-page prototype (`index.html`). The multi-screen design in `DESIGN_SPEC.md` was NOT built. This is the main gap.
- Resume intake: only accepts pasted text. No file upload, no parsing.

## Roles (three, each distinct — they must NOT see the same screen)

- **recruiter** — works requisitions and candidates; runs the pipeline; schedules interviews on their own linked Google Calendar.
- **ta_manager** — the admin: creates/manages user logins and roles, and sees analytics across all recruiters, BUs, and reports.
- **hiring_manager** — lighter role: reviews shortlisted profiles and gives panel/interview feedback; does not run the pipeline.

Add `hiring_manager` to the role options in the `app_user` table and seed at least one. After login, each role routes to a different home screen.

---

## STEP 1 — Add the hiring_manager role
Add `hiring_manager` to the allowed roles (DB check constraint + any backend enum) and seed one hiring manager user. Verify: the admin user-management screen can create a user with role hiring_manager.

## STEP 2 — Resume intake: file upload + parsing (PDF and Word first)
Change the apply flow to accept an uploaded file (PDF or .docx), not just pasted text. On upload: store the file (GCP path field already exists as `candidate.resume_url`), extract the text server-side (use a PDF text library and a docx library), and feed that extracted text into the existing `score_application`. Keep pasted-text as a fallback option.
- Image resumes (JPEG/PNG) via OCR: scaffold the hook but mark as a later sub-step.
- Naukri/LinkedIn import: do NOT build now — these need paid API/partnership access. Leave a clearly-marked `import_from_jobboard` stub so it can plug in later.
Verify: upload a real PDF resume in the browser, see the match score and breakdown appear.

## STEP 3 — The shared app shell (sidebar + top bar)
Build the layout shell from DESIGN_SPEC.md: dark left sidebar (~200px) with the Egnex logo and nav items, light working canvas, top bar with screen title + the logged-in user's name/role + Google Calendar status. Nav items shown depend on role:
- recruiter: Dashboard, Requisitions, Candidates, Interviews, Reports
- ta_manager: Dashboard, Requisitions, Candidates, Interviews, Reports, Team, Users, Analytics
- hiring_manager: Dashboard, Profiles to review, Interviews
Verify: after login, each role sees the shell with the correct nav items.

## STEP 4 — The dashboard (role-specific)
Build the dashboard from DESIGN_SPEC.md with the pipeline count cards in this exact order and naming: Open Requisitions, Applications Received, Under Screening, Screening Cleared, AI Interview, Panel Interview (single combined count here), Selected, Offer Stage, Joined (green). Emphasise Average Time to Hire with the orange accent. Below: requisitions list + gender-diversity bar. ta_manager dashboard adds a Recruiter Load panel (use `v_recruiter_load`). hiring_manager dashboard shows only profiles awaiting their review + their upcoming interviews.
Verify: each role's dashboard looks different and shows real counts from the database.

## STEP 5 — Requisitions list + New Requisition form
List screen per DESIGN_SPEC.md (status pills, table with Role/BU/Band/In pipeline/Levels). The New Requisition form lets the recruiter set the number of panel levels and name them (Level 1 Panel, Level 2 Panel, Level 3 Panel, Final Panel — customizable count), writing `round_config` rows.
Verify: create a requisition with 2 panel levels and one with 4; both save and show the right "Levels" value.

## STEP 6 — Requisition detail: the KANBAN board
The candidate kanban per DESIGN_SPEC.md. Columns reflect THIS requisition's configured levels dynamically (Applications → Screening → AI Interview → Level 1 … Final Panel → Selected → Offer → Joined). Cards are draggable; dragging advances the candidate (existing advance endpoint + stage_event log).
Verify: drag a candidate from one column to the next; status updates and persists on refresh.

## STEP 7 — Candidates, Interviews, Reports screens
Candidates: cross-requisition sortable table. Interviews: scheduled list + the existing Google Calendar connect/disconnect card. Reports: the 7 existing views as clean cards/charts, not raw JSON.
Verify: each screen loads real data.

## STEP 8 — Hiring Manager review flow
The hiring_manager's "Profiles to review" screen: shortlisted candidates awaiting their feedback, with a simple approve/comment action that feeds the panel decision. 
Verify: a hiring manager can log in, see a shortlisted profile, and submit feedback.

## Theme (applies to every screen)
Light theme, logo at `frontend/assets/egnex-logo.png`, fire-orange `#f15a22` as a sparing accent only (active nav, primary buttons, key metrics), dark sidebar `#0c0d10`, page `#faf9f6`, success green `#1d6e56`. Sentence case, generous whitespace, flat surfaces.

## Rule for every step
Build it, then STOP and tell the user: the command to run, the URL, and what to look for. Wait for them to confirm it works before the next step. If a step needs a backend API that doesn't exist, say so before inventing one.
