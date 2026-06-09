"""
LLM-driven interviewer brain (NexAI conversational mode).

Uses the openai Python client pointed at Groq's OpenAI-compatible API.
Two public async functions:
  next_turn(conversation_state)        -> {"reply": str, "is_complete": bool}
  score_transcript(conversation_state) -> {"raw_score": int, "score_detail": dict}

conversation_state schema:
  {
    "role_context": {"title": str, "key_skills": list, "job_description": str},
    "turns": [{"speaker": "bot"|"candidate", "text": str}, ...]
  }

Required env vars (backend .env only — never logged, never returned):
  GROQ_API_KEY   — your Groq API key (starts with gsk_)
  GROQ_BASE_URL  — defaults to https://api.groq.com/openai/v1
  LLM_MODEL      — defaults to llama-3.3-70b-versatile
"""
import json
import os
from typing import Optional

import openai

# ── Config (read at call time so env changes in tests are picked up) ──────────

def _model() -> str:
    return os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")

# Hard cap: force is_complete=True after this many bot turns regardless of sentinel
# Includes the hardcoded intro turn, so effective question count is _MAX_BOT_TURNS - 1
_MAX_BOT_TURNS = 15

# ── Prompts ───────────────────────────────────────────────────────────────────

_INTERVIEW_SYSTEM = """\
You are NexAI, a professional and warm AI screening interviewer.
Your job is to conduct a natural, spoken phone-screen for the role described below.

ROLE CONTEXT:
Title: {title}
Key Skills: {key_skills}
Job Description: {job_description}

RULES:
- The candidate has already received an introduction and confirmed they are ready. \
Do NOT introduce yourself or ask if the candidate is ready — begin directly with your first question.
- Ask exactly ONE question per turn. Never stack multiple questions in one reply.
- Listen carefully to what the candidate just said and open with a brief, varied \
acknowledgement before your next question -- as a human interviewer would.
- Cover the key skills organically across the conversation. Weave them in; do not \
ask about every skill back-to-back.
- Keep every reply short and spoken-friendly: no bullet points, no numbered lists, \
no markdown formatting -- this text will be read aloud by text-to-speech.
- Vary your acknowledgements. Do not open every turn with "Great!" or "Excellent!".
- After roughly 10 to 15 exchanges (not counting the opening introduction), thank the \
candidate warmly, let them know the team will review the responses, wish them well, and \
then append the exact token [INTERVIEW_COMPLETE] at the very end of your final message \
(no space before it, nothing after it).
- Never reveal you are an AI or mention that the interview is being scored.\
"""

_SCORE_SYSTEM = """\
You are an expert technical recruiter evaluating a screening interview transcript.
Return STRICT JSON only -- no prose before or after, no markdown fences.

ROLE: {title}
KEY SKILLS: {key_skills}
JOB DESCRIPTION: {job_description}

Return exactly this JSON structure (nothing else):
{{"raw_score": <integer 0-100>, "strengths": "<one concise paragraph>", \
"concerns": "<one concise paragraph>", "per_dimension": {{"relevance": <integer 0-10>, \
"depth": <integer 0-10>, "communication": <integer 0-10>, "fit": <integer 0-10>}}}}

Scoring dimensions:
  relevance     -- how closely the answers relate to the role and key skills
  depth         -- specificity and technical depth of the candidate's knowledge
  communication -- clarity, fluency, and conciseness in spoken answers
  fit           -- overall impression of culture and role fit

raw_score must be the per_dimension average scaled to 100, rounded to the nearest integer.\
"""

# ── Lazy Groq client (openai SDK, Groq base URL) ──────────────────────────────

_client: Optional[openai.AsyncOpenAI] = None


def _get_client() -> openai.AsyncOpenAI:
    global _client
    if _client is None:
        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. Add it to the backend .env file."
            )
        base_url = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
        _client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
    return _client


# ── Helpers ───────────────────────────────────────────────────────────────────

_SENTINEL = "[INTERVIEW_COMPLETE]"


def _build_messages(system_prompt: str, turns: list) -> list:
    """
    Build the OpenAI-format messages list from stored turns.
    A synthetic 'please begin' user message is always prepended so the model's
    first reply is the bot's opening question and role alternation stays valid.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "The candidate has confirmed they are ready. Please begin with your first interview question."},
    ]
    for turn in turns:
        role = "assistant" if turn["speaker"] == "bot" else "user"
        messages.append({"role": role, "content": turn["text"]})
    return messages


def _count_bot_turns(turns: list) -> int:
    return sum(1 for t in turns if t.get("speaker") == "bot")


# ── Public API ────────────────────────────────────────────────────────────────

async def next_turn(conversation_state: dict) -> dict:
    """
    Generate the bot's next spoken reply.

    Returns {"reply": str, "is_complete": bool}.
    is_complete becomes True when the model appends [INTERVIEW_COMPLETE] to its
    reply, or when the hard cap of 10 bot turns is reached.
    """
    role_ctx = conversation_state["role_context"]
    turns = conversation_state.get("turns", [])

    # Hard cap: if we've already reached the max, close regardless
    if _count_bot_turns(turns) >= _MAX_BOT_TURNS:
        return {
            "reply": (
                "Thank you so much for your time today. "
                "The team will carefully review your responses and be in touch soon. "
                "Have a wonderful day!"
            ),
            "is_complete": True,
        }

    system_prompt = _INTERVIEW_SYSTEM.format(
        title=role_ctx.get("title", "this role"),
        key_skills=", ".join(role_ctx.get("key_skills") or []) or "general professional skills",
        job_description=(role_ctx.get("job_description") or "")[:800],
    )

    messages = _build_messages(system_prompt, turns)

    response = await _get_client().chat.completions.create(
        model=_model(),
        max_tokens=400,
        messages=messages,
    )

    reply = (response.choices[0].message.content or "").strip()

    # Detect sentinel and strip it from the spoken reply
    if _SENTINEL in reply:
        reply = reply.replace(_SENTINEL, "").strip()
        return {"reply": reply, "is_complete": True}

    return {"reply": reply, "is_complete": False}


async def score_transcript(conversation_state: dict) -> dict:
    """
    Score the full interview conversation using the LLM.

    Returns {"raw_score": int 0-100, "score_detail": dict}.
    Falls back to rule-based scoring on any API or parse failure so a score
    is always produced.
    """
    role_ctx = conversation_state["role_context"]
    turns = conversation_state.get("turns", [])

    transcript_text = "\n".join(
        f"{t['speaker'].upper()}: {t['text']}" for t in turns
    )
    if not transcript_text.strip():
        return _rule_based_fallback(turns)

    try:
        system_prompt = _SCORE_SYSTEM.format(
            title=role_ctx.get("title", "this role"),
            key_skills=", ".join(role_ctx.get("key_skills") or []) or "general professional skills",
            job_description=(role_ctx.get("job_description") or "")[:800],
        )

        response = await _get_client().chat.completions.create(
            model=_model(),
            max_tokens=600,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"INTERVIEW TRANSCRIPT:\n\n{transcript_text}"
                        "\n\nEvaluate this candidate now. Return JSON only."
                    ),
                },
            ],
            response_format={"type": "json_object"},
        )

        raw_text = (response.choices[0].message.content or "").strip()

        # Defensive fence stripping (belt-and-suspenders)
        if raw_text.startswith("```"):
            parts = raw_text.split("```")
            raw_text = parts[1].lstrip("json").strip() if len(parts) > 1 else raw_text

        parsed = json.loads(raw_text)
        raw_score = max(0, min(100, int(round(float(parsed["raw_score"])))))
        per_dim = parsed.get("per_dimension", {})
        score_detail = {
            "strengths": parsed.get("strengths", ""),
            "concerns": parsed.get("concerns", ""),
            "per_dimension": {
                "relevance":     int(per_dim.get("relevance", 0)),
                "depth":         int(per_dim.get("depth", 0)),
                "communication": int(per_dim.get("communication", 0)),
                "fit":           int(per_dim.get("fit", 0)),
            },
            "scored_by": "llm",
        }
        return {"raw_score": raw_score, "score_detail": score_detail}

    except Exception as exc:
        print(f"[interviewer_llm] LLM scoring failed, falling back to rule-based: {exc}")
        return _rule_based_fallback(turns)


# ── Rule-based fallback ───────────────────────────────────────────────────────

def _rule_based_fallback(turns: list) -> dict:
    """Simple word-count heuristic used when LLM scoring is unavailable."""
    candidate_turns = [t for t in turns if t.get("speaker") == "candidate"]
    if not candidate_turns:
        return {
            "raw_score": 0,
            "score_detail": {
                "scored_by": "rule_based_fallback",
                "reason": "no_candidate_answers",
            },
        }

    total_words = sum(len(t["text"].split()) for t in candidate_turns)
    avg_words = total_words / len(candidate_turns)
    depth = min(avg_words / 60.0, 1.0)
    communication = 1.0 if avg_words >= 15 else (avg_words / 15.0)
    raw_score = min(round((depth * 0.6 + communication * 0.4) * 70, 1), 70.0)

    return {
        "raw_score": int(round(raw_score)),
        "score_detail": {
            "scored_by": "rule_based_fallback",
            "turns_answered": len(candidate_turns),
            "avg_words_per_turn": round(avg_words, 1),
        },
    }
