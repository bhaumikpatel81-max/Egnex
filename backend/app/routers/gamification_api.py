"""
Gamification API.

GET /api/gamification/me          — caller's own points/tier/badges/rank (all authenticated roles)
GET /api/gamification/leaderboard — ta_manager/admin only; full per-persona board
GET /api/gamification/config      — ta_manager/admin only; read config
PATCH /api/gamification/config    — ta_manager/admin only; edit base_points / multipliers / thresholds
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth_utils import get_current_user
from ..db import query, query_one
from ..services.gamification import get_profile, score_for, tier_for, rank_for

router = APIRouter(prefix="/api/gamification", tags=["gamification"])

_TA_ROLES = {"ta_manager", "admin"}


def _subject_for(user: dict) -> tuple[str, str]:
    """Map a staff JWT payload → (subject_type, subject_id) for the leaderboard."""
    role = user.get("role", "")
    uid  = user["sub"]
    if role in ("ta_manager", "recruiter", "admin"):
        return "recruiter", uid
    if role == "hiring_manager":
        return "hm", uid
    return "recruiter", uid


# ── /me — every authenticated user ────────────────────────────────────────────

@router.get("/me")
def my_profile(period: str = "all", user: dict = Depends(get_current_user)):
    """Returns the caller's own gamification profile (points, tier, badges, rank)."""
    subject_type, subject_id = _subject_for(user)
    return get_profile(subject_type, subject_id, period)


# ── /leaderboard — TA managers and admins only ────────────────────────────────

@router.get("/leaderboard")
def leaderboard(
    period: str          = "all",
    subject_type: str    = "recruiter",
    user: dict           = Depends(get_current_user),
):
    """
    Full leaderboard for a persona. ta_manager/admin only.
    Any other role gets a 403 — they use /me for their own rank.
    """
    if user.get("role") not in _TA_ROLES:
        raise HTTPException(403, "Full leaderboard is visible to ta_manager / admin only. Use /me for your own rank.")

    if subject_type not in ("recruiter", "vendor", "candidate", "hm"):
        raise HTTPException(400, "subject_type must be recruiter|vendor|candidate|hm")

    period_filter = {
        "month":   "AND created_at >= date_trunc('month', now())",
        "quarter": "AND created_at >= date_trunc('quarter', now())",
        "ytd":     "AND created_at >= date_trunc('year', now())",
        "all":     "",
    }.get(period, "")

    rows = query(
        f"""SELECT subject_id,
                   SUM(points_awarded) AS total_points,
                   COUNT(*)            AS event_count
            FROM gamification_event
            WHERE subject_type=%s {period_filter}
            GROUP BY subject_id
            ORDER BY total_points DESC
            LIMIT 50""",
        [subject_type],
    )

    result = []
    for i, r in enumerate(rows or [], start=1):
        sid    = str(r["subject_id"])
        points = float(r["total_points"])
        # Look up display name
        name = _resolve_name(subject_type, sid)
        badges = query(
            "SELECT badge_key FROM gamification_badge WHERE subject_type=%s AND subject_id=%s",
            [subject_type, sid],
        )
        result.append({
            "rank":        i,
            "subject_id":  sid,
            "name":        name,
            "points":      points,
            "tier":        tier_for(points),
            "event_count": int(r["event_count"]),
            "badge_count": len(badges or []),
        })
    return result


def _resolve_name(subject_type: str, subject_id: str) -> str:
    if subject_type in ("recruiter", "hm"):
        row = query_one("SELECT full_name FROM app_user WHERE id=%s", [subject_id])
    elif subject_type == "vendor":
        row = query_one("SELECT full_name FROM vendor_user WHERE id=%s", [subject_id])
    elif subject_type == "candidate":
        row = query_one(
            """SELECT c.full_name FROM candidate_user cu
               JOIN candidate c ON c.id = cu.candidate_id
               WHERE cu.id=%s""",
            [subject_id],
        )
    else:
        row = None
    return (row or {}).get("full_name", "Unknown")


# ── Config read/write (TA admin) ──────────────────────────────────────────────

@router.get("/config")
def get_config(user: dict = Depends(get_current_user)):
    if user.get("role") not in _TA_ROLES:
        raise HTTPException(403, "ta_manager / admin only")
    return query("SELECT key, value, updated_at FROM gamification_config ORDER BY key")


class ConfigPatchIn(BaseModel):
    key:   str
    value: str


@router.patch("/config")
def patch_config(body: ConfigPatchIn, user: dict = Depends(get_current_user)):
    if user.get("role") not in _TA_ROLES:
        raise HTTPException(403, "ta_manager / admin only")
    existing = query_one("SELECT key FROM gamification_config WHERE key=%s", [body.key])
    if not existing:
        raise HTTPException(404, f"Config key '{body.key}' not found")
    query(
        """UPDATE gamification_config SET value=%s, updated_at=now(), updated_by=%s
           WHERE key=%s""",
        [body.value, user["sub"], body.key], fetch=False,
    )
    return {"ok": True, "key": body.key, "value": body.value}
