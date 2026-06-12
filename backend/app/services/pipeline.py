"""
Pipeline state machine.

This is the heart of "one click hire": it advances an application through the
stages and writes a stage_event row on every transition. Those events power
all TAT reporting. Automated stages move themselves; human gates wait for a
recruiter action.
"""
import json
import os
from decimal import Decimal

from ..db import query, query_one
from . import screening, connectors


def _json_safe(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"not serializable: {type(obj)}")


def log_event(application_id, from_status, to_status, actor_id=None, note=None):
    query(
        """INSERT INTO stage_event (application_id, from_status, to_status, actor_id, note)
           VALUES (%s, %s, %s, %s, %s)""",
        [application_id, from_status, to_status, actor_id, note],
        fetch=False,
    )


def _extract_ai_detail(breakdown: dict) -> dict:
    """Pull the AI reasoning fields out of a breakdown dict into their own dict."""
    return {
        k: breakdown.get(k)
        for k in ("strengths", "concerns", "rationale", "scored_by", "fallback_reason")
        if breakdown.get(k) is not None
    }


def intake_and_screen(requisition_id, candidate_id, resume_text, candidate_years):
    """
    AUTOMATED. Runs when an application arrives: scores it, stores all screening
    columns, and parks it in the Gate-1 review queue. Returns the application row.
    """
    req = query_one(
        """SELECT r.*, b.code AS band_code
           FROM requisition r
           JOIN band b ON b.id = r.band_id
           WHERE r.id = %s""",
        [requisition_id],
    )
    if not req:
        raise ValueError("requisition not found")

    score, breakdown = screening.score_application(resume_text, candidate_years, req)

    ai_fit_score      = breakdown.get("ai_fit_score")
    ai_screen_detail  = json.dumps(_extract_ai_detail(breakdown), default=_json_safe)
    avg_tenure_months = breakdown.get("avg_tenure_months")
    stability_score   = breakdown.get("stability_score")
    stability_status  = breakdown.get("stability_status", "not_applicable")

    app = query_one(
        """INSERT INTO application
             (requisition_id, candidate_id, match_score, score_breakdown,
              ai_fit_score, ai_screen_detail,
              avg_tenure_months, stability_score, stability_status,
              status)
           VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s, %s, 'screen')
           ON CONFLICT (requisition_id, candidate_id) DO UPDATE
             SET match_score        = EXCLUDED.match_score,
                 score_breakdown    = EXCLUDED.score_breakdown,
                 ai_fit_score       = EXCLUDED.ai_fit_score,
                 ai_screen_detail   = EXCLUDED.ai_screen_detail,
                 avg_tenure_months  = EXCLUDED.avg_tenure_months,
                 stability_score    = EXCLUDED.stability_score,
                 stability_status   = EXCLUDED.stability_status
           RETURNING *""",
        [
            requisition_id, candidate_id, score,
            json.dumps(breakdown, default=_json_safe),
            ai_fit_score, ai_screen_detail,
            avg_tenure_months, stability_score, stability_status,
        ],
    )
    log_event(app["id"], "applied", "screen", note=f"auto-scored {score}")
    return app


def run_bot_round(application_id):
    """
    AUTOMATED (assistive). Runs the AI bot interview, stores the bot score, and
    computes the combined chart score. Does NOT advance past a gate.
    """
    app = query_one("SELECT * FROM application WHERE id = %s", [application_id])
    req = query_one("SELECT * FROM requisition WHERE id = %s", [app["requisition_id"]])
    result = connectors.run_bot_interview(app["candidate_id"], req.get("job_description") or "")

    match    = float(app["match_score"] or 0)
    bot      = result["bot_score"]
    combined = round(0.4 * match + 0.6 * bot, 1)  # tunable blend

    query(
        """UPDATE application
             SET bot_score = %s, combined_score = %s, status = 'nexai_bot'
           WHERE id = %s""",
        [bot, combined, application_id], fetch=False,
    )
    log_event(application_id, "screen", "nexai_bot",
              note=f"bot {bot}, combined {combined}")
    return {"bot_score": bot, "combined_score": combined}


def advance(application_id, to_status, actor_id=None, note=None):
    """
    HUMAN GATE. A recruiter taps to move an application forward or out.
    Records who did it for the audit trail.
    """
    app = query_one("SELECT status FROM application WHERE id = %s", [application_id])
    from_status = app["status"] if app else None
    query("UPDATE application SET status = %s WHERE id = %s",
          [to_status, application_id], fetch=False)
    log_event(application_id, from_status, to_status, actor_id, note)
    return query_one("SELECT * FROM application WHERE id = %s", [application_id])


def top_chart(requisition_id, limit=50):
    """Ranked chart of candidates by combined score -- what the recruiter sees
    to decide who advances past the bot round."""
    return query(
        """SELECT a.id, c.full_name, c.gender, a.match_score, a.bot_score,
                  a.combined_score, a.status
           FROM application a JOIN candidate c ON c.id = a.candidate_id
           WHERE a.requisition_id = %s
           ORDER BY a.combined_score DESC NULLS LAST
           LIMIT %s""",
        [requisition_id, limit],
    )


def update_manual_tenure(application_id: str, avg_tenure_months: float, actor_id=None):
    """
    Recruiter-provided average tenure for a 'pending_manual' application.
    Recomputes stability_score and match_score using the four-dimension weights.
    """
    app = query_one("SELECT * FROM application WHERE id = %s", [application_id])
    if not app:
        raise ValueError("application not found")

    bd = app.get("score_breakdown") or {}
    if isinstance(bd, str):
        bd = json.loads(bd)

    stability_s = screening.compute_stability_score(avg_tenure_months)

    skills_s = float(bd.get("skills_score") or 50.0)
    exp_s    = float(bd.get("experience_score") or 50.0)
    ai_s     = float(bd.get("ai_score") or 50.0)

    w_kw  = screening.SCORE_WEIGHT_KEYWORD
    w_exp = screening.SCORE_WEIGHT_EXPERIENCE
    w_ai  = screening.SCORE_WEIGHT_AI
    w_st  = screening.SCORE_WEIGHT_STABILITY

    new_score = round(
        skills_s * w_kw + exp_s * w_exp + ai_s * w_ai + stability_s * w_st, 1
    )

    bd.update({
        "stability_score":   round(stability_s, 1),
        "stability_status":  "computed",
        "avg_tenure_months": round(avg_tenure_months, 1),
        "weights": {
            "keyword": w_kw, "experience": w_exp,
            "ai": w_ai, "stability": w_st,
        },
    })

    query(
        """UPDATE application
             SET match_score       = %s,
                 score_breakdown   = %s::jsonb,
                 avg_tenure_months = %s,
                 stability_score   = %s,
                 stability_status  = 'computed'
           WHERE id = %s""",
        [
            new_score,
            json.dumps(bd, default=_json_safe),
            round(avg_tenure_months, 1),
            round(stability_s, 1),
            application_id,
        ],
        fetch=False,
    )
    log_event(
        application_id, None, None, actor_id,
        f"manual-tenure {avg_tenure_months:.0f}m → stability {stability_s:.0f}, score {new_score}",
    )
    return {
        "match_score":       new_score,
        "stability_score":   round(stability_s, 1),
        "avg_tenure_months": round(avg_tenure_months, 1),
        "stability_status":  "computed",
    }


def rescreen_application(application_id: str, actor_id=None):
    """
    Deliberate recruiter action: re-run AI screening for a single application
    using the candidate's stored resume file. Overwrites match_score and all
    screening columns. Does NOT touch bot_score / combined_score / status.
    """
    app  = query_one("SELECT * FROM application WHERE id = %s", [application_id])
    if not app:
        raise ValueError("application not found")

    cand = query_one("SELECT * FROM candidate WHERE id = %s", [app["candidate_id"]])
    req  = query_one(
        """SELECT r.*, b.code AS band_code
           FROM requisition r JOIN band b ON b.id = r.band_id
           WHERE r.id = %s""",
        [app["requisition_id"]],
    )

    # Resolve resume text: fast path from cv_repository.raw_text (already extracted),
    # fall back to re-parsing from disk if the row is missing or text is empty.
    resume_text = ""
    try:
        _cv = query_one(
            """SELECT cv.raw_text, cv.file_path
               FROM cv_repository cv
               WHERE cv.candidate_id = %s
               ORDER BY cv.created_at DESC LIMIT 1""",
            [str(cand["id"])],
        )
    except Exception:
        _cv = None

    if _cv and _cv.get("raw_text"):
        resume_text = _cv["raw_text"]
    elif cand.get("resume_url") or (_cv and _cv.get("file_path")):
        _resume_path = (_cv or {}).get("file_path") or cand.get("resume_url") or ""
        try:
            from .resume_parser import extract_text as _parse_resume
            with open(_resume_path, "rb") as fh:
                file_bytes = fh.read()
            filename = os.path.basename(_resume_path)
            resume_text, _ = _parse_resume(file_bytes, filename)
        except Exception as exc:
            print(f"[rescreen] Could not read resume for {application_id}: {exc}")

    # Recover candidate_years from stored breakdown if available
    candidate_years = None
    old_bd = app.get("score_breakdown") or {}
    if isinstance(old_bd, str):
        old_bd = json.loads(old_bd)
    yr = old_bd.get("years")
    if yr is not None:
        try:
            candidate_years = float(yr)
        except (TypeError, ValueError):
            pass

    score, breakdown = screening.score_application(resume_text, candidate_years, req)

    ai_fit_score      = breakdown.get("ai_fit_score")
    ai_screen_detail  = json.dumps(_extract_ai_detail(breakdown), default=_json_safe)
    avg_tenure_months = breakdown.get("avg_tenure_months")
    stability_score   = breakdown.get("stability_score")
    stability_status  = breakdown.get("stability_status", "not_applicable")

    query(
        """UPDATE application
             SET match_score       = %s,
                 score_breakdown   = %s::jsonb,
                 ai_fit_score      = %s,
                 ai_screen_detail  = %s::jsonb,
                 avg_tenure_months = %s,
                 stability_score   = %s,
                 stability_status  = %s
           WHERE id = %s""",
        [
            score,
            json.dumps(breakdown, default=_json_safe),
            ai_fit_score, ai_screen_detail,
            avg_tenure_months, stability_score, stability_status,
            application_id,
        ],
        fetch=False,
    )
    log_event(application_id, app["status"], app["status"],
              actor_id, f"re-screened: {score}")
    return {
        "match_score":       score,
        "breakdown":         breakdown,
        "stability_status":  stability_status,
    }
