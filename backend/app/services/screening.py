"""
Screening engine.

Scores a candidate against a requisition's job description and key skills.

In production, the `ai_screen` function would call a language model
(managed API with no-retention, or a self-hosted model on a company GCP VM)
to do nuanced resume understanding. The MODEL CALL IS ISOLATED HERE so the
rest of the system does not care which model runs behind it -- swap the body
of `ai_screen` and nothing else changes.

For the prototype, we use a transparent rule-based scorer so the logic is
visible and testable without external credentials.
"""
from typing import Optional


def keyword_match_score(resume_text: str, key_skills: list[str]) -> tuple[float, dict]:
    """Fraction of required skills found in the resume text, 0-100."""
    if not key_skills:
        return 50.0, {"matched_skills": [], "note": "no key skills defined"}
    resume_lc = (resume_text or "").lower()
    matched = [s for s in key_skills if s.lower() in resume_lc]
    score = 100.0 * len(matched) / len(key_skills)
    return score, {
        "matched_skills": matched,
        "missing_skills": [s for s in key_skills if s not in matched],
        "skills_matched_count": len(matched),
        "skills_total": len(key_skills),
    }


def experience_score(years: Optional[float], min_required: Optional[float]) -> tuple[float, dict]:
    """100 if meets/exceeds requirement, partial credit below."""
    if min_required is None or years is None:
        return 50.0, {"experience_note": "experience not evaluated"}
    years = float(years)
    min_required = float(min_required)
    if years >= min_required:
        return 100.0, {"experience_met": True, "years": years, "required": min_required}
    ratio = max(0.0, years / min_required) if min_required else 0.0
    return 100.0 * ratio, {"experience_met": False, "years": years, "required": min_required}


def ai_screen(resume_text: str, job_description: str) -> tuple[float, dict]:
    """
    PLACEHOLDER for the AI model call.

    Production: send resume_text + job_description to the model and parse a
    0-100 relevance score plus reasoning. Keep this function as the ONLY place
    that talks to the model.

    Prototype: returns a neutral score so the blended result is driven by the
    transparent rule-based components above.
    """
    return 60.0, {"ai_note": "stubbed model score (replace ai_screen in production)"}


def score_application(resume_text: str, candidate_years: Optional[float],
                      requisition: dict) -> tuple[float, dict]:
    """
    Blend the components into one 0-100 match score with a breakdown the
    recruiter can see. Weights are easy to tune.
    """
    skills_s, skills_b = keyword_match_score(resume_text, requisition.get("key_skills") or [])
    exp_s, exp_b = experience_score(candidate_years, requisition.get("min_experience"))
    ai_s, ai_b = ai_screen(resume_text, requisition.get("job_description") or "")

    weights = {"skills": 0.5, "experience": 0.3, "ai": 0.2}
    final = skills_s * weights["skills"] + exp_s * weights["experience"] + ai_s * weights["ai"]

    breakdown = {
        "skills_score": round(skills_s, 1),
        "experience_score": round(exp_s, 1),
        "ai_score": round(ai_s, 1),
        "weights": weights,
        **skills_b, **exp_b, **ai_b,
    }
    return round(final, 1), breakdown
