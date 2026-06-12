"""
Tier-2 CV enrichment using Groq LLM.

Background asyncio task started at app startup.
Picks the oldest enrich_status='pending' row, calls Groq, updates the DB.
Rate cap: 20 req/min → sleep 3 s between calls (with backoff on 429).

Designed to be fully resumable: if the server restarts, rows still marked
'pending' will be picked up again on next startup.

Never crashes the app — all exceptions are caught and logged.
"""
import asyncio
import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

from ..db import query, query_one

# ── LLM config (same Groq credentials used by interviewer_llm.py) ─────────────

def _make_client():
    import openai
    return openai.AsyncOpenAI(
        api_key=os.environ.get("GROQ_API_KEY"),
        base_url=os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
    )


def _model() -> str:
    return os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")


# ── Prompt ────────────────────────────────────────────────────────────────────

_SYSTEM = """\
You are a resume parsing assistant. Extract structured data from the resume text below.
Return ONLY a valid JSON object — no markdown fences, no prose before or after.

Required fields:
{
  "skills": ["array", "of", "normalized", "lowercase", "technical", "skills"],
  "experience_years": <total years of professional experience as a number, or null>,
  "current_position": "<most recent job title, or null>",
  "location": "<city or state/country, or null>",
  "summary": "<one concise sentence describing the candidate's profile>"
}"""


# ── Enrichment loop ───────────────────────────────────────────────────────────

_SLEEP_BETWEEN  = 3.0   # 20 req/min cap
_BACKOFF_429    = [60, 120, 240]
_MAX_RETRIES    = 3
_IDLE_SLEEP     = 30.0  # sleep when no pending rows


def _strip_fences(s: str) -> str:
    s = re.sub(r'^```(?:json)?\s*', '', s.strip(), flags=re.IGNORECASE)
    s = re.sub(r'\s*```$', '', s.strip())
    return s.strip()


async def _enrich_one(row_id: str, raw_text: str) -> Optional[dict]:
    """Call Groq and return parsed result, or None on unrecoverable failure."""
    client = _make_client()
    text_truncated = raw_text[:6000]  # keep well under context window
    retries = 0
    backoff_idx = 0

    while retries < _MAX_RETRIES:
        try:
            resp = await client.chat.completions.create(
                model=_model(),
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": f"Resume text:\n\n{text_truncated}"},
                ],
                temperature=0,
                max_tokens=512,
            )
            raw = resp.choices[0].message.content or ""
            cleaned = _strip_fences(raw)
            data = json.loads(cleaned)
            # Normalise types
            skills = [str(s).lower() for s in (data.get("skills") or []) if s]
            exp = data.get("experience_years")
            try:
                exp = float(exp) if exp is not None else None
            except (TypeError, ValueError):
                exp = None
            return {
                "skills":           skills,
                "experience_years": exp,
                "current_position":  str(data.get("current_position") or "") or None,
                "location":         str(data.get("location") or "") or None,
                "ai_summary":       str(data.get("summary") or "") or None,
            }

        except Exception as exc:
            exc_str = str(exc)
            is_rate_limit = "429" in exc_str or "rate" in exc_str.lower()

            if is_rate_limit:
                sleep_sec = _BACKOFF_429[min(backoff_idx, len(_BACKOFF_429) - 1)]
                backoff_idx += 1
                print(f"[cv-enricher] 429 for {row_id} — backing off {sleep_sec}s")
                await asyncio.sleep(sleep_sec)
                # Do NOT count rate-limit waits as retries
                continue

            # Parse failure — retry once
            if "json" in exc_str.lower() and retries < _MAX_RETRIES - 1:
                retries += 1
                await asyncio.sleep(2)
                continue

            retries += 1
            print(f"[cv-enricher] error for {row_id} (attempt {retries}): {exc}")

    return None  # exhausted retries


async def start_enricher():
    """
    Infinite background loop — picks pending CV rows and enriches them.
    Safe to restart: picks up where it left off from DB state.
    """
    print("[cv-enricher] background enricher started")
    while True:
        try:
            row = await asyncio.to_thread(
                query_one,
                """SELECT id, raw_text FROM cv_repository
                   WHERE enrich_status = 'pending'
                     AND raw_text IS NOT NULL
                     AND raw_text != ''
                   ORDER BY created_at ASC
                   LIMIT 1""",
                [],
            )

            if not row:
                await asyncio.sleep(_IDLE_SLEEP)
                continue

            row_id = str(row["id"])
            result = await _enrich_one(row_id, row["raw_text"])

            if result is not None:
                await asyncio.to_thread(
                    query,
                    """UPDATE cv_repository
                       SET skills           = %s,
                           experience_years = %s,
                           current_position = %s,
                           location         = %s,
                           ai_summary       = %s,
                           enrich_status    = 'done',
                           enriched_at      = now()
                       WHERE id = %s""",
                    [
                        result["skills"],
                        result["experience_years"],
                        result["current_position"],
                        result["location"],
                        result["ai_summary"],
                        row_id,
                    ],
                    False,
                )
                print(f"[cv-enricher] enriched {row_id}")
            else:
                await asyncio.to_thread(
                    query,
                    "UPDATE cv_repository SET enrich_status='failed' WHERE id=%s",
                    [row_id],
                    False,
                )
                print(f"[cv-enricher] failed to enrich {row_id}")

            await asyncio.sleep(_SLEEP_BETWEEN)

        except asyncio.CancelledError:
            print("[cv-enricher] task cancelled, shutting down")
            return
        except Exception as exc:
            # Must never crash — log and keep running
            print(f"[cv-enricher] unexpected error: {exc}")
            await asyncio.sleep(10)
