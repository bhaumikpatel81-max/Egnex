"""
Campus Bulk Upload — batch invite flow for freshers / campus drives.

Endpoints (all scoped to TA + Admin roles except two public session endpoints):
  POST /api/campus/upload                      — parse Excel, create batch + candidates
  GET  /api/campus/batch/{batch_id}            — paginated candidate list
  POST /api/campus/batch/{batch_id}/invite     — bulk invite selected candidates
  GET  /api/campus/batches                     — list batches for a requisition
  POST /api/campus/session/{token}/resume      — PUBLIC: candidate resume upload during NexAI
  GET  /api/campus/session/{token}/is-campus   — PUBLIC: does this token belong to a campus batch?
"""
import io
import json
import os
import re
import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import get_current_user
from ..services.connectors import send_email
from ..services.email_templates import render_template as _render_email_tmpl
from ..services.resume_parser import extract_text as _parse_resume
from ..services import pipeline as _pipeline_svc
from ..services import prerender as _prerender_svc

router = APIRouter(prefix="/api/campus", tags=["campus"])

# ── Column name normalisation ─────────────────────────────────────────────────

_COL_ALIASES: dict[str, list[str]] = {
    "name":            ["name", "full name", "candidate name", "student name"],
    "email":           ["email", "email address", "e-mail", "mail"],
    "phone":           ["phone", "mobile", "contact", "phone number",
                        "mobile number", "contact number"],
    "college":         ["college", "university", "institution",
                        "college name", "university name"],
    "branch":          ["branch", "degree", "course", "department",
                        "programme", "program"],
    "cgpa":            ["cgpa", "percentage", "marks", "gpa",
                        "score", "aggregate"],
    "graduation_year": ["graduation year", "passout year", "batch",
                        "pass out year", "year of graduation",
                        "year of passing"],
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalise_headers(raw_headers: list[str]) -> dict[str, str]:
    """Return {canonical_field: original_header} for recognised columns."""
    result: dict[str, str] = {}
    lower = [h.lower().strip() for h in raw_headers]
    for canonical, aliases in _COL_ALIASES.items():
        for i, h in enumerate(lower):
            if h in aliases:
                result[canonical] = raw_headers[i]
                break
    return result


def _valid_email(v) -> bool:
    return bool(v and _EMAIL_RE.match(str(v).strip()))


def _cell(row, headers: list[str], col_name: str | None):
    """Safe column read."""
    if col_name is None:
        return None
    try:
        idx = headers.index(col_name)
        v = row[idx]
        return str(v).strip() if v is not None else None
    except (ValueError, IndexError):
        return None


# ── Upload ────────────────────────────────────────────────────────────────────

@router.post("/upload", status_code=201)
async def upload_excel(
    file: UploadFile = File(...),
    requisition_id: str = Query(...),
    user: dict = Depends(get_current_user),
):
    """Parse an .xlsx/.xls file and create a campus_upload_batch with campus_candidate rows."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Only .xlsx or .xls files are accepted")

    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "File too large — maximum 10 MB")

    if not query_one("SELECT id FROM requisition WHERE id=%s", [requisition_id]):
        raise HTTPException(404, "Requisition not found")

    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        ws = wb.active
        raw_rows = list(ws.iter_rows(values_only=True))
        wb.close()
    except Exception as exc:
        raise HTTPException(400, f"Cannot parse Excel file: {exc}")

    if not raw_rows:
        raise HTTPException(400, "Excel file is empty")

    raw_headers = [str(h) if h is not None else "" for h in raw_rows[0]]
    mapping = _normalise_headers(raw_headers)
    canonical_header_set = set(mapping.values())

    # Indices of unrecognised columns → go into extra_data
    extra_indices = [
        i for i, h in enumerate(raw_headers)
        if h and h not in canonical_header_set
    ]

    total_rows = 0
    skipped_rows = 0
    valid_candidates: list[dict] = []

    for raw_row in raw_rows[1:]:
        # Skip blank rows
        if all(v is None or str(v).strip() == "" for v in raw_row):
            continue
        total_rows += 1

        email_raw = _cell(raw_row, raw_headers, mapping.get("email"))
        if not _valid_email(email_raw):
            skipped_rows += 1
            continue

        # CGPA
        cgpa = None
        cgpa_raw = _cell(raw_row, raw_headers, mapping.get("cgpa"))
        if cgpa_raw:
            try:
                cgpa = round(float(cgpa_raw.replace("%", "")), 2)
            except (ValueError, AttributeError):
                pass

        # Graduation year
        grad_year = None
        gy_raw = _cell(raw_row, raw_headers, mapping.get("graduation_year"))
        if gy_raw:
            try:
                grad_year = int(float(gy_raw))
            except (ValueError, TypeError):
                pass

        # Extra columns
        extra_data: dict = {}
        for i in extra_indices:
            if i < len(raw_row) and raw_row[i] is not None:
                extra_data[raw_headers[i]] = str(raw_row[i]).strip()

        valid_candidates.append({
            "name":            _cell(raw_row, raw_headers, mapping.get("name")),
            "email":           email_raw.strip().lower(),
            "phone":           _cell(raw_row, raw_headers, mapping.get("phone")),
            "college":         _cell(raw_row, raw_headers, mapping.get("college")),
            "branch":          _cell(raw_row, raw_headers, mapping.get("branch")),
            "cgpa":            cgpa,
            "graduation_year": grad_year,
            "extra_data":      extra_data,
        })

    if not valid_candidates:
        raise HTTPException(
            400,
            "No valid candidates found — all rows have blank or invalid email addresses",
        )

    # Persist batch
    batch_row = query_one(
        """INSERT INTO campus_upload_batch
               (requisition_id, uploaded_by, file_name, total_rows)
           VALUES (%s, %s, %s, %s) RETURNING id""",
        [requisition_id, user["sub"], file.filename, len(valid_candidates)],
    )
    batch_id = str(batch_row["id"])

    for c in valid_candidates:
        query(
            """INSERT INTO campus_candidate
                   (batch_id, requisition_id, name, email, phone,
                    college, branch, cgpa, graduation_year, extra_data)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
            [
                batch_id, requisition_id,
                c["name"], c["email"], c["phone"],
                c["college"], c["branch"], c["cgpa"],
                c["graduation_year"], json.dumps(c["extra_data"]),
            ],
            fetch=False,
        )

    detected = {k: (k in mapping) for k in _COL_ALIASES}
    preview = valid_candidates[:10]

    return {
        "batch_id":    batch_id,
        "total_rows":  len(valid_candidates),
        "skipped_rows": skipped_rows,
        "detected":    detected,
        "preview":     preview,
    }


# ── Batch detail (paginated) ──────────────────────────────────────────────────

@router.get("/batch/{batch_id}")
def get_batch(
    batch_id: str,
    page: int = Query(1, ge=1),
    user: dict = Depends(get_current_user),
):
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    batch = query_one("SELECT * FROM campus_upload_batch WHERE id=%s", [batch_id])
    if not batch:
        raise HTTPException(404, "Batch not found")

    per_page = 50
    offset = (page - 1) * per_page
    candidates = query(
        """SELECT id, name, email, phone, college, branch,
                  cgpa, graduation_year, invite_status, invite_sent_at,
                  resume_uploaded, nexai_session_id, created_at
           FROM campus_candidate
           WHERE batch_id=%s
           ORDER BY created_at, id
           LIMIT %s OFFSET %s""",
        [batch_id, per_page, offset],
    )
    total = (query_one(
        "SELECT COUNT(*) AS n FROM campus_candidate WHERE batch_id=%s", [batch_id]
    ) or {}).get("n", 0)

    return {
        "batch": {
            "id":             str(batch["id"]),
            "requisition_id": str(batch["requisition_id"]),
            "file_name":      batch["file_name"],
            "total_rows":     batch["total_rows"],
            "selected_count": batch["selected_count"],
            "invited_count":  batch["invited_count"],
            "status":         batch["status"],
            "created_at":     batch["created_at"].isoformat() if batch["created_at"] else None,
        },
        "candidates": [
            {
                "id":              str(c["id"]),
                "name":            c["name"],
                "email":           c["email"],
                "phone":           c["phone"],
                "college":         c["college"],
                "branch":          c["branch"],
                "cgpa":            float(c["cgpa"]) if c["cgpa"] is not None else None,
                "graduation_year": c["graduation_year"],
                "invite_status":   c["invite_status"],
                "invite_sent_at":  c["invite_sent_at"].isoformat() if c["invite_sent_at"] else None,
                "resume_uploaded": c["resume_uploaded"],
                "nexai_link":      (
                    c["nexai_session_id"]
                    if c["invite_status"] == "invite_queued" and c["nexai_session_id"]
                    else None
                ),
            }
            for c in candidates
        ],
        "total": total,
        "page":  page,
        "pages": max(1, -(-total // per_page)),
    }


# ── Bulk invite ───────────────────────────────────────────────────────────────

class BulkCampusInviteIn(BaseModel):
    candidate_ids: list[str]
    requisition_id: str


def _campus_base_url() -> tuple[str, bool]:
    """
    Return (base_url, is_localhost).
    Uses connectors._load_email_cfg() which already applies the correct
    priority: DB (Settings UI) → APP_BASE_URL env var → default localhost.
    DB always wins, so the Settings UI value is never shadowed by .env.prod.
    """
    from ..services.connectors import _load_email_cfg
    url = (_load_email_cfg().get("base_url") or "http://localhost:8000").strip().rstrip("/")
    is_local = any(x in url for x in ("localhost", "127.0.0.1", "0.0.0.0"))
    return url, is_local


@router.post("/batch/{batch_id}/invite")
def bulk_invite(
    batch_id: str,
    body: BulkCampusInviteIn,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """
    For each candidate_id:
      1. Upsert candidate + application records.
      2. Generate NexAI invite token.
      3. Queue or send invite email based on PROD_BASE_URL.
      4. Update batch counts.
    """
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    batch = query_one("SELECT * FROM campus_upload_batch WHERE id=%s", [batch_id])
    if not batch:
        raise HTTPException(404, "Batch not found")

    req = query_one(
        """SELECT r.id, r.title, r.key_skills, r.job_description,
                  gc.name AS company
           FROM requisition r
           JOIN business_unit bu ON bu.id = r.bu_id
           JOIN group_company gc ON gc.id = bu.company_id
           WHERE r.id=%s""",
        [body.requisition_id],
    )
    if not req:
        raise HTTPException(404, "Requisition not found")

    base_url, is_local = _campus_base_url()

    invited = 0
    queued = 0
    failed: list[dict] = []

    # Lazy import to avoid circular dependency
    from ..routers.nexai_api import _generate_questions, _build_invite_html

    for cid in body.candidate_ids:
        campus_c = query_one(
            "SELECT * FROM campus_candidate WHERE id=%s AND batch_id=%s",
            [cid, batch_id],
        )
        if not campus_c:
            failed.append({"id": cid, "reason": "not_found"})
            continue

        # 1 — upsert candidate
        existing_cand = query_one(
            "SELECT id FROM candidate WHERE LOWER(email)=LOWER(%s)",
            [campus_c["email"]],
        )
        if existing_cand:
            cand_id = str(existing_cand["id"])
        else:
            new_cand = query_one(
                """INSERT INTO candidate (full_name, email, phone, source)
                   VALUES (%s, %s, %s, 'campus_bulk')
                   ON CONFLICT DO NOTHING RETURNING id""",
                [campus_c["name"] or "Unknown", campus_c["email"], campus_c["phone"]],
            )
            if not new_cand:
                # Race: inserted by another request between our check and insert
                new_cand = query_one(
                    "SELECT id FROM candidate WHERE LOWER(email)=LOWER(%s)",
                    [campus_c["email"]],
                )
            if not new_cand:
                failed.append({"id": cid, "reason": "candidate_upsert_failed"})
                continue
            cand_id = str(new_cand["id"])

        # 2 — upsert application
        existing_app = query_one(
            "SELECT id FROM application WHERE candidate_id=%s AND requisition_id=%s",
            [cand_id, body.requisition_id],
        )
        if existing_app:
            app_id = str(existing_app["id"])
        else:
            new_app = query_one(
                """INSERT INTO application
                       (candidate_id, requisition_id, status, applied_at)
                   VALUES (%s, %s, 'nexai_bot', now()) RETURNING id""",
                [cand_id, body.requisition_id],
            )
            if not new_app:
                failed.append({"id": cid, "reason": "application_create_failed"})
                continue
            app_id = str(new_app["id"])

        # 3 — generate invite token (skip if active invite already exists)
        active = query_one(
            """SELECT id FROM nexai_invite
               WHERE application_id=%s AND used_at IS NULL AND expires_at > now()
               LIMIT 1""",
            [app_id],
        )
        if active:
            # Re-use existing token for the link
            token_row = query_one(
                "SELECT token FROM nexai_invite WHERE id=%s", [active["id"]]
            )
            token = token_row["token"] if token_row else secrets.token_urlsafe(32)
        else:
            token = secrets.token_urlsafe(32)
            try:
                query(
                    """INSERT INTO nexai_invite (application_id, token, created_by)
                       VALUES (%s, %s, %s)""",
                    [app_id, token, user["sub"]],
                    fetch=False,
                )
            except Exception as exc:
                failed.append({"id": cid, "reason": f"token_error: {exc}"})
                continue

        # 4 — upsert nexai_session for avatar pre-render
        _saved_qs = query_one(
            "SELECT questions FROM requisition_questions WHERE requisition_id=%s",
            [body.requisition_id],
        )
        questions = (
            list(_saved_qs["questions"]) if _saved_qs
            else _generate_questions(
                req.get("key_skills") or [],
                req.get("job_description") or "",
            )
        )
        existing_sess = query_one(
            "SELECT id FROM nexai_session WHERE application_id=%s", [app_id]
        )
        if existing_sess:
            session_id = str(existing_sess["id"])
        else:
            sess_row = query_one(
                """INSERT INTO nexai_session
                       (application_id, requisition_id, questions, status)
                   VALUES (%s, %s, %s::jsonb, 'pending') RETURNING id""",
                [app_id, body.requisition_id, json.dumps(questions)],
            )
            session_id = str(sess_row["id"])

        background_tasks.add_task(_prerender_svc.prerender_interview_videos, session_id)

        invite_url = f"{base_url}/nexai-interview?token={token}"

        # 5 — update campus_candidate with application_id + link
        query(
            """UPDATE campus_candidate
               SET application_id=%s, nexai_session_id=%s, invite_sent_at=now()
               WHERE id=%s""",
            [app_id, invite_url if is_local else token, cid],
            fetch=False,
        )

        if is_local:
            # PROD_BASE_URL not set or is localhost — queue, do not email
            query(
                "UPDATE campus_candidate SET invite_status='invite_queued' WHERE id=%s",
                [cid], fetch=False,
            )
            queued += 1
        else:
            # Send invite email using existing email template system
            try:
                from ..services.connectors import resolve_global_placeholders as _rgp
                _globals = _rgp(req_id=body.requisition_id, actor=user)
                _reply_to = _globals.get("recruiter_email") or None

                email_subject, plain = _render_email_tmpl("nexai_invite", {
                    "candidate_name": campus_c["name"] or "Candidate",
                    "job_title":      req["title"],
                    "company_name":   req["company"],
                    "interview_link": invite_url,
                }, req_id=body.requisition_id, actor=user)

                html_body = _build_invite_html(
                    name=campus_c["name"] or "Candidate",
                    job=req["title"],
                    company=req["company"],
                    invite_url=invite_url,
                )
                send_email(
                    campus_c["email"], email_subject, plain,
                    html=html_body, reply_to=_reply_to,
                )
                query(
                    "UPDATE campus_candidate SET invite_status='invited' WHERE id=%s",
                    [cid], fetch=False,
                )
                invited += 1
            except Exception as exc:
                # Email delivery failed — fall back to queued so candidate is not lost
                query(
                    "UPDATE campus_candidate SET invite_status='invite_queued' WHERE id=%s",
                    [cid], fetch=False,
                )
                queued += 1
                print(f"[campus-invite] email failed for {campus_c['email']}: {exc}")

    # Update batch counters
    total_actioned = len(body.candidate_ids) - len(failed)
    total_sent = invited + queued
    if total_actioned > 0:
        query(
            """UPDATE campus_upload_batch
               SET selected_count = selected_count + %s,
                   invited_count  = invited_count  + %s,
                   status = CASE WHEN status='draft' THEN 'invites_sent' ELSE status END
               WHERE id=%s""",
            [total_actioned, total_sent, batch_id],
            fetch=False,
        )

    return {"invited": invited, "queued": queued, "failed": failed}


# ── Resend queued invites ─────────────────────────────────────────────────────

@router.post("/batch/{batch_id}/resend-queued")
def resend_queued_invites(
    batch_id: str,
    user: dict = Depends(get_current_user),
):
    """Email all invite_queued candidates in a batch using their existing tokens."""
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    base_url, is_local = _campus_base_url()
    if is_local:
        raise HTTPException(400, "Production URL is still localhost. Set the base URL in Settings first.")

    batch = query_one("SELECT * FROM campus_upload_batch WHERE id=%s", [batch_id])
    if not batch:
        raise HTTPException(404, "Batch not found")

    req = query_one(
        """SELECT r.id, r.title, gc.name AS company
           FROM requisition r
           JOIN business_unit bu ON bu.id = r.bu_id
           JOIN group_company gc ON gc.id = bu.company_id
           WHERE r.id=%s""",
        [str(batch["requisition_id"])],
    )
    if not req:
        raise HTTPException(404, "Requisition not found")

    queued = query(
        """SELECT cc.id, cc.name, cc.email, cc.application_id
           FROM campus_candidate cc
           WHERE cc.batch_id=%s AND cc.invite_status='invite_queued'""",
        [batch_id],
    )
    if not queued:
        return {"sent": 0, "failed": []}

    try:
        from ..routers.nexai_api import _build_invite_html
        from ..services.connectors import send_email, resolve_global_placeholders as _rgp
        from ..services.email_templates import render_template as _render_email_tmpl
    except Exception as imp_exc:
        raise HTTPException(500, f"Import error: {imp_exc}")

    sent = 0
    failed: list[dict] = []

    for c in queued:
        if not c["application_id"]:
            failed.append({"id": str(c["id"]), "reason": "no_application"})
            continue

        invite_row = query_one(
            """SELECT token FROM nexai_invite
               WHERE application_id=%s AND used_at IS NULL AND expires_at > now()
               ORDER BY invited_at DESC LIMIT 1""",
            [str(c["application_id"])],
        )
        if not invite_row:
            failed.append({"id": str(c["id"]), "reason": "no_valid_token"})
            continue

        token = invite_row["token"]
        invite_url = f"{base_url}/nexai-interview?token={token}"

        try:
            _globals = _rgp(req_id=str(req["id"]), actor=user)
            _reply_to = _globals.get("recruiter_email") or None

            email_subject, plain = _render_email_tmpl("nexai_invite", {
                "candidate_name": c["name"] or "Candidate",
                "job_title":      req["title"],
                "company_name":   req["company"],
                "interview_link": invite_url,
            }, req_id=str(req["id"]), actor=user)

            html_body = _build_invite_html(
                name=c["name"] or "Candidate",
                job=req["title"],
                company=req["company"],
                invite_url=invite_url,
            )
            send_email(c["email"], email_subject, plain, html=html_body, reply_to=_reply_to)

            query(
                """UPDATE campus_candidate
                   SET invite_status='invited', nexai_session_id=%s, invite_sent_at=now()
                   WHERE id=%s""",
                [token, str(c["id"])],
                fetch=False,
            )
            sent += 1
        except Exception as exc:
            failed.append({"id": str(c["id"]), "reason": str(exc)})

    if sent > 0:
        query(
            "UPDATE campus_upload_batch SET invited_count = invited_count + %s WHERE id=%s",
            [sent, batch_id],
            fetch=False,
        )

    return {"sent": sent, "failed": failed}


# ── Batch list ────────────────────────────────────────────────────────────────

@router.get("/batches")
def list_batches(
    requisition_id: str = Query(...),
    user: dict = Depends(get_current_user),
):
    if user["role"] not in ("recruiter", "ta_manager", "admin"):
        raise HTTPException(403, "Not authorised")

    rows = query(
        """SELECT id, file_name, total_rows, selected_count,
                  invited_count, status, created_at
           FROM campus_upload_batch
           WHERE requisition_id=%s
           ORDER BY created_at DESC""",
        [requisition_id],
    )
    return [
        {
            "id":             str(r["id"]),
            "file_name":      r["file_name"],
            "total_rows":     r["total_rows"],
            "selected_count": r["selected_count"],
            "invited_count":  r["invited_count"],
            "status":         r["status"],
            "created_at":     r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


# ── Public: campus session resume upload ──────────────────────────────────────

@router.post("/session/{session_token}/resume")
async def upload_campus_resume(
    session_token: str,
    file: UploadFile = File(...),
):
    """
    Public (no JWT) — candidate uploads resume during a campus NexAI session.
    Runs intake_and_screen with is_fresher_role forced True, updates campus_candidate.
    """
    invite = query_one(
        """SELECT ni.application_id, a.requisition_id
           FROM nexai_invite ni
           JOIN application a ON a.id = ni.application_id
           WHERE ni.token=%s""",
        [session_token],
    )
    if not invite:
        raise HTTPException(404, "Invalid session token")

    app_id = str(invite["application_id"])
    req_id = str(invite["requisition_id"])

    if not file.filename or not file.filename.lower().endswith((".pdf", ".docx", ".doc")):
        raise HTTPException(400, "Only PDF or Word documents are accepted (PDF/DOCX/DOC)")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(400, "File too large — maximum 5 MB")

    _CV_STORE = os.environ.get("CV_STORE_DIR", "/app/cv_store")
    os.makedirs(_CV_STORE, exist_ok=True)
    import uuid as _uuid
    safe_name = f"campus_{_uuid.uuid4().hex}_{os.path.basename(file.filename).replace(' ', '_')}"
    file_path = os.path.join(_CV_STORE, safe_name)
    with open(file_path, "wb") as fh:
        fh.write(content)

    resume_url = f"/api/resume/{safe_name}"

    # Parse resume text and run screening
    try:
        resume_text = _parse_resume(file_path)
    except Exception:
        resume_text = ""

    cand_row = query_one(
        "SELECT candidate_id FROM application WHERE id=%s", [app_id]
    )
    if cand_row and resume_text:
        try:
            _pipeline_svc.intake_and_screen(
                requisition_id=req_id,
                candidate_id=str(cand_row["candidate_id"]),
                resume_text=resume_text,
                candidate_years=0.0,
                file_size_bytes=len(content),
            )
        except Exception as exc:
            print(f"[campus-resume] intake_and_screen failed: {exc}")

    # Update candidate record with resume_url
    if cand_row:
        query(
            "UPDATE candidate SET resume_url=%s WHERE id=%s",
            [resume_url, str(cand_row["candidate_id"])],
            fetch=False,
        )

    # Mark campus_candidate as uploaded
    query(
        """UPDATE campus_candidate
           SET resume_uploaded=TRUE, resume_url=%s
           WHERE application_id=%s""",
        [resume_url, app_id],
        fetch=False,
    )

    return {"ok": True, "resume_url": resume_url}


# ── Public: is-campus check ───────────────────────────────────────────────────

@router.get("/session/{session_token}/is-campus")
def is_campus_session(session_token: str):
    """Public (no JWT) — returns whether this invite belongs to a campus bulk batch."""
    invite = query_one(
        "SELECT application_id FROM nexai_invite WHERE token=%s",
        [session_token],
    )
    if not invite:
        return {"is_campus": False}

    campus_c = query_one(
        "SELECT id FROM campus_candidate WHERE application_id=%s LIMIT 1",
        [str(invite["application_id"])],
    )
    return {"is_campus": campus_c is not None}
