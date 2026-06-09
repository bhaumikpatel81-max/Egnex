"""
Screening engine.

Scores a candidate against a requisition using four dimensions:

  Keyword match   — rule-based skill/JD overlap
  Experience      — years vs. minimum required
  AI holistic fit — Groq LLM reads resume + JD and returns 0-100
  Stability       — average role tenure (experienced candidates only)

WEIGHTS (editable constants below):
  Experienced candidates: keyword 0.30 + experience 0.20 + AI 0.40 + stability 0.10
  Freshers / low-history: stability excluded; remaining three weights renormalised
                          to sum to 1.0 so score still spans 0-100.

STABILITY:
  Parsed from resume date ranges. If not parseable: status = 'pending_manual'
  and recruiter can enter it via the manual-tenure endpoint. Until provided,
  score uses renormalised fresher weights and is flagged 'stability pending'.

AI FALLBACK:
  On any Groq failure (rate-limit / network / parse error) the AI component
  falls back to neutral midpoint 50 so a resume upload is never blocked.
"""
import json
import os
import re
from datetime import date
from typing import Optional

import openai

# ── Editable scoring constants ────────────────────────────────────────────────

# Experienced threshold — EITHER condition marks a candidate as "experienced"
EXPERIENCED_MIN_YEARS  = 4.0   # total years of experience stated by candidate
EXPERIENCED_MIN_ROLES  = 2     # OR distinct roles found in resume work history

# Weights for experienced candidates (must sum to 1.0)
SCORE_WEIGHT_KEYWORD    = 0.30
SCORE_WEIGHT_EXPERIENCE = 0.20
SCORE_WEIGHT_AI         = 0.40
SCORE_WEIGHT_STABILITY  = 0.10

# Stability curve — average role tenure in months
STABILITY_FULL_MONTHS = 24   # avg >= 24 months → stability_score = 100
STABILITY_ZERO_MONTHS = 3    # avg <=  3 months → stability_score = 0


# ── Groq sync client ──────────────────────────────────────────────────────────

_sync_client: Optional[openai.OpenAI] = None


def _get_client() -> openai.OpenAI:
    global _sync_client
    if _sync_client is None:
        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is not set — add it to .env.prod")
        base_url = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
        _sync_client = openai.OpenAI(api_key=api_key, base_url=base_url)
    return _sync_client


def _llm_model() -> str:
    return os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")


# ── AI screen prompt ──────────────────────────────────────────────────────────

_AI_SCREEN_SYSTEM = """\
You are an expert recruiter evaluating a candidate resume against a job opening.
Return STRICT JSON only -- no prose before or after, no markdown fences.

Return exactly this JSON structure (nothing else):
{"ai_fit_score":<integer 0-100>,"strengths":"<one concise paragraph>",\
"concerns":"<one concise paragraph>","rationale":"<one concise paragraph>"}

Scoring guide: 0-30 poor fit, 31-50 below average, 51-70 average,\
 71-85 good fit, 86-100 excellent fit.
Base ai_fit_score on: relevance of skills to role requirements, quality and\
 depth of experience, seniority alignment with band, overall career trajectory.\
"""

# ── Tenure extraction ─────────────────────────────────────────────────────────

_MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}

_MON = (
    r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
)
_YR      = r'(\d{4})'
_SEP     = r'(?:\s*[-–—/|]\s*|\s+to\s+)'
_PRESENT = r'(present|current|now|till\s+date|today|to\s+date)'

# "Mon YYYY SEP (Mon YYYY | present_token)"
_TENURE_RE = re.compile(
    _MON + r'\s+' + _YR + _SEP
    + r'(?:' + _PRESENT + r'|' + _MON + r'\s+' + _YR + r')',
    re.IGNORECASE,
)


def _parse_avg_tenure(resume_text: str) -> tuple[Optional[float], dict]:
    """
    Best-effort extraction of average role tenure from resume text.
    Returns (avg_months_or_None, debug_dict).
    None is returned whenever fewer than one reliable date range is found —
    the caller treats this as 'pending_manual'.
    """
    today = date.today()
    durations: list[int] = []

    for m in _TENURE_RE.finditer(resume_text or ""):
        s_mon    = m.group(1)
        s_yr     = int(m.group(2))
        present  = m.group(3)   # "present" / "current" / etc., or None
        e_mon    = m.group(4)   # end month name when not "present"
        e_yr_str = m.group(5)   # end year when not "present"

        s_m = _MONTHS.get(s_mon[:3].lower(), 1)

        if present:
            e_m, e_yr = today.month, today.year
        elif e_mon and e_yr_str:
            e_m  = _MONTHS.get(e_mon[:3].lower(), 1)
            e_yr = int(e_yr_str)
        else:
            continue

        months = (e_yr - s_yr) * 12 + (e_m - s_m)
        if 1 <= months <= 480:   # sanity gate: 1 month – 40 years
            durations.append(months)

    if not durations:
        return None, {"tenure_parse": "no_date_ranges_found"}

    avg = sum(durations) / len(durations)
    return avg, {
        "tenure_parse":   "computed",
        "roles_parsed":   len(durations),
        "tenures_months": durations,
    }


# ── Stability score ───────────────────────────────────────────────────────────

def compute_stability_score(avg_tenure_months: float) -> float:
    """
    Linear curve from STABILITY_ZERO_MONTHS (→0) to STABILITY_FULL_MONTHS (→100).
    Public so pipeline.py can call it when a recruiter submits manual tenure.
    """
    if avg_tenure_months >= STABILITY_FULL_MONTHS:
        return 100.0
    if avg_tenure_months <= STABILITY_ZERO_MONTHS:
        return 0.0
    span = max(1.0, STABILITY_FULL_MONTHS - STABILITY_ZERO_MONTHS)
    return 100.0 * (avg_tenure_months - STABILITY_ZERO_MONTHS) / span


# ── Keyword match (unchanged from original) ───────────────────────────────────

def keyword_match_score(resume_text: str, key_skills: list[str]) -> tuple[float, dict]:
    """Fraction of required skills found in the resume text, 0-100."""
    if not key_skills:
        return 50.0, {"matched_skills": [], "note": "no key skills defined"}
    resume_lc = (resume_text or "").lower()
    matched = [s for s in key_skills if s.lower() in resume_lc]
    score = 100.0 * len(matched) / len(key_skills)
    return score, {
        "matched_skills":       matched,
        "missing_skills":       [s for s in key_skills if s not in matched],
        "skills_matched_count": len(matched),
        "skills_total":         len(key_skills),
    }


# ── Experience score (unchanged from original) ────────────────────────────────

def experience_score(years: Optional[float], min_required: Optional[float]) -> tuple[float, dict]:
    """100 if meets/exceeds requirement, partial credit below."""
    if min_required is None or years is None:
        return 50.0, {"experience_note": "experience not evaluated"}
    years        = float(years)
    min_required = float(min_required)
    if years >= min_required:
        return 100.0, {"experience_met": True, "years": years, "required": min_required}
    ratio = max(0.0, years / min_required) if min_required else 0.0
    return 100.0 * ratio, {"experience_met": False, "years": years, "required": min_required}


# ── AI holistic screen (Groq) ─────────────────────────────────────────────────

def ai_screen(
    resume_text: str,
    job_description: str,
    key_skills: Optional[list[str]] = None,
    title: str = "",
    band_code: str = "",
) -> tuple[float, dict]:
    """
    Groq LLM holistic resume-vs-JD evaluation.
    Returns (score_0-100, detail_dict).
    Falls back to neutral midpoint 50 on ANY failure — upload is never blocked.
    """
    try:
        skills_str   = ", ".join(key_skills or []) or "not specified"
        user_content = (
            f"ROLE: {title or 'not specified'}\n"
            f"BAND/SENIORITY: {band_code or 'not specified'}\n"
            f"KEY SKILLS: {skills_str}\n"
            f"JOB DESCRIPTION:\n{(job_description or '')[:1200]}\n\n"
            f"RESUME:\n{(resume_text or '')[:3000]}\n\n"
            "Evaluate this candidate. Return JSON only."
        )
        response = _get_client().chat.completions.create(
            model=_llm_model(),
            max_tokens=500,
            messages=[
                {"role": "system", "content": _AI_SCREEN_SYSTEM},
                {"role": "user",   "content": user_content},
            ],
            response_format={"type": "json_object"},
        )
        raw = (response.choices[0].message.content or "").strip()
        # Belt-and-suspenders fence strip
        if raw.startswith("```"):
            parts = raw.split("```")
            raw   = parts[1].lstrip("json").strip() if len(parts) > 1 else raw
        parsed = json.loads(raw)
        score  = max(0, min(100, int(round(float(parsed["ai_fit_score"])))))
        return float(score), {
            "ai_fit_score": score,
            "strengths":    parsed.get("strengths", ""),
            "concerns":     parsed.get("concerns",  ""),
            "rationale":    parsed.get("rationale", ""),
            "scored_by":    "groq",
        }
    except Exception as exc:
        print(f"[screening] Groq AI screen failed — neutral fallback (50): {exc}")
        return 50.0, {
            "ai_fit_score":    50,
            "strengths":       "",
            "concerns":        "",
            "rationale":       "",
            "scored_by":       "fallback",
            "fallback_reason": str(exc)[:200],
        }


# ── Combined scorer ───────────────────────────────────────────────────────────

def score_application(
    resume_text: str,
    candidate_years: Optional[float],
    requisition: dict,
) -> tuple[float, dict]:
    """
    Blend keyword, experience, AI-fit, and (for experienced) stability into
    one 0-100 match score.

    Returns (final_score, breakdown_dict).

    breakdown_dict includes all sub-scores, weights used, AI reasoning,
    stability status, and tenure parse details — ready to store in score_breakdown
    JSONB and to display in the 'Why this score?' recruiter UI.
    """
    key_skills = requisition.get("key_skills") or []
    jd         = requisition.get("job_description") or ""
    min_exp    = requisition.get("min_experience")
    title      = requisition.get("title", "")
    band_code  = requisition.get("band_code", "")

    skills_s, skills_b = keyword_match_score(resume_text, key_skills)
    exp_s,    exp_b    = experience_score(candidate_years, min_exp)
    ai_s,     ai_b     = ai_screen(resume_text, jd, key_skills, title, band_code)

    avg_months, tenure_b = _parse_avg_tenure(resume_text)
    roles_parsed = tenure_b.get("roles_parsed", 0)

    # Experienced check: EITHER years threshold OR number of distinct roles
    is_experienced = (
        (candidate_years is not None and float(candidate_years) >= EXPERIENCED_MIN_YEARS)
        or roles_parsed >= EXPERIENCED_MIN_ROLES
    )

    # Stability dimension
    stability_s:      Optional[float]
    stability_status: str

    if not is_experienced:
        stability_s      = None
        stability_status = "not_applicable"
    elif avg_months is not None:
        stability_s      = compute_stability_score(avg_months)
        stability_status = "computed"
    else:
        stability_s      = None
        stability_status = "pending_manual"

    # Weight selection and final score
    if is_experienced and stability_status == "computed" and stability_s is not None:
        w_kw  = SCORE_WEIGHT_KEYWORD
        w_exp = SCORE_WEIGHT_EXPERIENCE
        w_ai  = SCORE_WEIGHT_AI
        w_st  = SCORE_WEIGHT_STABILITY
        final = (skills_s * w_kw + exp_s * w_exp
                 + float(ai_s) * w_ai + stability_s * w_st)
        weights_used = {
            "keyword": w_kw, "experience": w_exp, "ai": w_ai, "stability": w_st,
        }
    else:
        # Renormalise: stability excluded from weight pool
        base  = SCORE_WEIGHT_KEYWORD + SCORE_WEIGHT_EXPERIENCE + SCORE_WEIGHT_AI
        w_kw  = SCORE_WEIGHT_KEYWORD    / base
        w_exp = SCORE_WEIGHT_EXPERIENCE / base
        w_ai  = SCORE_WEIGHT_AI         / base
        final = skills_s * w_kw + exp_s * w_exp + float(ai_s) * w_ai
        weights_used = {
            "keyword":    round(w_kw,  4),
            "experience": round(w_exp, 4),
            "ai":         round(w_ai,  4),
            "stability":  0,
        }

    breakdown = {
        # Dimension sub-scores
        "skills_score":     round(skills_s, 1),
        "experience_score": round(exp_s, 1),
        "ai_score":         round(float(ai_s), 1),
        "stability_score":  round(stability_s, 1) if stability_s is not None else None,
        # Weights & flags
        "weights":          weights_used,
        "is_experienced":   is_experienced,
        "stability_status": stability_status,
        # Tenure
        "avg_tenure_months": round(avg_months, 1) if avg_months is not None else None,
        **tenure_b,
        # Skill details
        **skills_b,
        # Experience details
        **exp_b,
        # AI reasoning (ai_fit_score duplicated here for convenience)
        **ai_b,
    }
    return round(final, 1), breakdown
