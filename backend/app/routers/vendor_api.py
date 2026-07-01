"""
Vendor Management API — sourcing partner companies and their portal.

Internal endpoints (recruiter / ta_manager / admin):
  register vendors, add users, open reqs, suspend access.

Portal endpoints (vendor JWT):
  see reqs opened to them, submit CVs, track submissions.

Auth split:
  - Internal routes  → get_current_user (staff JWT, role checked)
  - Portal routes    → get_current_vendor (vendor JWT with account_type='vendor')
  - Vendor login     → public (no JWT required — listed in main._PUBLIC)

Token flow reuses password_api._issue_token with account_type='vendor'
so the /set-password page works for vendor users without any new page.
"""
import os
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from ..auth_utils import (
    SECRET_KEY, ALGORITHM, get_current_user,
    hash_password, verify_password,
)
from ..db import query, query_one
from ..routers.password_api import issue_invite_for_external_user

router = APIRouter(prefix="/api/vendors", tags=["vendors"])

_INTERNAL_ROLES = {"admin", "ta_manager", "recruiter"}
_ALLOWED_RESUME = {".pdf", ".docx", ".doc"}
_bearer = HTTPBearer(auto_error=False)
_VENDOR_TOKEN_HOURS = 8


# ── Vendor JWT helpers ────────────────────────────────────────────────────────

def _create_vendor_token(vu: dict) -> str:
    expire = datetime.utcnow() + timedelta(hours=_VENDOR_TOKEN_HOURS)
    return jwt.encode(
        {
            "sub":          str(vu["id"]),
            "email":        vu["email"],
            "vendor_id":    str(vu["vendor_id"]),
            "name":         vu["full_name"],
            "account_type": "vendor",
            "exp":          expire,
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def get_current_vendor(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Dependency: resolve a vendor JWT → payload dict.
    Mirrors get_current_user but checks account_type='vendor'."""
    if not creds:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("account_type") != "vendor":
            raise HTTPException(403, "Vendor credentials required")
        return payload
    except HTTPException:
        raise
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")


def _require_internal(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") not in _INTERNAL_ROLES:
        raise HTTPException(403, "recruiter / ta_manager / admin only")
    return user


# ── Vendor login (public endpoint) ───────────────────────────────────────────

class VendorLoginIn(BaseModel):
    email: str
    password: str


@router.post("/portal/login")
def vendor_login(body: VendorLoginIn):
    """Public. Vendor user logs in; receives a short-lived JWT."""
    vu = query_one(
        """SELECT vu.id, vu.vendor_id, vu.email, vu.full_name,
                  vu.password_hash, vu.is_active
           FROM vendor_user vu
           JOIN vendor v ON v.id = vu.vendor_id
           WHERE LOWER(vu.email) = %s
             AND vu.is_active = TRUE
             AND v.status = 'active'""",
        [body.email.lower().strip()],
    )
    if not vu or not verify_password(body.password, vu.get("password_hash") or ""):
        raise HTTPException(401, "Invalid credentials")
    return {
        "token": _create_vendor_token(vu),
        "name":  vu["full_name"],
        "email": vu["email"],
    }


# ── Internal: register a new vendor + first user ─────────────────────────────

class RegisterVendorIn(BaseModel):
    name: str
    contact_email: str
    contact_phone: Optional[str] = None
    first_user_name: str
    first_user_email: str


@router.post("/")
def register_vendor(body: RegisterVendorIn, user: dict = Depends(_require_internal)):
    """Create a vendor company + its first login account; email the invite link."""
    existing = query_one(
        "SELECT id FROM vendor_user WHERE LOWER(email) = %s",
        [body.first_user_email.lower().strip()],
    )
    if existing:
        raise HTTPException(409, "A vendor user with that email already exists")

    vendor = query_one(
        """INSERT INTO vendor (name, contact_email, contact_phone, created_by)
           VALUES (%s, %s, %s, %s) RETURNING id, name""",
        [body.name, body.contact_email, body.contact_phone, user["sub"]],
    )

    vu = query_one(
        """INSERT INTO vendor_user (vendor_id, full_name, email)
           VALUES (%s, %s, %s) RETURNING id, email, full_name""",
        [str(vendor["id"]), body.first_user_name,
         body.first_user_email.lower().strip()],
    )

    invite_link = None
    try:
        raw = issue_invite_for_external_user(
            str(vu["id"]), vu["email"], vu["full_name"], "vendor"
        )
        from ..routers.password_api import _base_url
        invite_link = f"{_base_url()}/set-password?token={raw}"
    except Exception as exc:
        print(f"[vendor] Invite email failed for {vu['email']}: {exc}")

    return {
        "vendor_id":   str(vendor["id"]),
        "vendor_name": vendor["name"],
        "user_id":     str(vu["id"]),
        "invite_link": invite_link,
    }


# ── Internal: add another login under an existing vendor ─────────────────────

class AddVendorUserIn(BaseModel):
    full_name: str
    email: str


@router.post("/{vendor_id}/users")
def add_vendor_user(
    vendor_id: str,
    body: AddVendorUserIn,
    user: dict = Depends(_require_internal),
):
    vendor = query_one(
        "SELECT id FROM vendor WHERE id=%s AND status='active'", [vendor_id]
    )
    if not vendor:
        raise HTTPException(404, "Vendor not found or suspended")

    existing = query_one(
        "SELECT id FROM vendor_user WHERE LOWER(email) = %s",
        [body.email.lower().strip()],
    )
    if existing:
        raise HTTPException(409, "A vendor user with that email already exists")

    vu = query_one(
        """INSERT INTO vendor_user (vendor_id, full_name, email)
           VALUES (%s, %s, %s) RETURNING id, email, full_name""",
        [vendor_id, body.full_name, body.email.lower().strip()],
    )

    invite_link = None
    try:
        raw = issue_invite_for_external_user(
            str(vu["id"]), vu["email"], vu["full_name"], "vendor"
        )
        from ..routers.password_api import _base_url
        invite_link = f"{_base_url()}/set-password?token={raw}"
    except Exception as exc:
        print(f"[vendor] Invite email failed for {vu['email']}: {exc}")

    return {"user_id": str(vu["id"]), "invite_link": invite_link}


# ── Internal: list all vendors ────────────────────────────────────────────────

@router.get("/")
def list_vendors(user: dict = Depends(_require_internal)):
    return query(
        """SELECT v.id, v.name, v.contact_email, v.contact_phone,
                  v.status, v.created_at,
                  COUNT(vu.id) FILTER (WHERE vu.is_active) AS user_count
           FROM vendor v
           LEFT JOIN vendor_user vu ON vu.vendor_id = v.id
           GROUP BY v.id
           ORDER BY v.created_at DESC"""
    )


# ── Internal: suspend or reactivate a vendor ─────────────────────────────────

class PatchVendorIn(BaseModel):
    status: str


@router.patch("/{vendor_id}")
def patch_vendor(
    vendor_id: str,
    body: PatchVendorIn,
    user: dict = Depends(_require_internal),
):
    if body.status not in ("active", "suspended"):
        raise HTTPException(400, "status must be 'active' or 'suspended'")
    if not query_one("SELECT id FROM vendor WHERE id=%s", [vendor_id]):
        raise HTTPException(404, "Vendor not found")
    query(
        "UPDATE vendor SET status=%s WHERE id=%s",
        [body.status, vendor_id], fetch=False,
    )
    return {"ok": True, "status": body.status}


# ── Internal: open a requisition to one or more vendors ──────────────────────

class OpenReqIn(BaseModel):
    vendor_ids: List[str]


@router.post("/requisitions/{req_id}/open")
def open_req_to_vendors(
    req_id: str,
    body: OpenReqIn,
    user: dict = Depends(_require_internal),
):
    if not query_one("SELECT id FROM requisition WHERE id=%s", [req_id]):
        raise HTTPException(404, "Requisition not found")
    opened = []
    for vid in body.vendor_ids:
        if not query_one("SELECT id FROM vendor WHERE id=%s AND status='active'", [vid]):
            continue
        query(
            """INSERT INTO requisition_vendor (requisition_id, vendor_id, opened_by)
               VALUES (%s, %s, %s) ON CONFLICT (requisition_id, vendor_id) DO NOTHING""",
            [req_id, vid, user["sub"]], fetch=False,
        )
        opened.append(vid)
    return {"ok": True, "opened": opened}


# ── Internal: remove vendor access to a req ──────────────────────────────────

@router.delete("/requisitions/{req_id}/vendors/{vendor_id}")
def close_req_vendor(
    req_id: str,
    vendor_id: str,
    user: dict = Depends(_require_internal),
):
    query(
        "DELETE FROM requisition_vendor WHERE requisition_id=%s AND vendor_id=%s",
        [req_id, vendor_id], fetch=False,
    )
    return {"ok": True}


# ── Portal: list reqs opened to this vendor ───────────────────────────────────

@router.get("/portal/requisitions")
def portal_list_reqs(vendor: dict = Depends(get_current_vendor)):
    vid = vendor["vendor_id"]
    source_tag = f"vendor:{vid}"
    return query(
        """SELECT r.id, r.title, r.status, r.roll_type,
                  r.hiring_location, r.min_experience, r.max_experience,
                  r.key_skills, b.code AS band, bu.name AS business_unit,
                  rv.opened_at,
                  (SELECT COUNT(*) FROM application a
                   WHERE a.requisition_id = r.id
                     AND a.source = %s) AS my_submissions
           FROM requisition_vendor rv
           JOIN requisition r  ON r.id  = rv.requisition_id
           JOIN band b         ON b.id  = r.band_id
           JOIN business_unit bu ON bu.id = r.bu_id
           WHERE rv.vendor_id = %s
             AND r.status = 'open'
           ORDER BY rv.opened_at DESC""",
        [source_tag, vid],
    )


# ── Portal: submit a CV to an opened req ─────────────────────────────────────

@router.post("/portal/requisitions/{req_id}/submit")
async def portal_submit_cv(
    req_id: str,
    full_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    gender: str = Form("undisclosed"),
    years_experience: float = Form(None),
    file: UploadFile = File(...),
    vendor: dict = Depends(get_current_vendor),
):
    """
    Vendor uploads a candidate CV.  Creates candidate + application with
    source = 'vendor:<id>'.  Enters the standard intake_and_screen pipeline.
    """
    vid = vendor["vendor_id"]

    # Enforce: this req must be opened to the calling vendor
    if not query_one(
        "SELECT id FROM requisition_vendor WHERE requisition_id=%s AND vendor_id=%s",
        [req_id, vid],
    ):
        raise HTTPException(403, "This requisition is not opened to your vendor")

    req = query_one(
        "SELECT id, approval_status FROM requisition WHERE id=%s", [req_id]
    )
    if not req:
        raise HTTPException(404, "Requisition not found")
    if (req.get("approval_status") or "approved") != "approved":
        raise HTTPException(403, "Requisition is not open for applications")

    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix not in _ALLOWED_RESUME:
        raise HTTPException(400, f"Unsupported file type '{suffix}'. Upload PDF or Word.")

    file_bytes = await file.read()
    try:
        from ..services.resume_parser import extract_text as _parse_resume
        resume_text, _ = _parse_resume(file_bytes, file.filename or "")
    except NotImplementedError:
        raise HTTPException(422, "Image files are not supported; upload PDF or Word.")

    source_tag = f"vendor:{vid}"

    # Dedup or create candidate
    from ..services.resume_parser import normalize_phone
    norm_phone = normalize_phone(phone) if phone else None

    existing_cand = query_one(
        "SELECT id, full_name FROM candidate WHERE LOWER(email) = %s",
        [email.lower().strip()],
    )
    if existing_cand:
        cand_id = str(existing_cand["id"])
        if query_one(
            "SELECT id FROM application WHERE requisition_id=%s AND candidate_id=%s",
            [req_id, cand_id],
        ):
            raise HTTPException(
                409,
                f"Candidate '{existing_cand['full_name']}' has already applied to this requisition",
            )
    else:
        new_cand = query_one(
            """INSERT INTO candidate (full_name, email, phone, gender, source)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            [full_name, email.lower().strip(), norm_phone, gender, source_tag],
        )
        cand_id = str(new_cand["id"])

    # Enter the standard pipeline
    from ..services.pipeline import intake_and_screen
    app_row = intake_and_screen(req_id, cand_id, resume_text, years_experience, len(file_bytes))

    # Tag the application source (vendor:<id>)
    query(
        "UPDATE application SET source=%s WHERE id=%s",
        [source_tag, str(app_row["id"])], fetch=False,
    )

    # Ingest into CV repository (non-blocking on failure)
    try:
        from ..routers.cv_api import ingest_and_link
        ingest_and_link(
            data=file_bytes,
            filename=file.filename or f"vendor_{cand_id}.pdf",
            source="application",
            uploaded_by=None,
            candidate_id=cand_id,
            req_id=req_id,
        )
    except Exception as exc:
        print(f"[vendor-submit] CV repository ingest failed: {exc}")

    return {
        "application_id": str(app_row["id"]),
        "candidate_id":   cand_id,
        "source":         source_tag,
        "match_score":    app_row.get("match_score"),
    }


# ── Portal: vendor sees status of their own submissions ──────────────────────

@router.get("/portal/submissions")
def portal_submissions(vendor: dict = Depends(get_current_vendor)):
    vid = vendor["vendor_id"]
    source_tag = f"vendor:{vid}"
    return query(
        """SELECT a.id AS application_id,
                  c.full_name   AS candidate_name,
                  c.email       AS candidate_email,
                  r.id          AS requisition_id,
                  r.title       AS requisition_title,
                  a.status,
                  a.applied_at,
                  a.source
           FROM application a
           JOIN candidate   c ON c.id = a.candidate_id
           JOIN requisition r ON r.id = a.requisition_id
           WHERE a.source = %s
           ORDER BY a.applied_at DESC""",
        [source_tag],
    )
