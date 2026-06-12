"""
CV Repository — bulk ingest, search, file serve, API-token management.

Roles: ta_manager, recruiter, admin only (others → 403).
Auth: standard JWT OR long-lived API token (for the watcher script).
"""
import asyncio
import io
import os
import re
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..auth_utils import _decode, get_current_user
from ..db import query, query_one
from ..services import cv_parser as _parser

router = APIRouter(prefix="/api/cv", tags=["cv-repository"])

_ALLOWED    = {"ta_manager", "recruiter", "admin"}
_bearer     = HTTPBearer(auto_error=False)
_SUPPORTED  = {".pdf", ".docx", ".doc"}
_CV_STORE   = os.environ.get("CV_STORE_DIR", "/app/cv_store")


# ── Auth: JWT or long-lived API token ────────────────────────────────────────

def _cv_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    if not creds:
        raise HTTPException(401, "Not authenticated")
    token = creds.credentials
    # Try JWT first
    try:
        payload = _decode(token)
        if payload.get("role") in _ALLOWED:
            return payload
        raise HTTPException(403, "CV Repository: ta_manager / recruiter / admin only")
    except HTTPException:
        raise
    except Exception:
        pass
    # Try long-lived API token
    row = query_one(
        "SELECT id, email, role, full_name FROM app_user WHERE api_token=%s",
        [token],
    )
    if row and row["role"] in _ALLOWED:
        return {"sub": str(row["id"]), "email": row["email"],
                "role": row["role"], "name": row["full_name"]}
    raise HTTPException(401, "Invalid or expired token")


def _require(user: dict):
    if user.get("role") not in _ALLOWED:
        raise HTTPException(403, "CV Repository: ta_manager / recruiter / admin only")


# ── Boolean query → tsquery ───────────────────────────────────────────────────

def _to_tsquery(raw: str) -> Optional[str]:
    """
    Convert user boolean search string to PostgreSQL tsquery.
    Supports: AND, OR, NOT keywords; "quoted phrases"; parentheses.
    Raises ValueError with a friendly message on syntax error.
    Returns None if query is empty.
    """
    q = raw.strip()
    if not q:
        return None

    # Phase 1: extract quoted phrases → phrase placeholders
    _phrases: list[str] = []

    def _repl_phrase(m: re.Match) -> str:
        words = m.group(1).split()
        if not words:
            return ""
        idx = len(_phrases)
        _phrases.append("(" + " <-> ".join(w.lower() for w in words) + ")")
        return f"__P{idx}__"

    q = re.sub(r'"([^"]*)"', _repl_phrase, q)

    # Phase 2: keyword operators
    q = re.sub(r'\bAND\b', "&", q, flags=re.IGNORECASE)
    q = re.sub(r'\bOR\b',  "|", q, flags=re.IGNORECASE)
    q = re.sub(r'\bNOT\b', "!", q, flags=re.IGNORECASE)

    # Phase 3: tokenize
    raw_tokens = re.split(r'([&|!()\s])', q)
    tokens = [t.strip() for t in raw_tokens if t and t.strip()]

    # Phase 4: build output with implicit & between adjacent value tokens
    _OPS = frozenset({"&", "|", "!", "(", ")"})
    out: list[str] = []
    for tok in tokens:
        if out:
            prev = out[-1]
            prev_ends_value = prev not in _OPS or prev == ")"
            tok_starts_value = tok not in _OPS or tok in ("(", "!")
            if prev_ends_value and tok_starts_value:
                out.append("&")
        out.append(tok.lower() if tok not in _OPS and not tok.startswith("__P") else tok)

    # Phase 5: validation
    depth = 0
    for i, tok in enumerate(out):
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("Unmatched ')' — check your parentheses")
        if i == 0 and tok in ("&", "|"):
            raise ValueError("Query cannot start with AND or OR")
        if i == len(out) - 1 and tok in ("&", "|"):
            raise ValueError("Query cannot end with AND or OR")
        if tok in ("&", "|") and i + 1 < len(out) and out[i + 1] in ("&", "|"):
            raise ValueError("Consecutive AND/OR operators are not allowed")
        if tok in ("&", "|") and i + 1 < len(out) and out[i + 1] == ")":
            raise ValueError("Operator before ')' is not allowed")
        if tok == "(" and i + 1 < len(out) and out[i + 1] in ("&", "|"):
            raise ValueError("Operator immediately after '(' is not allowed")

    if depth != 0:
        raise ValueError("Unmatched '(' — check your parentheses")
    if not out:
        return None

    result = " ".join(out)
    for i, ph in enumerate(_phrases):
        result = result.replace(f"__p{i}__", ph).replace(f"__P{i}__", ph)
    return result


# ── Ingest helpers ────────────────────────────────────────────────────────────

def _ingest_one(
    data: bytes,
    filename: str,
    source: str,
    uploaded_by: Optional[str],
) -> dict:
    """
    Process one file: hash-check, extract, map, store.
    Returns a status dict: {status:'ok'|'duplicate'|'error', cv_id, mapped}.
    """
    os.makedirs(_CV_STORE, exist_ok=True)

    ext = Path(filename).suffix.lower().lstrip(".")
    file_hash = _parser.sha256_hash(data)

    existing = query_one(
        "SELECT id FROM cv_repository WHERE file_hash=%s", [file_hash]
    )
    if existing:
        return {"status": "duplicate", "filename": filename}

    raw_text = _parser.extract_text(data, ext)
    skills   = _parser.extract_tier1_skills(raw_text)
    name     = _parser.parse_candidate_name(filename)

    cv_id   = str(uuid.uuid4())
    dest    = os.path.join(_CV_STORE, f"{cv_id}.{ext}")
    with open(dest, "wb") as f:
        f.write(data)

    # Auto-map by normalised full_name
    candidate_id = req_id = None
    map_status = "pool"
    if name:
        cand = query_one(
            "SELECT id FROM candidate WHERE LOWER(TRIM(full_name)) = LOWER(TRIM(%s)) LIMIT 1",
            [name],
        )
        if cand:
            candidate_id = str(cand["id"])
            app_row = query_one(
                """SELECT requisition_id FROM application
                   WHERE candidate_id=%s ORDER BY applied_at DESC LIMIT 1""",
                [candidate_id],
            )
            req_id     = str(app_row["requisition_id"]) if app_row and app_row["requisition_id"] else None
            map_status = "mapped"

    query(
        """INSERT INTO cv_repository
           (id, file_name, file_path, file_hash, file_ext, candidate_name,
            candidate_id, requisition_id, map_status, raw_text,
            text_vector, skills, source, uploaded_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                   to_tsvector('english', %s), %s, %s, %s)""",
        [cv_id, filename, dest, file_hash, ext, name,
         candidate_id, req_id, map_status, raw_text,
         raw_text or "", skills, source, uploaded_by],
        fetch=False,
    )

    # Attach to candidate if they have no CV yet
    if candidate_id:
        query(
            """UPDATE candidate SET cv_repository_id=%s
               WHERE id=%s AND cv_repository_id IS NULL""",
            [cv_id, candidate_id],
            fetch=False,
        )

    return {"status": "ok", "cv_id": cv_id, "mapped": map_status == "mapped"}


def ingest_and_link(
    data: bytes,
    filename: str,
    source: str,
    uploaded_by: Optional[str],
    candidate_id: str,
    req_id: Optional[str],
) -> dict:
    """
    Ingest a resume file and hard-link it to a known candidate + requisition.
    Always produces map_status='mapped'. Hash-deduplication still applies —
    if the same bytes already exist, the existing row is re-linked if unlinked.
    Returns: {status:'ok'|'duplicate'|'error', cv_id, mapped:True}.
    """
    os.makedirs(_CV_STORE, exist_ok=True)

    ext = Path(filename).suffix.lower().lstrip(".")
    if not ext:
        ext = "bin"
    file_hash = _parser.sha256_hash(data)

    existing = query_one(
        "SELECT id, candidate_id FROM cv_repository WHERE file_hash=%s", [file_hash]
    )
    if existing:
        # Existing row — update link if it's currently unmapped
        if not existing["candidate_id"] and candidate_id:
            query(
                """UPDATE cv_repository
                   SET candidate_id=%s, requisition_id=%s, map_status='mapped'
                   WHERE id=%s""",
                [candidate_id, req_id, str(existing["id"])],
                fetch=False,
            )
            query(
                "UPDATE candidate SET cv_repository_id=%s WHERE id=%s AND cv_repository_id IS NULL",
                [str(existing["id"]), candidate_id],
                fetch=False,
            )
        return {"status": "duplicate", "cv_id": str(existing["id"]), "mapped": True, "filename": filename}

    raw_text = _parser.extract_text(data, ext)
    skills   = _parser.extract_tier1_skills(raw_text)
    name     = _parser.parse_candidate_name(filename)

    cv_id = str(uuid.uuid4())
    dest  = os.path.join(_CV_STORE, f"{cv_id}.{ext}")
    with open(dest, "wb") as f:
        f.write(data)

    query(
        """INSERT INTO cv_repository
           (id, file_name, file_path, file_hash, file_ext, candidate_name,
            candidate_id, requisition_id, map_status, raw_text,
            text_vector, skills, source, uploaded_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'mapped',%s,
                   to_tsvector('english', %s), %s, %s, %s)""",
        [cv_id, filename, dest, file_hash, ext, name,
         candidate_id, req_id, raw_text,
         raw_text or "", skills, source, uploaded_by],
        fetch=False,
    )

    query(
        "UPDATE candidate SET cv_repository_id=%s WHERE id=%s AND cv_repository_id IS NULL",
        [cv_id, candidate_id],
        fetch=False,
    )

    return {"status": "ok", "cv_id": cv_id, "mapped": True}


def _run_ingest_job(job_id: str, folder: str, uploaded_by: Optional[str]):
    """Background worker for scan-folder ingestion."""
    paths: list[Path] = []
    for root, _, files in os.walk(folder):
        for fn in files:
            if Path(fn).suffix.lower() in _SUPPORTED:
                paths.append(Path(root) / fn)

    query(
        "UPDATE cv_ingest_jobs SET total=%s WHERE id=%s",
        [len(paths), job_id], fetch=False,
    )

    processed = mapped = pooled = duplicates = 0
    errors: list[dict] = []

    for p in paths:
        try:
            data = p.read_bytes()
            r = _ingest_one(data, p.name, "bulk_folder", uploaded_by)
            if r["status"] == "duplicate":
                duplicates += 1
            elif r["status"] == "ok":
                processed += 1
                if r["mapped"]:
                    mapped += 1
                else:
                    pooled += 1
            else:
                errors.append({"file": p.name, "error": r.get("error", "unknown")})
        except Exception as exc:
            errors.append({"file": p.name, "error": str(exc)})

        import json
        query(
            """UPDATE cv_ingest_jobs
               SET processed=%s, mapped=%s, pooled=%s, duplicates=%s, errors=%s::jsonb
               WHERE id=%s""",
            [processed, mapped, pooled, duplicates,
             json.dumps(errors), job_id],
            fetch=False,
        )

    query(
        "UPDATE cv_ingest_jobs SET status='done' WHERE id=%s",
        [job_id], fetch=False,
    )


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats")
def cv_stats(user: dict = Depends(_cv_auth)):
    row = query_one(
        """SELECT
               COUNT(*)                                            AS total,
               COUNT(*) FILTER (WHERE map_status='mapped')        AS mapped,
               COUNT(*) FILTER (WHERE map_status='pool')          AS pool,
               COUNT(*) FILTER (WHERE enrich_status='done')       AS enriched,
               COUNT(*) FILTER (WHERE enrich_status='pending')    AS pending,
               COUNT(*) FILTER (WHERE enrich_status='failed')     AS failed
           FROM cv_repository""",
        [],
    )
    total    = int(row["total"] or 0)
    enriched = int(row["enriched"] or 0)
    return {
        "total":        total,
        "mapped":       int(row["mapped"] or 0),
        "pool":         int(row["pool"] or 0),
        "enriched":     enriched,
        "pending":      int(row["pending"] or 0),
        "failed":       int(row["failed"] or 0),
        "enriched_pct": round(enriched / total * 100, 1) if total else 0,
    }


# ── Search ────────────────────────────────────────────────────────────────────

@router.get("/search")
def cv_search(
    q:          Optional[str] = None,
    skills:     Optional[str] = None,
    min_exp:    Optional[float] = None,
    map_status: Optional[str] = None,
    limit:      int = 50,
    offset:     int = 0,
    user: dict = Depends(_cv_auth),
):
    conditions: list[str] = []
    params: list = []

    if q and q.strip():
        try:
            tsq = _to_tsquery(q)
        except ValueError as exc:
            raise HTTPException(400, f"Search syntax error: {exc}")
        if tsq:
            conditions.append("text_vector @@ to_tsquery('english', %s)")
            params.append(tsq)

    if skills:
        skill_list = [s.strip().lower() for s in skills.split(",") if s.strip()]
        if skill_list:
            conditions.append("skills && %s")
            params.append(skill_list)

    if min_exp is not None:
        conditions.append("experience_years >= %s")
        params.append(min_exp)

    if map_status in ("mapped", "pool"):
        conditions.append("map_status = %s")
        params.append(map_status)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    count_row = query_one(
        f"SELECT COUNT(*) AS n FROM cv_repository {where}", params
    )
    total = int((count_row or {}).get("n") or 0)

    rows = query(
        f"""SELECT
               cv.id, cv.file_name, cv.candidate_name, cv.map_status,
               cv.enrich_status, cv.experience_years, cv.current_position,
               cv.location, cv.ai_summary,
               cv.skills[1:8]               AS top_skills,
               cv.source, cv.created_at,
               cv.candidate_id,
               c.full_name                  AS cand_full_name,
               cv.requisition_id,
               r.req_code, r.title          AS req_title,
               lat_app.status               AS candidate_stage,
               lat_app.id                   AS application_id
           FROM cv_repository cv
           LEFT JOIN candidate c   ON c.id = cv.candidate_id
           LEFT JOIN requisition r ON r.id = cv.requisition_id
           LEFT JOIN LATERAL (
               SELECT id, status FROM application
               WHERE candidate_id = cv.candidate_id
               ORDER BY applied_at DESC LIMIT 1
           ) lat_app ON cv.candidate_id IS NOT NULL
           {where}
           ORDER BY cv.created_at DESC
           LIMIT %s OFFSET %s""",
        params + [limit, offset],
    ) or []

    def _row(r):
        return {
            "id":              str(r["id"]),
            "file_name":       r["file_name"],
            "candidate_name":  r["candidate_name"],
            "map_status":      r["map_status"],
            "enrich_status":   r["enrich_status"],
            "experience_years": r["experience_years"],
            "current_position": r["current_position"],
            "location":        r["location"],
            "ai_summary":      r["ai_summary"],
            "top_skills":      list(r["top_skills"] or []),
            "source":          r["source"],
            "created_at":      r["created_at"].isoformat() if r["created_at"] else None,
            "candidate_id":    str(r["candidate_id"]) if r["candidate_id"] else None,
            "cand_full_name":  r["cand_full_name"],
            "requisition_id":  str(r["requisition_id"]) if r["requisition_id"] else None,
            "req_code":        r["req_code"],
            "req_title":       r["req_title"],
            "candidate_stage": r["candidate_stage"],
            "application_id":  str(r["application_id"]) if r["application_id"] else None,
        }

    return {"total": total, "results": [_row(r) for r in rows]}


# ── Job progress ──────────────────────────────────────────────────────────────

@router.get("/jobs/{job_id}")
def get_job(job_id: str, user: dict = Depends(_cv_auth)):
    row = query_one(
        "SELECT * FROM cv_ingest_jobs WHERE id=%s", [job_id]
    )
    if not row:
        raise HTTPException(404, "Job not found")
    return {
        "id":          str(row["id"]),
        "status":      row["status"],
        "total":       row["total"],
        "processed":   row["processed"],
        "mapped":      row["mapped"],
        "pooled":      row["pooled"],
        "duplicates":  row["duplicates"],
        "errors":      row["errors"] or [],
        "created_at":  row["created_at"].isoformat() if row["created_at"] else None,
    }


# ── Gmail status ──────────────────────────────────────────────────────────────

@router.get("/email-status")
def email_status(user: dict = Depends(_cv_auth)):
    creds_path = os.environ.get("GOOGLE_OAUTH_CREDENTIALS")
    configured = bool(creds_path and os.path.exists(creds_path))
    setting = query_one(
        "SELECT value FROM system_settings WHERE key='email_ingest_accounts'", []
    )
    accounts = (setting or {}).get("value") or ""
    return {
        "configured": configured,
        "accounts":   [a.strip() for a in accounts.split(",") if a.strip()],
        "env_var":    "GOOGLE_OAUTH_CREDENTIALS",
    }


# ── File serve ────────────────────────────────────────────────────────────────

@router.get("/{cv_id}/file")
def serve_cv_file(cv_id: str, user: dict = Depends(_cv_auth)):
    row = query_one(
        "SELECT file_path, file_name, file_ext FROM cv_repository WHERE id=%s",
        [cv_id],
    )
    if not row:
        raise HTTPException(404, "CV not found")
    path = row["file_path"]
    if not path or not os.path.exists(path):
        raise HTTPException(404, "File not found on disk")
    media_types = {
        "pdf":  "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc":  "application/msword",
    }
    mt = media_types.get(row["file_ext"] or "", "application/octet-stream")
    return FileResponse(
        path,
        media_type=mt,
        filename=row["file_name"] or f"cv_{cv_id}.{row['file_ext']}",
    )


# ── Upload (multiple files) ───────────────────────────────────────────────────

@router.post("/upload")
async def upload_cvs(
    files: list[UploadFile] = File(...),
    user:  dict = Depends(_cv_auth),
):
    results = []
    for f in files:
        if Path(f.filename or "").suffix.lower() not in _SUPPORTED:
            results.append({"filename": f.filename, "status": "skipped",
                            "reason": "unsupported file type"})
            continue
        data = await f.read()
        r = await asyncio.to_thread(
            _ingest_one, data, f.filename, "upload", user["sub"]
        )
        results.append({**r, "filename": f.filename})
    ok  = sum(1 for r in results if r.get("status") == "ok")
    dup = sum(1 for r in results if r.get("status") == "duplicate")
    return {"processed": ok, "duplicates": dup, "details": results}


# ── Scan folder ───────────────────────────────────────────────────────────────

@router.post("/scan-folder")
def scan_folder(
    background_tasks: BackgroundTasks,
    user: dict = Depends(_cv_auth),
):
    if user.get("role") not in ("ta_manager", "admin"):
        raise HTTPException(403, "Only ta_manager or admin can trigger folder scan")

    inbox = os.environ.get("CV_INBOX_DIR", "/app/cv_inbox")
    if not os.path.isdir(inbox):
        raise HTTPException(400, f"Inbox folder not found: {inbox}")

    import json
    job_id = str(uuid.uuid4())
    query(
        """INSERT INTO cv_ingest_jobs (id, status, total, processed, mapped,
           pooled, duplicates, errors) VALUES (%s,'running',0,0,0,0,0,'[]'::jsonb)""",
        [job_id], fetch=False,
    )
    background_tasks.add_task(_run_ingest_job, job_id, inbox, user["sub"])
    return {"job_id": job_id, "message": "Ingest job started"}


# ── Backfill: sync uploaded candidate resumes into CV Repository ──────────────

@router.post("/backfill-candidates")
def backfill_candidates(user: dict = Depends(_cv_auth)):
    """
    Idempotent — walks all candidates that have a resume file stored on disk
    (resume_url points to a file in UPLOADS_DIR) and ingests each one into
    cv_repository with map_status='mapped' and source='application'.
    Hash dedupe ensures running twice is safe.
    Returns counts: {processed, duplicates, skipped, errors}.
    """
    if user.get("role") not in ("ta_manager", "admin"):
        raise HTTPException(403, "Only ta_manager or admin can run backfill")

    rows = query(
        """SELECT c.id AS candidate_id, c.resume_url, c.full_name,
                  a.requisition_id
           FROM candidate c
           LEFT JOIN LATERAL (
               SELECT requisition_id FROM application
               WHERE candidate_id = c.id
               ORDER BY applied_at DESC LIMIT 1
           ) a ON true
           WHERE c.resume_url IS NOT NULL
             AND c.resume_url != ''
           ORDER BY c.id""",
        [],
    ) or []

    processed = duplicates = skipped = 0
    errors: list[dict] = []

    for row in rows:
        resume_path = row["resume_url"]
        if not resume_path or not os.path.isfile(resume_path):
            skipped += 1
            continue
        try:
            data = Path(resume_path).read_bytes()
            filename = Path(resume_path).name
            r = ingest_and_link(
                data=data,
                filename=filename,
                source="application",
                uploaded_by=user["sub"],
                candidate_id=str(row["candidate_id"]),
                req_id=str(row["requisition_id"]) if row["requisition_id"] else None,
            )
            if r["status"] == "duplicate":
                duplicates += 1
            else:
                processed += 1
        except Exception as exc:
            errors.append({"candidate_id": str(row["candidate_id"]), "error": str(exc)})

    return {
        "processed":  processed,
        "duplicates": duplicates,
        "skipped":    skipped,
        "errors":     errors,
        "total_candidates": len(rows),
    }


# ── Generate / regenerate long-lived API token ────────────────────────────────

@router.post("/generate-token")
def generate_api_token(user: dict = Depends(get_current_user)):
    """Generate (or replace) a long-lived API token for the current user.
    Used by the watcher script running on recruiter PCs.
    """
    _require(user)
    import secrets as _secrets
    token = _secrets.token_urlsafe(40)
    query(
        "UPDATE app_user SET api_token=%s WHERE id=%s",
        [token, user["sub"]], fetch=False,
    )
    return {"api_token": token,
            "note": "Store this securely — it grants upload access to your account."}
