"""
Band management API (TA admin).

Bands are now editable: create, rename, reorder, soft-delete.
Scoring uses the requisition.criticality FLAG (not band codes) so
renaming or removing a band never corrupts gamification points.

GET    /api/bands/all       all bands incl. inactive (admin view)
GET    /api/bands           active bands only (used by req form dropdowns — existing endpoint)
POST   /api/bands           create a new band
PATCH  /api/bands/{id}      rename / reorder / reactivate
DELETE /api/bands/{id}      soft-delete (is_active=false); refuses if referenced by open reqs
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth_utils import get_current_user
from ..db import query, query_one

router = APIRouter(prefix="/api/bands", tags=["bands"])

_ADMIN_ROLES = {"admin", "ta_manager"}


def _require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") not in _ADMIN_ROLES:
        raise HTTPException(403, "ta_manager / admin only")
    return user


# ── List all bands (admin — includes inactive) ────────────────────────────────

@router.get("/all")
def list_all_bands(user: dict = Depends(_require_admin)):
    return query("SELECT id, code, rank, description, is_active FROM band ORDER BY rank")


# ── Create a new band ─────────────────────────────────────────────────────────

class CreateBandIn(BaseModel):
    code:        str
    rank:        int
    description: Optional[str] = None


@router.post("/")
def create_band(body: CreateBandIn, user: dict = Depends(_require_admin)):
    existing = query_one("SELECT id FROM band WHERE UPPER(code) = UPPER(%s)", [body.code])
    if existing:
        raise HTTPException(409, f"Band code '{body.code}' already exists")
    row = query_one(
        """INSERT INTO band (code, rank, description)
           VALUES (%s, %s, %s) RETURNING id, code, rank, description, is_active""",
        [body.code.upper(), body.rank, body.description],
    )
    return dict(row)


# ── Patch band (rename / reorder / reactivate) ────────────────────────────────

class PatchBandIn(BaseModel):
    code:        Optional[str] = None
    rank:        Optional[int] = None
    description: Optional[str] = None
    is_active:   Optional[bool] = None


@router.patch("/{band_id}")
def patch_band(band_id: str, body: PatchBandIn, user: dict = Depends(_require_admin)):
    band = query_one("SELECT id, code FROM band WHERE id=%s", [band_id])
    if not band:
        raise HTTPException(404, "Band not found")

    sets, vals = [], []
    if body.code is not None:
        dup = query_one(
            "SELECT id FROM band WHERE UPPER(code)=UPPER(%s) AND id<>%s",
            [body.code, band_id],
        )
        if dup:
            raise HTTPException(409, f"Band code '{body.code}' already exists")
        sets.append("code=%s"); vals.append(body.code.upper())
    if body.rank is not None:
        sets.append("rank=%s"); vals.append(body.rank)
    if body.description is not None:
        sets.append("description=%s"); vals.append(body.description)
    if body.is_active is not None:
        sets.append("is_active=%s"); vals.append(body.is_active)

    if not sets:
        return query_one("SELECT id, code, rank, description, is_active FROM band WHERE id=%s", [band_id])

    vals.append(band_id)
    query(f"UPDATE band SET {', '.join(sets)} WHERE id=%s", vals, fetch=False)
    return query_one("SELECT id, code, rank, description, is_active FROM band WHERE id=%s", [band_id])


# ── Soft-delete a band ────────────────────────────────────────────────────────

@router.delete("/{band_id}")
def delete_band(band_id: str, user: dict = Depends(_require_admin)):
    band = query_one("SELECT id, code FROM band WHERE id=%s", [band_id])
    if not band:
        raise HTTPException(404, "Band not found")

    # Refuse hard-delete; soft-delete only. If open reqs reference this band, just deactivate.
    open_req_count = query_one(
        "SELECT COUNT(*) AS n FROM requisition WHERE band_id=%s AND status='open'",
        [band_id],
    )
    if open_req_count and open_req_count["n"] > 0:
        # Soft-deactivate — still referenced
        query("UPDATE band SET is_active=FALSE WHERE id=%s", [band_id], fetch=False)
        return {
            "ok": True,
            "action": "deactivated",
            "note": f"Band has {open_req_count['n']} open requisition(s) — deactivated rather than deleted",
        }

    query("UPDATE band SET is_active=FALSE WHERE id=%s", [band_id], fetch=False)
    return {"ok": True, "action": "deactivated"}
