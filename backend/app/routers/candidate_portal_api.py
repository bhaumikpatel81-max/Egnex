"""
Candidate Portal API.

All portal routes are gated to candidate JWT (account_type='candidate').
The TA feedback route is gated to ta_manager / admin.

HARD INVARIANT: the portal NEVER exposes any score column.
Queries that touch the application table from portal routes must
return only status + human-readable stage label. No match_score,
ai_fit_score, bot_score, combined_score, score_breakdown,
stability_score, or ai_screen_detail — ever.

Candidate login: POST /api/candidate/portal/login (public — listed in main._PUBLIC)
"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from ..auth_utils import (
    SECRET_KEY, ALGORITHM, get_current_user,
    hash_password, verify_password,
)
from ..db import query, query_one
from ..routers.password_api import issue_invite_for_external_user
from ..services.sla import PIPELINE_STAGE_LABELS
from ..services.pipeline import intake_and_screen
from ..services.screening import KEYWORD_ALIASES

router = APIRouter(prefix="/api/candidate", tags=["candidate-portal"])

_TA_ROLES = {"ta_manager", "admin"}
_BEARER   = HTTPBearer(auto_error=False)
_TTL_HOURS = 8

# Human-readable stage labels (mirrors sla.PIPELINE_STAGE_LABELS but safe to extend)
_STAGE_LABELS = {
    **PIPELINE_STAGE_LABELS,
    "applied":       "Applied",
    "nexai_bot":     "NexAI Interview",
    "hired":         "Offer Accepted",
    "offered":       "Offer Received",
    "rejected":      "Not Progressing",
    "on_hold":       "On Hold",
    "documentation": "Documentation Review",
}


# ── Candidate JWT helpers ─────────────────────────────────────────────────────

def _create_candidate_token(cu: dict) -> str:
    expire = datetime.utcnow() + timedelta(hours=_TTL_HOURS)
    return jwt.encode(
        {
            "sub":          str(cu["id"]),
            "email":        cu["email"],
            "candidate_id": str(cu["candidate_id"]),
            "account_type": "candidate",
            "exp":          expire,
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def get_current_candidate(
    creds: HTTPAuthorizationCredentials | None = Depends(_BEARER),
) -> dict:
    """Dependency: resolve a candidate JWT. Mirrors get_current_vendor."""
    if not creds:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("account_type") != "candidate":
            raise HTTPException(403, "Candidate credentials required")
        return payload
    except HTTPException:
        raise
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")


def _require_ta(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") not in _TA_ROLES:
        raise HTTPException(403, "ta_manager / admin only")
    return user


# ── Public: candidate login ───────────────────────────────────────────────────

class CandidateLoginIn(BaseModel):
    email: str
    password: str


@router.post("/portal/login")
def candidate_login(body: CandidateLoginIn):
    """Public. Candidate logs in; receives a short-lived JWT."""
    cu = query_one(
        """SELECT cu.id, cu.candidate_id, cu.email, cu.password_hash, cu.is_active,
                  c.full_name
           FROM candidate_user cu
           JOIN candidate c ON c.id = cu.candidate_id
           WHERE LOWER(cu.email) = %s AND cu.is_active = TRUE""",
        [body.email.lower().strip()],
    )
    if not cu or not verify_password(body.password, cu.get("password_hash") or ""):
        raise HTTPException(401, "Invalid credentials")
    return {
        "token": _create_candidate_token(cu),
        "name":  cu["full_name"],
        "email": cu["email"],
    }


# ── Portal: candidate's own applications — NO SCORES EVER ────────────────────

@router.get("/portal/applications")
def portal_applications(candidate: dict = Depends(get_current_candidate)):
    cand_id = candidate["candidate_id"]
    # INVARIANT: never return any score column from this endpoint.
    rows = query(
        """SELECT a.id, a.status, a.applied_at, a.source,
                  r.title AS job_title, r.hiring_location,
                  b.code  AS band
           FROM application a
           JOIN requisition r ON r.id = a.requisition_id
           JOIN band b        ON b.id = r.band_id
           WHERE a.candidate_id = %s
           ORDER BY a.applied_at DESC""",
        [cand_id],
    )
    return [
        {
            "application_id": str(r["id"]),
            "job_title":      r["job_title"],
            "hiring_location": r["hiring_location"],
            "band":           r["band"],
            "status":         r["status"],
            "stage_label":    _STAGE_LABELS.get(r["status"], r["status"].replace("_", " ").title()),
            "applied_at":     r["applied_at"],
        }
        for r in rows
    ]


# ── Portal: recommended roles by skill overlap — no numeric score ─────────────

def _candidate_skills(candidate_id: str) -> list[str]:
    """Return the candidate's skills array from their latest CV in the repository."""
    row = query_one(
        """SELECT cr.skills
           FROM cv_repository cr
           JOIN candidate c ON c.cv_repository_id = cr.id
           WHERE c.id = %s""",
        [candidate_id],
    )
    if row and row.get("skills"):
        return [s.lower() for s in row["skills"] if s]
    return []


def _skill_match_reason(req_skills_text: str, candidate_skills: list[str]) -> tuple[int, str]:
    """
    Compute overlap between req key_skills and candidate's skill list.
    Returns (match_count, human-readable reason string).
    No numeric score is returned to the candidate — only the reason text.
    """
    if not req_skills_text or not candidate_skills:
        return 0, ""
    req_lower = req_skills_text.lower()
    matched = []
    for cand_skill in candidate_skills:
        # Check canonical key and all aliases
        for canonical, aliases in KEYWORD_ALIASES.items():
            if cand_skill in aliases or cand_skill == canonical:
                if any(alias in req_lower for alias in aliases) or canonical in req_lower:
                    if canonical not in matched:
                        matched.append(canonical)
                    break
        else:
            # Direct substring match
            if cand_skill in req_lower and cand_skill not in matched:
                matched.append(cand_skill)
    count = len(matched)
    if matched:
        readable = ", ".join(s.title() for s in matched[:4])
        if len(matched) > 4:
            readable += f" and {len(matched)-4} more"
        reason = f"Matches your skills: {readable}"
    else:
        reason = "Explore this opportunity"
    return count, reason


@router.get("/portal/recommended")
def portal_recommended(candidate: dict = Depends(get_current_candidate)):
    """
    Open reqs ranked by skill overlap with the candidate's profile.
    Returns role + match-reason text — NO numeric score exposed.
    """
    cand_id = candidate["candidate_id"]
    cand_skills = _candidate_skills(cand_id)

    # Already applied reqs — exclude from recommendations
    applied_ids = {
        str(r["requisition_id"])
        for r in query(
            "SELECT requisition_id FROM application WHERE candidate_id=%s", [cand_id]
        )
    }

    open_reqs = query(
        """SELECT r.id, r.title, r.hiring_location, r.min_experience, r.max_experience,
                  r.key_skills, b.code AS band, bu.name AS business_unit
           FROM requisition r
           JOIN band b ON b.id = r.band_id
           JOIN business_unit bu ON bu.id = r.bu_id
           WHERE r.status = 'open'
             AND COALESCE(r.approval_status, 'approved') = 'approved'
           ORDER BY r.created_at DESC""",
    )

    results = []
    for req in open_reqs:
        if str(req["id"]) in applied_ids:
            continue
        match_count, reason = _skill_match_reason(req.get("key_skills") or "", cand_skills)
        results.append({
            "requisition_id":  str(req["id"]),
            "title":           req["title"],
            "hiring_location": req["hiring_location"],
            "band":            req["band"],
            "business_unit":   req["business_unit"],
            "min_experience":  req["min_experience"],
            "max_experience":  req["max_experience"],
            "match_reason":    reason,
            "_sort_key":       match_count,
        })

    results.sort(key=lambda x: x.pop("_sort_key"), reverse=True)
    return results


# ── Portal: one-click apply ───────────────────────────────────────────────────

@router.post("/portal/apply/{req_id}")
def portal_apply(req_id: str, candidate: dict = Depends(get_current_candidate)):
    """
    Idempotent one-click apply.  No duplicate application allowed for the
    same candidate + req (application table has UNIQUE on those two columns).
    """
    cand_id = candidate["candidate_id"]

    req = query_one(
        "SELECT id, approval_status FROM requisition WHERE id=%s", [req_id]
    )
    if not req:
        raise HTTPException(404, "Requisition not found")
    if (req.get("approval_status") or "approved") != "approved":
        raise HTTPException(403, "This requisition is not open for applications yet")

    # Idempotency: if already applied, return existing application
    existing = query_one(
        "SELECT id, status FROM application WHERE requisition_id=%s AND candidate_id=%s",
        [req_id, cand_id],
    )
    if existing:
        return {
            "application_id": str(existing["id"]),
            "status":         existing["status"],
            "already_applied": True,
        }

    # Get candidate's latest resume text for screening
    cv_row = query_one(
        """SELECT cr.raw_text, cr.experience_years
           FROM cv_repository cr
           JOIN candidate c ON c.cv_repository_id = cr.id
           WHERE c.id = %s""",
        [cand_id],
    )
    resume_text = (cv_row or {}).get("raw_text") or ""
    years_exp   = (cv_row or {}).get("experience_years")

    app_row = intake_and_screen(req_id, cand_id, resume_text, years_exp)

    # Tag source
    query(
        "UPDATE application SET source='career_site' WHERE id=%s",
        [str(app_row["id"])], fetch=False,
    )
    return {
        "application_id": str(app_row["id"]),
        "status":         app_row.get("status"),
        "already_applied": False,
    }


# ── Portal: submit feedback ───────────────────────────────────────────────────

class FeedbackIn(BaseModel):
    company_rating:   int
    interview_rating: int
    comments:         Optional[str] = None
    application_id:   Optional[str] = None


@router.post("/portal/feedback")
def portal_submit_feedback(body: FeedbackIn, candidate: dict = Depends(get_current_candidate)):
    cand_id = candidate["candidate_id"]
    if not (1 <= body.company_rating <= 5):
        raise HTTPException(400, "company_rating must be 1–5")
    if not (1 <= body.interview_rating <= 5):
        raise HTTPException(400, "interview_rating must be 1–5")
    if body.application_id:
        app = query_one(
            "SELECT id FROM application WHERE id=%s AND candidate_id=%s",
            [body.application_id, cand_id],
        )
        if not app:
            raise HTTPException(403, "Application not found or does not belong to you")
    row = query_one(
        """INSERT INTO candidate_feedback
               (candidate_id, application_id, company_rating, interview_rating, comments)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        [cand_id, body.application_id, body.company_rating, body.interview_rating, body.comments],
    )
    return {"ok": True, "feedback_id": str(row["id"])}


# ── Portal: candidate sees their own feedback ─────────────────────────────────

@router.get("/portal/feedback")
def portal_my_feedback(candidate: dict = Depends(get_current_candidate)):
    cand_id = candidate["candidate_id"]
    return query(
        """SELECT cf.id, cf.company_rating, cf.interview_rating, cf.comments,
                  cf.submitted_at, r.title AS job_title
           FROM candidate_feedback cf
           LEFT JOIN application a  ON a.id  = cf.application_id
           LEFT JOIN requisition r  ON r.id  = a.requisition_id
           WHERE cf.candidate_id = %s
           ORDER BY cf.submitted_at DESC""",
        [cand_id],
    )


# ── TA route: all candidate feedback (experience dashboard) ──────────────────

@router.get("/feedback")  # mounted at /api/candidate/feedback
def ta_all_feedback(
    company_rating: Optional[int]  = None,
    req_id:         Optional[str]  = None,
    date_from:      Optional[str]  = None,
    date_to:        Optional[str]  = None,
    user: dict = Depends(_require_ta),
):
    """TA-only: all candidate experience feedback, filterable."""
    conditions = ["1=1"]
    params: list = []
    if company_rating:
        conditions.append("cf.company_rating = %s"); params.append(company_rating)
    if req_id:
        conditions.append("a.requisition_id = %s"); params.append(req_id)
    if date_from:
        conditions.append("cf.submitted_at >= %s"); params.append(date_from)
    if date_to:
        conditions.append("cf.submitted_at <= %s"); params.append(date_to)

    where = " AND ".join(conditions)
    return query(
        f"""SELECT cf.id, c.full_name AS candidate_name, c.email AS candidate_email,
                   cf.company_rating, cf.interview_rating, cf.comments, cf.submitted_at,
                   r.title AS job_title
            FROM candidate_feedback cf
            JOIN candidate c ON c.id = cf.candidate_id
            LEFT JOIN application a ON a.id = cf.application_id
            LEFT JOIN requisition r ON r.id = a.requisition_id
            WHERE {where}
            ORDER BY cf.submitted_at DESC""",
        params or None,
    )
