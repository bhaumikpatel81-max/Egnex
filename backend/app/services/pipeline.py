"""
Pipeline state machine.

This is the heart of "one click hire": it advances an application through the
stages and writes a stage_event row on every transition. Those events power
all TAT reporting. Automated stages move themselves; human gates wait for a
recruiter action.
"""
from ..db import query, query_one
from . import screening, connectors
import json
from decimal import Decimal


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


def intake_and_screen(requisition_id, candidate_id, resume_text, candidate_years):
    """
    AUTOMATED. Runs when an application arrives: scores it, stores the score,
    and parks it in the Gate-1 review queue (screen_passed/screen_rejected
    suggestion) -- but never silently rejects. Returns the application row.
    """
    req = query_one("SELECT * FROM requisition WHERE id = %s", [requisition_id])
    if not req:
        raise ValueError("requisition not found")

    score, breakdown = screening.score_application(resume_text, candidate_years, req)

    app = query_one(
        """INSERT INTO application
             (requisition_id, candidate_id, match_score, score_breakdown, status)
           VALUES (%s, %s, %s, %s::jsonb, 'screening')
           ON CONFLICT (requisition_id, candidate_id) DO UPDATE
             SET match_score = EXCLUDED.match_score,
                 score_breakdown = EXCLUDED.score_breakdown
           RETURNING *""",
        [requisition_id, candidate_id, score, json.dumps(breakdown, default=_json_safe)],
    )
    log_event(app["id"], "applied", "screening", note=f"auto-scored {score}")
    return app


def run_bot_round(application_id):
    """
    AUTOMATED (assistive). Runs the AI bot interview, stores the bot score, and
    computes the combined chart score. Does NOT advance past a gate.
    """
    app = query_one("SELECT * FROM application WHERE id = %s", [application_id])
    req = query_one("SELECT * FROM requisition WHERE id = %s", [app["requisition_id"]])
    result = connectors.run_bot_interview(app["candidate_id"], req.get("job_description") or "")

    match = float(app["match_score"] or 0)
    bot = result["bot_score"]
    combined = round(0.4 * match + 0.6 * bot, 1)  # tunable blend

    query(
        """UPDATE application
             SET bot_score = %s, combined_score = %s, status = 'screen_passed'
           WHERE id = %s""",
        [bot, combined, application_id], fetch=False,
    )
    log_event(application_id, "screening", "screen_passed",
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
