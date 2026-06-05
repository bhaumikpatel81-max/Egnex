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

    # ─── NexAI data ────────────────────────────────────────────────────────────

    _NX_SUMMARY_COLS = """
        COUNT(*)                                                           AS total,
        COUNT(*) FILTER (WHERE ns.status = 'completed')                    AS completed,
        COUNT(*) FILTER (WHERE ns.status = 'failed')                       AS failed,
        COUNT(*) FILTER (WHERE ns.status IN ('pending','in_progress'))     AS pending,
        ROUND(AVG(ns.raw_score) FILTER (WHERE ns.status='completed')
              ::numeric, 1)                                                AS avg_score,
        COUNT(*) FILTER (WHERE ns.raw_score >= 70 AND ns.status='completed') AS high_scorers,
        COUNT(*) FILTER (WHERE ns.raw_score <  40 AND ns.status='completed') AS low_scorers,
        ROUND(
          COALESCE(
            COUNT(*) FILTER (WHERE ns.raw_score >= 50 AND ns.status='completed')
            ::numeric /
            NULLIF(COUNT(*) FILTER (WHERE ns.status='completed'), 0),
          0) * 100, 1
        )                                                                  AS pass_rate
    """

    if role == "recruiter":
        nx_where = """
            JOIN requisition r2 ON r2.id = ns.requisition_id
            JOIN requisition_recruiter rr2
                 ON rr2.requisition_id = r2.id AND rr2.recruiter_id = %s
        """
        nx_params = [uid]
        nx_dist_where = f"""
            WHERE ns.status = 'completed' AND ns.raw_score IS NOT NULL
              AND ns.requisition_id IN (
                  SELECT requisition_id FROM requisition_recruiter WHERE recruiter_id = %s
              )
        """
        nx_dist_params = [uid]
        nx_recent_where = f"""
            JOIN requisition ri ON ri.id = ns.requisition_id
            JOIN requisition_recruiter rir
                 ON rir.requisition_id = ri.id AND rir.recruiter_id = %s
        """
        nx_recent_params = [uid]
    else:
        nx_where = ""
        nx_params = []
        nx_dist_where = "WHERE ns.status = 'completed' AND ns.raw_score IS NOT NULL"
        nx_dist_params = []
        nx_recent_where = ""
        nx_recent_params = []

    if role in ("recruiter", "ta_manager"):
        nx_row = query_one(
            f"SELECT {_NX_SUMMARY_COLS} FROM nexai_session ns {nx_where}",
            nx_params,
        )
        counts["nexai_summary"] = dict(nx_row) if nx_row else {}

        counts["nexai_score_dist"] = query(
            f"""
            SELECT
              CASE
                WHEN raw_score >= 80 THEN '80-100'
                WHEN raw_score >= 60 THEN '60-79'
                WHEN raw_score >= 40 THEN '40-59'
                WHEN raw_score >= 20 THEN '20-39'
                ELSE '0-19'
              END AS bucket,
              CASE
                WHEN raw_score >= 80 THEN 5
                WHEN raw_score >= 60 THEN 4
                WHEN raw_score >= 40 THEN 3
                WHEN raw_score >= 20 THEN 2
                ELSE 1
              END AS sort_ord,
              COUNT(*) AS n
            FROM nexai_session ns
            {nx_dist_where}
            GROUP BY bucket, sort_ord
            ORDER BY sort_ord
            """,
            nx_dist_params,
        )

        counts["nexai_recent"] = query(
            f"""
            SELECT ns.id, ns.raw_score, ns.status,
                   ns.created_at, ns.completed_at,
                   c.full_name AS candidate_name,
                   r.title     AS req_title
            FROM nexai_session ns
            JOIN application a ON a.id = ns.application_id
            JOIN candidate   c ON c.id = a.candidate_id
            JOIN requisition r ON r.id = ns.requisition_id
            {nx_recent_where}
            ORDER BY ns.created_at DESC LIMIT 10
            """,
            nx_recent_params,
        )

    if role == "ta_manager":
        counts["nexai_by_recruiter"] = query(
            """
            SELECT u.full_name AS recruiter_name,
                   COUNT(ns.id)                                                   AS total,
                   COUNT(ns.id) FILTER (WHERE ns.status='completed')              AS completed,
                   ROUND(AVG(ns.raw_score) FILTER
                         (WHERE ns.status='completed')::numeric, 1)               AS avg_score,
                   COUNT(ns.id) FILTER
                         (WHERE ns.raw_score >= 70 AND ns.status='completed')     AS high_scorers
            FROM app_user u
            LEFT JOIN requisition_recruiter rr ON rr.recruiter_id = u.id
            LEFT JOIN nexai_session ns ON ns.requisition_id = rr.requisition_id
            WHERE u.role IN ('recruiter','ta_manager') AND u.is_active = true
            GROUP BY u.id, u.full_name
            ORDER BY avg_score DESC NULLS LAST, u.full_name
            """,
            [],
        )

    # Recruiter load panel (ta_manager / admin only)
    if role in ("ta_manager", "admin"):
        counts["recruiter_load"] = query("SELECT * FROM v_recruiter_load", [])

    # TA Manager: hiring manager overview
    if role == "ta_manager":
        counts["hiring_manager_stats"] = query(
            """
            SELECT u.id AS hm_id, u.full_name, u.email,
                   COUNT(DISTINCT r.id) AS assigned_reqs,
                   SUM(CASE WHEN a.status = 'selected'
                                 AND (a.hm_feedback IS NULL OR a.hm_feedback = '')
                            THEN 1 ELSE 0 END)              AS pending_reviews,
                   COUNT(DISTINCT CASE WHEN a.hm_feedback IS NOT NULL
                                            AND a.hm_feedback != ''
                                       THEN a.id END)       AS reviews_done
            FROM app_user u
            LEFT JOIN requisition r  ON r.hiring_manager_id = u.id
            LEFT JOIN application a  ON a.requisition_id    = r.id
            WHERE u.role = 'hiring_manager' AND u.is_active = true
            GROUP BY u.id, u.full_name, u.email
            ORDER BY pending_reviews DESC, u.full_name
            """,
            [],
        )

    # Hiring manager: profiles + interviews + nexai + skills + time data
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
        counts["my_interviews"] = query(
            """
            SELECT i.id, i.scheduled_at, i.mode, i.duration_min,
                   COALESCE(i.status, 'scheduled') AS status,
                   c.full_name  AS candidate_name,
                   r.title      AS req_title,
                   rc.name      AS round_name
            FROM interview i
            JOIN application  a  ON a.id  = i.application_id
            JOIN candidate    c  ON c.id  = a.candidate_id
            JOIN requisition  r  ON r.id  = a.requisition_id
            LEFT JOIN round_config rc ON rc.id = i.round_config_id
            WHERE r.hiring_manager_id = %s
            ORDER BY i.scheduled_at DESC LIMIT 10
            """,
            [uid],
        )
        counts["feedback_outcomes"] = query(
            """
            SELECT COALESCE(NULLIF(a.hm_feedback,''), 'pending') AS outcome,
                   COUNT(*) AS n
            FROM application a
            JOIN requisition r ON r.id = a.requisition_id
            WHERE r.hiring_manager_id = %s
              AND a.status IN ('selected','offer_stage','offered','joined','rejected')
            GROUP BY COALESCE(NULLIF(a.hm_feedback,''), 'pending')
            ORDER BY n DESC
            """,
            [uid],
        )

        # Interviews conducted + time stats
        itime = query_one(
            """
            SELECT COUNT(DISTINCT i.id)                          AS n,
                   ROUND(AVG(i.duration_min)::numeric, 0)        AS avg_min,
                   ROUND(SUM(i.duration_min)::numeric / 60.0, 1) AS total_hrs
            FROM interview i
            JOIN application a  ON a.id = i.application_id
            JOIN requisition r  ON r.id = a.requisition_id
            WHERE r.hiring_manager_id = %s
            """,
            [uid],
        )
        counts["interviews_conducted"] = int(itime["n"]) if itime else 0
        counts["avg_interview_min"]    = float(itime["avg_min"])   if itime and itime["avg_min"]   else None
        counts["total_interview_hrs"]  = float(itime["total_hrs"]) if itime and itime["total_hrs"] else 0

        # NexAI screening summary for HM's requisitions
        nexai = query_one(
            """
            SELECT
              COUNT(*)                                                  AS total,
              COUNT(*) FILTER (WHERE ns.status = 'completed')          AS completed,
              COUNT(*) FILTER (WHERE ns.status = 'failed')             AS failed,
              COUNT(*) FILTER (WHERE ns.status IN ('pending','in_progress')) AS pending,
              ROUND(AVG(ns.raw_score) FILTER
                    (WHERE ns.status='completed')::numeric, 1)          AS avg_score,
              ROUND(AVG(
                EXTRACT(EPOCH FROM (ns.completed_at - ns.started_at))/60.0
              ) FILTER (WHERE ns.status='completed')::numeric, 1)       AS avg_session_min
            FROM nexai_session ns
            JOIN requisition r ON r.id = ns.requisition_id
            WHERE r.hiring_manager_id = %s
            """,
            [uid],
        )
        counts["nexai_summary"] = dict(nexai) if nexai else {
            "total": 0, "completed": 0, "failed": 0, "pending": 0,
            "avg_score": None, "avg_session_min": None,
        }

        # Skills breakdown: aggregate key_skills from HM's requisitions
        counts["skills_summary"] = query(
            """
            SELECT UNNEST(key_skills) AS skill, COUNT(*) AS n
            FROM requisition
            WHERE hiring_manager_id = %s
              AND key_skills IS NOT NULL AND array_length(key_skills, 1) > 0
            GROUP BY skill
            ORDER BY n DESC, skill
            LIMIT 15
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
                   r.hiring_manager_id,
                   hm.full_name AS hiring_manager_name,
                   (SELECT COUNT(*) FROM application  WHERE requisition_id = r.id) AS in_pipeline,
                   (SELECT COUNT(*) FROM round_config WHERE requisition_id = r.id) AS levels
            FROM requisition r
            JOIN requisition_recruiter rr ON rr.requisition_id = r.id AND rr.recruiter_id = %s
            JOIN band b          ON b.id = r.band_id
            JOIN business_unit bu ON bu.id = r.bu_id
            LEFT JOIN app_user hm ON hm.id = r.hiring_manager_id
            ORDER BY r.created_at DESC
            """,
            [uid],
        )
    return query(
        """
        SELECT r.id, r.title, r.status, r.roll_type, r.fiscal_year,
               r.is_p1, r.risk, r.hiring_location,
               b.code AS band, bu.name AS business_unit,
               r.hiring_manager_id,
               hm.full_name AS hiring_manager_name,
               (SELECT COUNT(*) FROM application  WHERE requisition_id = r.id) AS in_pipeline,
               (SELECT COUNT(*) FROM round_config WHERE requisition_id = r.id) AS levels
        FROM requisition r
        JOIN band b          ON b.id = r.band_id
        JOIN business_unit bu ON bu.id = r.bu_id
        LEFT JOIN app_user hm ON hm.id = r.hiring_manager_id
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
    budgeted_fixed: Optional[float] = None
    budgeted_variable: Optional[float] = None
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
    # Derive total CTC from fixed + variable if not explicitly provided
    fixed = body.budgeted_fixed
    variable = body.budgeted_variable
    total_ctc = body.budgeted_ctc
    if total_ctc is None and (fixed is not None or variable is not None):
        total_ctc = (fixed or 0) + (variable or 0)

    req = query_one(
        """
        INSERT INTO requisition
          (title, bu_id, band_id, roll_type, key_skills, min_experience,
           budgeted_ctc, budgeted_fixed, budgeted_variable,
           openings, fiscal_year, job_description,
           is_p1, risk, hiring_location,
           status, opened_at, created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',now(),%s)
        RETURNING id, title, status
        """,
        [
            body.title, body.bu_id, body.band_id, body.roll_type,
            body.key_skills, body.min_experience, total_ctc,
            fixed, variable,
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
               c.full_name, c.gender, c.email
        FROM application a
        JOIN candidate c ON c.id = a.candidate_id
        WHERE a.requisition_id = %s
          AND a.status NOT IN ('screen_rejected','rejected','dropped')
        ORDER BY score DESC NULLS LAST
        """,
        [req_id],
    )
    return {"rounds": rounds, "candidates": candidates}


# ─── Recruiter Assignment ─────────────────────────────────────────────────────

@router.get("/requisitions/{req_id}/recruiters")
def get_req_recruiters(req_id: str, user: dict = Depends(get_current_user)):
    if user["role"] not in ("ta_manager", "admin", "recruiter"):
        raise HTTPException(403, "Not authorised")
    return query(
        """SELECT u.id, u.full_name, u.email, rr.is_owner, rr.assigned_at
           FROM requisition_recruiter rr
           JOIN app_user u ON u.id = rr.recruiter_id
           WHERE rr.requisition_id = %s
           ORDER BY rr.is_owner DESC, rr.assigned_at""",
        [req_id],
    )


class AssignRecruiterIn(BaseModel):
    recruiter_id: str


@router.post("/requisitions/{req_id}/assign-recruiter")
def assign_recruiter(req_id: str, body: AssignRecruiterIn, user: dict = Depends(get_current_user)):
    if user["role"] not in ("ta_manager", "admin"):
        raise HTTPException(403, "Only TA managers can assign recruiters")
    req = query_one("SELECT id FROM requisition WHERE id = %s", [req_id])
    if not req:
        raise HTTPException(404, "Requisition not found")
    recruiter = query_one(
        "SELECT id FROM app_user WHERE id = %s AND role = 'recruiter' AND is_active = true",
        [body.recruiter_id],
    )
    if not recruiter:
        raise HTTPException(404, "Active recruiter not found")
    query(
        """INSERT INTO requisition_recruiter (requisition_id, recruiter_id, is_owner, assigned_by)
           VALUES (%s, %s, false, %s)
           ON CONFLICT (requisition_id, recruiter_id) DO NOTHING""",
        [req_id, body.recruiter_id, user["sub"]],
        fetch=False,
    )
    return {"ok": True}


@router.delete("/requisitions/{req_id}/recruiters/{recruiter_id}")
def unassign_recruiter(req_id: str, recruiter_id: str, user: dict = Depends(get_current_user)):
    if user["role"] not in ("ta_manager", "admin"):
        raise HTTPException(403, "Only TA managers can remove assignments")
    query(
        "DELETE FROM requisition_recruiter WHERE requisition_id = %s AND recruiter_id = %s",
        [req_id, recruiter_id],
        fetch=False,
    )
    return {"ok": True}


# ─── Team ──────────────────────────────────────────────────────────────────────

@router.get("/team")
def get_team(user: dict = Depends(get_current_user)):
    if user["role"] not in ("ta_manager", "admin"):
        raise HTTPException(403, "TA Manager access required")
    return query(
        """
        SELECT u.id, u.full_name, u.email, u.role,
               COUNT(DISTINCT rr.requisition_id)
                 FILTER (WHERE r.status = 'open') AS active_req_count,
               COALESCE(
                 json_agg(
                   json_build_object('req_id', r.id, 'title', r.title, 'status', r.status)
                 ) FILTER (WHERE r.id IS NOT NULL AND r.status = 'open'),
                 '[]'::json
               ) AS assigned_requisitions
        FROM app_user u
        LEFT JOIN requisition_recruiter rr ON rr.recruiter_id = u.id
        LEFT JOIN requisition r ON r.id = rr.requisition_id
        WHERE u.role IN ('recruiter', 'ta_manager', 'hiring_manager')
          AND u.is_active = true
        GROUP BY u.id, u.full_name, u.email, u.role
        ORDER BY u.role, u.full_name
        """
    )


# ─── Candidates ───────────────────────────────────────────────────────────────

@router.get("/candidates")
def list_candidates(user: dict = Depends(get_current_user)):
    role = user["role"]
    uid  = user["sub"]
    # Recruiter LATERAL sub-select: owner recruiter of each requisition
    _rec_lat = """
        LEFT JOIN LATERAL (
            SELECT rr2.recruiter_id, ru.full_name AS recruiter_name
            FROM requisition_recruiter rr2
            JOIN app_user ru ON ru.id = rr2.recruiter_id
            WHERE rr2.requisition_id = r.id
            ORDER BY rr2.is_owner DESC NULLS LAST
            LIMIT 1
        ) rc_info ON true
    """
    if role == "recruiter":
        return query(
            f"""
            SELECT c.id, c.full_name, c.email, c.gender,
                   r.id AS req_id, r.title AS requisition, a.status,
                   a.combined_score, a.match_score, a.id AS app_id,
                   rc_info.recruiter_id, rc_info.recruiter_name
            FROM candidate c
            JOIN application  a ON a.candidate_id = c.id
            JOIN requisition  r ON r.id = a.requisition_id
            JOIN requisition_recruiter rr
                 ON rr.requisition_id = r.id AND rr.recruiter_id = %s
            {_rec_lat}
            ORDER BY a.applied_at DESC
            """,
            [uid],
        )
    return query(
        f"""
        SELECT c.id, c.full_name, c.email, c.gender,
               r.id AS req_id, r.title AS requisition, a.status,
               a.combined_score, a.match_score, a.id AS app_id,
               rc_info.recruiter_id, rc_info.recruiter_name
        FROM candidate c
        JOIN application a ON a.candidate_id = c.id
        JOIN requisition r ON r.id = a.requisition_id
        {_rec_lat}
        ORDER BY a.applied_at DESC
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


# ─── CV Database (Admin / TA Manager / Recruiter) ────────────────────────────

@router.get("/cv-database")
def cv_database(user: dict = Depends(get_current_user)):
    role = user["role"]
    uid  = user["sub"]

    if role not in ("admin", "ta_manager", "recruiter"):
        raise HTTPException(403, "Not authorised to view CV database")

    # For recruiter, only show candidates from their requisitions
    if role == "recruiter":
        scope_where = """
            AND c.id IN (
                SELECT DISTINCT a_s.candidate_id
                FROM application a_s
                JOIN requisition_recruiter rr_s
                     ON rr_s.requisition_id = a_s.requisition_id
                     AND rr_s.recruiter_id = %s
            )
        """
        scope_params = [uid]
    else:
        scope_where  = ""
        scope_params = []

    summary = query_one(
        f"""
        SELECT
          COUNT(DISTINCT c.id)                                              AS total_candidates,
          COUNT(a.id)                                                       AS total_applications,
          COUNT(DISTINCT c.id) FILTER
            (WHERE c.resume_url IS NOT NULL AND c.resume_url <> '')         AS resumes_on_file,
          ROUND(AVG(a.combined_score)
            FILTER (WHERE a.combined_score IS NOT NULL)::numeric, 1)        AS avg_score,
          COUNT(DISTINCT c.id) FILTER (WHERE a.status = 'joined')           AS total_joined
        FROM candidate c
        LEFT JOIN application a ON a.candidate_id = c.id
        WHERE 1=1 {scope_where}
        """,
        scope_params,
    )

    candidates = query(
        f"""
        SELECT
          c.id, c.full_name, c.email, c.gender, c.source,
          c.resume_url,
          c.created_at                                                     AS registered_at,
          (SELECT COUNT(*) FROM application WHERE candidate_id = c.id)    AS total_applications,
          (SELECT r.title
           FROM application a2
           JOIN requisition r ON r.id = a2.requisition_id
           WHERE a2.candidate_id = c.id
           ORDER BY a2.applied_at DESC LIMIT 1)                           AS latest_position,
          (SELECT a3.status
           FROM application a3
           WHERE a3.candidate_id = c.id
           ORDER BY a3.applied_at DESC LIMIT 1)                           AS latest_status,
          (SELECT a4.combined_score
           FROM application a4
           WHERE a4.candidate_id = c.id
           ORDER BY a4.combined_score DESC NULLS LAST LIMIT 1)            AS best_score,
          (SELECT a5.bot_score
           FROM application a5
           WHERE a5.candidate_id = c.id
           ORDER BY a5.bot_score DESC NULLS LAST LIMIT 1)                 AS ai_score,
          (SELECT a6.match_score
           FROM application a6
           WHERE a6.candidate_id = c.id
           ORDER BY a6.match_score DESC NULLS LAST LIMIT 1)               AS match_score
        FROM candidate c
        WHERE 1=1 {scope_where}
        ORDER BY c.created_at DESC
        """,
        scope_params,
    )

    return {"summary": dict(summary) if summary else {}, "candidates": candidates}


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
