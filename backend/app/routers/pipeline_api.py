"""
New pipeline API endpoints: dashboard, requisitions CRUD,
kanban, candidates, interviews, hiring-manager review.
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import get_current_user

router = APIRouter(prefix="/api", tags=["pipeline"])


# ─── helpers ──────────────────────────────────────────────────────────────────

def _is_recruiter_scoped(role: str) -> bool:
    return role == "recruiter"


# ─── Dashboard ────────────────────────────────────────────────────────────────

@router.get("/dashboard")
def dashboard(user: dict = Depends(get_current_user)):
    role = user["role"]
    uid  = user["sub"]

    if role == "recruiter":
        # Only count applications under the recruiter's own requisitions
        app_filter = """
            a.requisition_id IN (
                SELECT requisition_id FROM requisition_recruiter WHERE recruiter_id = %s
            )
        """
        req_filter = """
            r.id IN (
                SELECT requisition_id FROM requisition_recruiter WHERE recruiter_id = %s
            )
        """
        p = [uid]
    else:
        app_filter = "1=1"
        req_filter  = "1=1"
        p = []

    def cnt(extra_where):
        row = query_one(
            f"SELECT COUNT(*) AS n FROM application a WHERE {app_filter} AND ({extra_where})",
            p,
        )
        return int(row["n"]) if row else 0

    open_reqs = query_one(
        f"SELECT COUNT(*) AS n FROM requisition r WHERE {req_filter} AND r.status='open'",
        p,
    )

    counts = {
        "open_reqs":         int(open_reqs["n"]) if open_reqs else 0,
        "apps_received":     cnt("1=1"),
        "under_screening":   cnt("a.status='screening'"),
        "screening_cleared": cnt("a.status='screen_passed'"),
        "ai_interview":      cnt("a.bot_score IS NOT NULL"),
        "panel_interview":   cnt("a.status='interviewing'"),
        "selected":          cnt("a.status='selected'"),
        "offer_stage":       cnt("a.status IN ('offer_stage','offered')"),
        "joined":            cnt("a.status='joined'"),
    }

    # Average days to hire (stage_event: applied → joined)
    ath = query_one(
        """
        SELECT ROUND(
            AVG(EXTRACT(EPOCH FROM (e2.occurred_at - e1.occurred_at)) / 86400)::numeric, 1
        ) AS avg_days
        FROM stage_event e1
        JOIN stage_event e2 ON e2.application_id = e1.application_id
        WHERE e1.to_status = 'applied' AND e2.to_status = 'joined'
        """,
        [],
    )
    counts["avg_days_to_hire"] = float(ath["avg_days"]) if ath and ath["avg_days"] else None

    # Gender split (global or scoped)
    if role == "recruiter":
        gender = query(
            """
            SELECT c.gender, COUNT(*) AS n
            FROM application a
            JOIN candidate c ON c.id = a.candidate_id
            WHERE a.requisition_id IN (
                SELECT requisition_id FROM requisition_recruiter WHERE recruiter_id = %s
            )
            GROUP BY c.gender
            """,
            [uid],
        )
    else:
        gender = query(
            "SELECT gender, COUNT(*) AS n FROM candidate GROUP BY gender",
            [],
        )
    counts["gender_split"] = gender

    # Recent requisitions (scoped)
    if role == "recruiter":
        reqs = query(
            """
            SELECT r.id, r.title, r.status, b.code AS band, bu.name AS business_unit,
                   (SELECT COUNT(*) FROM application WHERE requisition_id = r.id) AS in_pipeline,
                   (SELECT COUNT(*) FROM round_config  WHERE requisition_id = r.id) AS levels
            FROM requisition r
            JOIN requisition_recruiter rr ON rr.requisition_id = r.id AND rr.recruiter_id = %s
            JOIN band b          ON b.id = r.band_id
            JOIN business_unit bu ON bu.id = r.bu_id
            ORDER BY r.created_at DESC LIMIT 10
            """,
            [uid],
        )
    elif role == "hiring_manager":
        reqs = query(
            """
            SELECT r.id, r.title, r.status, b.code AS band, bu.name AS business_unit,
                   (SELECT COUNT(*) FROM application WHERE requisition_id = r.id) AS in_pipeline,
                   (SELECT COUNT(*) FROM round_config  WHERE requisition_id = r.id) AS levels
            FROM requisition r
            JOIN band b          ON b.id = r.band_id
            JOIN business_unit bu ON bu.id = r.bu_id
            WHERE r.hiring_manager_id = %s
            ORDER BY r.created_at DESC LIMIT 10
            """,
            [uid],
        )
    else:
        reqs = query(
            """
            SELECT r.id, r.title, r.status, b.code AS band, bu.name AS business_unit,
                   (SELECT COUNT(*) FROM application WHERE requisition_id = r.id) AS in_pipeline,
                   (SELECT COUNT(*) FROM round_config  WHERE requisition_id = r.id) AS levels
            FROM requisition r
            JOIN band b          ON b.id = r.band_id
            JOIN business_unit bu ON bu.id = r.bu_id
            ORDER BY r.created_at DESC LIMIT 10
            """,
            [],
        )
    counts["recent_reqs"] = reqs

    # Recruiter load panel (ta_manager / admin only)
    if role in ("ta_manager", "admin"):
        counts["recruiter_load"] = query("SELECT * FROM v_recruiter_load", [])

    # Hiring manager: profiles awaiting review
    if role == "hiring_manager":
        counts["profiles_to_review"] = query(
            """
            SELECT a.id, c.full_name, r.title AS req_title,
                   a.combined_score, a.match_score, a.status
            FROM application a
            JOIN candidate  c ON c.id  = a.candidate_id
            JOIN requisition r ON r.id = a.requisition_id
            WHERE r.hiring_manager_id = %s
              AND a.status = 'selected'
              AND (a.hm_feedback IS NULL OR a.hm_feedback = '')
            ORDER BY a.combined_score DESC NULLS LAST
            LIMIT 20
            """,
            [uid],
        )

    return counts


# ─── Requisitions ─────────────────────────────────────────────────────────────

@router.get("/requisitions/full")
def list_requisitions_full(user: dict = Depends(get_current_user)):
    role = user["role"]
    uid  = user["sub"]
    if role == "recruiter":
        return query(
            """
            SELECT r.id, r.title, r.status, r.roll_type, r.fiscal_year,
                   r.is_p1, r.risk, r.hiring_location,
                   b.code AS band, bu.name AS business_unit,
                   (SELECT COUNT(*) FROM application  WHERE requisition_id = r.id) AS in_pipeline,
                   (SELECT COUNT(*) FROM round_config WHERE requisition_id = r.id) AS levels
            FROM requisition r
            JOIN requisition_recruiter rr ON rr.requisition_id = r.id AND rr.recruiter_id = %s
            JOIN band b          ON b.id = r.band_id
            JOIN business_unit bu ON bu.id = r.bu_id
            ORDER BY r.created_at DESC
            """,
            [uid],
        )
    return query(
        """
        SELECT r.id, r.title, r.status, r.roll_type, r.fiscal_year,
               r.is_p1, r.risk, r.hiring_location,
               b.code AS band, bu.name AS business_unit,
               (SELECT COUNT(*) FROM application  WHERE requisition_id = r.id) AS in_pipeline,
               (SELECT COUNT(*) FROM round_config WHERE requisition_id = r.id) AS levels
        FROM requisition r
        JOIN band b          ON b.id = r.band_id
        JOIN business_unit bu ON bu.id = r.bu_id
        ORDER BY r.created_at DESC
        """,
        [],
    )


class RoundIn(BaseModel):
    sequence: int
    name: str
    round_type: str = "panel"
    is_auto: bool = False


class RequisitionIn(BaseModel):
    title: str
    bu_id: str
    band_id: str
    roll_type: str = "on_roll"
    key_skills: list[str] = []
    min_experience: Optional[float] = None
    budgeted_ctc: Optional[float] = None
    openings: int = 1
    fiscal_year: Optional[str] = None
    job_description: Optional[str] = None
    is_p1: bool = False
    risk: Optional[str] = None
    hiring_location: Optional[str] = None
    rounds: list[RoundIn] = []


@router.post("/requisitions")
def create_requisition(body: RequisitionIn, user: dict = Depends(get_current_user)):
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Only recruiters and TA managers can create requisitions")
    req = query_one(
        """
        INSERT INTO requisition
          (title, bu_id, band_id, roll_type, key_skills, min_experience,
           budgeted_ctc, openings, fiscal_year, job_description,
           is_p1, risk, hiring_location,
           status, opened_at, created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',now(),%s)
        RETURNING id, title, status
        """,
        [
            body.title, body.bu_id, body.band_id, body.roll_type,
            body.key_skills, body.min_experience, body.budgeted_ctc,
            body.openings, body.fiscal_year, body.job_description,
            body.is_p1, body.risk, body.hiring_location,
            user["sub"],
        ],
    )
    # Auto-assign recruiter as owner
    if user["role"] == "recruiter":
        query(
            """INSERT INTO requisition_recruiter
               (requisition_id, recruiter_id, is_owner, assigned_by)
               VALUES (%s,%s,true,%s)""",
            [req["id"], user["sub"], user["sub"]],
            fetch=False,
        )
    # Create panel rounds
    for r in body.rounds:
        query(
            """INSERT INTO round_config
               (requisition_id, sequence, name, round_type, is_auto)
               VALUES (%s,%s,%s,%s,%s)""",
            [req["id"], r.sequence, r.name, r.round_type, r.is_auto],
            fetch=False,
        )
    return req


@router.get("/requisitions/{req_id}/detail")
def get_requisition_detail(req_id: str, _user: dict = Depends(get_current_user)):
    req = query_one(
        """
        SELECT r.*, b.code AS band_code, bu.name AS business_unit_name
        FROM requisition r
        JOIN band b          ON b.id = r.band_id
        JOIN business_unit bu ON bu.id = r.bu_id
        WHERE r.id = %s
        """,
        [req_id],
    )
    if not req:
        raise HTTPException(404, "requisition not found")
    rounds = query(
        "SELECT * FROM round_config WHERE requisition_id = %s ORDER BY sequence",
        [req_id],
    )
    return {**dict(req), "rounds": rounds}


@router.get("/requisitions/{req_id}/kanban")
def kanban(req_id: str, _user: dict = Depends(get_current_user)):
    rounds = query(
        """SELECT id, sequence, name, round_type, is_auto
           FROM round_config WHERE requisition_id = %s ORDER BY sequence""",
        [req_id],
    )
    candidates = query(
        """
        SELECT a.id AS app_id, a.status, a.current_round,
               COALESCE(a.combined_score, a.match_score) AS score,
               c.full_name, c.gender
        FROM application a
        JOIN candidate c ON c.id = a.candidate_id
        WHERE a.requisition_id = %s
          AND a.status NOT IN ('screen_rejected','rejected','dropped')
        ORDER BY score DESC NULLS LAST
        """,
        [req_id],
    )
    return {"rounds": rounds, "candidates": candidates}


# ─── Candidates ───────────────────────────────────────────────────────────────

@router.get("/candidates")
def list_candidates(user: dict = Depends(get_current_user)):
    role = user["role"]
    uid  = user["sub"]
    if role == "recruiter":
        return query(
            """
            SELECT c.id, c.full_name, c.email, c.gender,
                   r.title AS requisition, a.status,
                   a.combined_score, a.match_score, a.id AS app_id
            FROM candidate c
            JOIN application  a ON a.candidate_id = c.id
            JOIN requisition  r ON r.id = a.requisition_id
            JOIN requisition_recruiter rr
                 ON rr.requisition_id = r.id AND rr.recruiter_id = %s
            ORDER BY a.combined_score DESC NULLS LAST
            """,
            [uid],
        )
    return query(
        """
        SELECT c.id, c.full_name, c.email, c.gender,
               r.title AS requisition, a.status,
               a.combined_score, a.match_score, a.id AS app_id
        FROM candidate c
        JOIN application a ON a.candidate_id = c.id
        JOIN requisition r ON r.id = a.requisition_id
        ORDER BY a.combined_score DESC NULLS LAST
        """,
        [],
    )


# ─── Interviews ───────────────────────────────────────────────────────────────

@router.get("/interviews")
def list_interviews(user: dict = Depends(get_current_user)):
    role = user["role"]
    uid  = user["sub"]
    if role == "recruiter":
        return query(
            """
            SELECT i.id, i.scheduled_at, i.status, i.meet_link, i.mode,
                   c.full_name AS candidate_name, r.title AS requisition,
                   rc.name AS round_name
            FROM interview i
            JOIN application  a  ON a.id  = i.application_id
            JOIN candidate    c  ON c.id  = a.candidate_id
            JOIN requisition  r  ON r.id  = a.requisition_id
            JOIN round_config rc ON rc.id = i.round_config_id
            JOIN requisition_recruiter rr
                 ON rr.requisition_id = r.id AND rr.recruiter_id = %s
            ORDER BY i.scheduled_at DESC NULLS LAST
            """,
            [uid],
        )
    return query(
        """
        SELECT i.id, i.scheduled_at, i.status, i.meet_link, i.mode,
               c.full_name AS candidate_name, r.title AS requisition,
               rc.name AS round_name
        FROM interview i
        JOIN application  a  ON a.id  = i.application_id
        JOIN candidate    c  ON c.id  = a.candidate_id
        JOIN requisition  r  ON r.id  = a.requisition_id
        JOIN round_config rc ON rc.id = i.round_config_id
        ORDER BY i.scheduled_at DESC NULLS LAST
        """,
        [],
    )


# ─── Hiring-manager review ────────────────────────────────────────────────────

@router.get("/profiles-to-review")
def profiles_to_review(user: dict = Depends(get_current_user)):
    uid = user["sub"]
    return query(
        """
        SELECT a.id, c.full_name, c.email, c.gender,
               r.title AS req_title, r.id AS req_id,
               a.combined_score, a.match_score, a.status,
               a.hm_feedback, a.hm_reviewed_at
        FROM application a
        JOIN candidate  c ON c.id  = a.candidate_id
        JOIN requisition r ON r.id = a.requisition_id
        WHERE r.hiring_manager_id = %s
          AND a.status IN ('selected','interviewing')
          AND (a.hm_feedback IS NULL OR a.hm_feedback = '')
        ORDER BY a.combined_score DESC NULLS LAST
        """,
        [uid],
    )


class HMFeedbackIn(BaseModel):
    approved: bool
    comment: Optional[str] = None


@router.post("/applications/{app_id}/hm-feedback")
def hm_feedback(
    app_id: str,
    body: HMFeedbackIn,
    _user: dict = Depends(get_current_user),
):
    verdict = body.comment or ("Approved" if body.approved else "Not approved")
    row = query_one(
        """UPDATE application
           SET hm_feedback = %s, hm_reviewed_at = now()
           WHERE id = %s
           RETURNING id, status, hm_feedback""",
        [verdict, app_id],
    )
    if not row:
        raise HTTPException(404, "application not found")
    return row
