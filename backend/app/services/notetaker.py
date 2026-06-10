"""
DEPRECATED — Meeting Notetaker (old bot/recording approach).

This module previously provided a recording-consent + bot-join based notetaker.
It has been replaced by transcript_service.py which reads Google Meet's own
auto-generated transcripts from the meeting organizer's Google Drive.

Old endpoints (now REMOVED from main.py):
  POST /api/consent/request   → notetaker.request_consent()
  POST /api/consent/respond   → notetaker.record_consent_response()
  POST /api/interviews/notes  → notetaker.process_interview()

The tables they referenced (meeting_recording, meeting_notes, meeting_transcript,
recording_consent) were never created in production schema — no data is lost.

New approach (transcript_service.py):
  POST /api/interviews/{id}/fetch-transcript  — trigger Drive fetch + Groq summarize
  GET  /api/interviews/{id}/transcript        — poll status / read summary + transcript

No action required — this file is kept as a historical note only.
"""
