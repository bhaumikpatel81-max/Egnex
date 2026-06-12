"""
HM Experience API — Hiring Manager home dashboard and TA-approval workflow.

Read endpoints: scoped server-side to hiring_manager_id = current uid.
Allowed roles for dashboard: hiring_manager + admin (for debugging).
ta-approve / ta-reject: ta_manager / admin only — 403 for any other role.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import get_current_user
from ..services.sla import (
    STAGE_SLA_KEY,
    PIPELINE_STAGE_LABELS,
    load_config,
    compute_rag,
)
from ..services.connectors import send_email
from ..services.email_templates import render_template

router = APIRouter(prefix="/api", tags=["hm"])

_TERMINAL = frozenset({
    "hired", "rejected", "on_hold",
    "joined", "screen_rejected", "dropped", "offer_cancelled",
})
_TERMINAL_TUPLE = ("hired", "rejected", "on_hold", "joined",
                   "screen_rejected", "dropped", "offer_cancelled")


# ── helpers ────────────────────────────────────────────────────────────────────

def _require_hm(user: dict):
    if user["role"] not in ("hiring_manager", "admin"):
        raise HTTPException(403, "Hiring Manager access required")


def _require_ta(user: dict):
    if user["role"] not in ("ta_manager", "admin"):
        raise HTTPException(403, "TA Manager or Admin access required")


def _user_email_name(user_id: str) -> tuple[Optional[str], str]:
    row = query_one("SELECT email, full_name FROM app_user WHERE id=%s", [user_id])
    if row:
        return row["email"], (row["full_name"] or "—")
    return None, "—"


def _ta_manager_emails() -> list[str]:
    rows = query(
        "SELECT email FROM app_user WHERE role='ta_manager' AND is_active=TRUE", []
    )
    return [r["email"] for r in (rows or []) if r.get("email")]


def _send_safe(
    template_key: str,
    values: dict,
    to_emails: list[str],
    req_id: str | None = None,
    actor: dict | None = None,
) -> None:
    if not to_emails:
        return
    try:
        from ..services.connectors import resolve_global_placeholders
        globals_ = resolve_global_placeholders(req_id=req_id, actor=actor)
        reply_to = globals_.get("recruiter_email") or None
        subject, body = render_template(template_key, values, req_id=req_id, actor=actor)
        for addr in to_emails:
            try:
                send_email(addr, subject, body, reply_to=reply_to)
            except Exception as exc:
                print(f"[hm_api] email to {addr} failed: {exc}")
    except Exception as exc:
        print(f"[hm_api] render_template({template_key!r}) failed: {exc}")


def _rag_sort_key(item: dict) -> int:
    return {"red": 0, "amber": 1, "green": 2}.get(
        (item.get("rag") or {}).get("status", "green"), 2
    )


# ── GET /api/hm/dashboard ─────────────────────────────────────────────────────

@router.get("/hm/dashboard")
def hm_dashboard(user: dict = Depends(get_current_user)):
    _require_hm(user)
    uid = user["sub"]
    sla_cfg = load_config()

    # ── 1. Pending scorecards ─────────────────────────────────────────────────
    # Interviews on the HM's reqs where the HM is on the panel but has not
    # yet submitted a scorecard (missing or draft).
    sc_rows = query(
        """
        SELECT
            i.id        AS interview_id,
            i.scheduled_at,
            c.full_name AS candidate_name,
            r.title     AS req_title,
            r.id        AS req_id,
            rc.name     AS round_name,
            COALESCE(s.status, 'not_started') AS sc_status,
            EXTRACT(EPOCH FROM (now() - COALESCE(i.scheduled_at, now()))) / 86400.0
                        AS days_waiting
        FROM interview i
        JOIN application   a  ON a.id  = i.application_id
        JOIN candidate     c  ON c.id  = a.candidate_id
        JOIN requisition   r  ON r.id  = a.requisition_id
        JOIN round_config  rc ON rc.id = i.round_config_id
        JOIN interview_panel ip
             ON ip.interview_id = i.id AND ip.interviewer_id = %s
        LEFT JOIN scorecard s
             ON s.interview_id = i.id AND s.interviewer_id = %s
        WHERE r.hiring_manager_id = %s
          AND COALESCE(i.status, 'scheduled') != 'cancelled'
          AND (s.id IS NULL OR s.status = 'draft')
        ORDER BY i.scheduled_at NULLS LAST
        LIMIT 20
        """,
        [uid, uid, uid],
    )
    sla_feedback = sla_cfg.get("stage_interview", 5)
    pending_scorecards = []
    for r in (sc_rows or []):
        days = float(r["days_waiting"] or 0)
        rag  = compute_rag(days, sla_feedback)
        pending_scorecards.append({
            "type":           "scorecard",
            "interview_id":   str(r["interview_id"]),
            "candidate_name": r["candidate_name"],
            "req_title":      r["req_title"],
            "req_id":         str(r["req_id"]),
            "round_name":     r["round_name"],
            "sc_status":      r["sc_status"],
            "days_waiting":   round(days, 1),
            "rag":            rag,
        })
    pending_scorecards.sort(key=_rag_sort_key)

    # ── 2. Pending offer approvals (HM is current sequential approver) ────────
    appr_rows = query(
        """
        SELECT
            o.id         AS offer_id,
            o.current_step,
            o.designation,
            o.total_ctc,
            oas.sla_days,
            oas.created_at AS step_created_at,
            (SELECT COUNT(*) FROM offer_approval_step oas2
             WHERE oas2.offer_id = o.id)            AS total_steps,
            c.full_name  AS candidate_name,
            r.title      AS req_title,
            r.id         AS req_id,
            a.id         AS application_id,
            EXTRACT(EPOCH FROM (now() - oas.created_at)) / 86400.0
                         AS days_waiting
        FROM offer o
        JOIN offer_approval_step oas
             ON oas.offer_id   = o.id
            AND oas.sequence   = o.current_step
            AND oas.approver_id = %s
            AND oas.status      = 'pending'
        JOIN application  a ON a.id = o.application_id
        JOIN candidate    c ON c.id = a.candidate_id
        JOIN requisition  r ON r.id = a.requisition_id
        WHERE o.status = 'pending_approval'
        ORDER BY oas.created_at NULLS LAST
        LIMIT 20
        """,
        [uid],
    )
    pending_approvals = []
    for r in (appr_rows or []):
        days     = float(r["days_waiting"] or 0)
        sla_days = int(r["sla_days"] or 2)
        rag      = compute_rag(days, sla_days)
        pending_approvals.append({
            "type":           "approval",
            "offer_id":       str(r["offer_id"]),
            "candidate_name": r["candidate_name"],
            "req_title":      r["req_title"],
            "req_id":         str(r["req_id"]),
            "application_id": str(r["application_id"]),
            "designation":    r["designation"],
            "total_ctc":      float(r["total_ctc"]) if r["total_ctc"] else None,
            "current_step":   int(r["current_step"]),
            "total_steps":    int(r["total_steps"]),
            "days_waiting":   round(days, 1),
            "rag":            rag,
        })
    pending_approvals.sort(key=_rag_sort_key)

    # ── 3. Awaiting HM decision (interview + shortlisted stages on HM's reqs) ─
    dec_rows = query(
        f"""
        SELECT
            a.id           AS app_id,
            a.status,
            a.current_round,
            c.full_name    AS candidate_name,
            r.title        AS req_title,
            r.id           AS req_id,
            EXTRACT(EPOCH FROM (
                now() - COALESCE(
                    (SELECT se.occurred_at FROM stage_event se
                     WHERE se.application_id = a.id
                       AND se.to_status = a.status
                     ORDER BY se.occurred_at DESC LIMIT 1),
                    a.applied_at
                )
            )) / 86400.0   AS days_in_stage
        FROM application a
        JOIN candidate   c ON c.id = a.candidate_id
        JOIN requisition r ON r.id = a.requisition_id
        WHERE r.hiring_manager_id = %s
          AND a.status IN ('interview', 'shortlisted')
          AND a.status NOT IN ({', '.join(['%s']*len(_TERMINAL_TUPLE))})
        ORDER BY a.status, days_in_stage DESC NULLS LAST
        LIMIT 30
        """,
        [uid] + list(_TERMINAL_TUPLE),
    )
    awaiting_decision = []
    for r in (dec_rows or []):
        days    = float(r["days_in_stage"] or 0)
        sla_key = STAGE_SLA_KEY.get(r["status"], "stage_default")
        target  = sla_cfg.get(sla_key, sla_cfg.get("stage_default", 5))
        rag     = compute_rag(days, target)
        awaiting_decision.append({
            "type":           "decision",
            "app_id":         str(r["app_id"]),
            "candidate_name": r["candidate_name"],
            "req_title":      r["req_title"],
            "req_id":         str(r["req_id"]),
            "stage":          r["status"],
            "stage_label":    PIPELINE_STAGE_LABELS.get(r["status"], r["status"]),
            "current_round":  r["current_round"],
            "days_in_stage":  round(days, 1),
            "rag":            rag,
        })
    awaiting_decision.sort(key=_rag_sort_key)

    # ── 4. My requisitions with per-stage counts + approval badge ─────────────
    req_rows = query(
        """
        SELECT
            r.id, r.title, r.status, r.req_code,
            COALESCE(r.approval_status, 'approved') AS approval_status,
            r.openings,
            EXTRACT(EPOCH FROM (
                now() - COALESCE(r.opened_at, r.created_at)
            )) / 86400.0                             AS open_days,
            COUNT(a.id) FILTER (WHERE a.status = 'applied')       AS cnt_applied,
            COUNT(a.id) FILTER (WHERE a.status = 'screen')        AS cnt_screen,
            COUNT(a.id) FILTER (WHERE a.status = 'nexai_bot')     AS cnt_nexai_bot,
            COUNT(a.id) FILTER (WHERE a.status = 'shortlisted')   AS cnt_shortlisted,
            COUNT(a.id) FILTER (WHERE a.status = 'interview')     AS cnt_interview,
            COUNT(a.id) FILTER (WHERE a.status = 'documentation') AS cnt_documentation,
            COUNT(a.id) FILTER (WHERE a.status = 'offered')       AS cnt_offered,
            COUNT(a.id) FILTER (WHERE a.status = 'hired')         AS cnt_hired
        FROM requisition r
        LEFT JOIN application a ON a.requisition_id = r.id
        WHERE r.hiring_manager_id = %s
        GROUP BY r.id, r.title, r.status, r.req_code,
                 r.approval_status, r.openings, r.opened_at, r.created_at
        ORDER BY r.created_at DESC
        """,
        [uid],
    )
    ttf_target = sla_cfg.get("req_time_to_fill", 45)
    my_reqs = []
    for r in (req_rows or []):
        open_days = float(r["open_days"] or 0)
        rag = compute_rag(open_days, ttf_target) if r["status"] == "open" else None
        my_reqs.append({
            "id":              str(r["id"]),
            "title":           r["title"],
            "status":          r["status"],
            "req_code":        r["req_code"],
            "approval_status": r["approval_status"],
            "openings":        int(r["openings"] or 1),
            "open_days":       round(open_days, 1),
            "rag":             rag,
            "stage_counts": {
                "applied":       int(r["cnt_applied"] or 0),
                "screen":        int(r["cnt_screen"] or 0),
                "nexai_bot":     int(r["cnt_nexai_bot"] or 0),
                "shortlisted":   int(r["cnt_shortlisted"] or 0),
                "interview":     int(r["cnt_interview"] or 0),
                "documentation": int(r["cnt_documentation"] or 0),
                "offered":       int(r["cnt_offered"] or 0),
                "hired":         int(r["cnt_hired"] or 0),
            },
        })

    # ── 5. KPI strip ─────────────────────────────────────────────────────────
    kpi_open = query_one(
        """SELECT COUNT(DISTINCT r.id) AS n
           FROM requisition r
           WHERE r.hiring_manager_id = %s
             AND r.status = 'open'
             AND COALESCE(r.approval_status, 'approved') = 'approved'""",
        [uid],
    )
    ph = ", ".join(["%s"] * len(_TERMINAL_TUPLE))
    kpi_pipeline = query_one(
        f"""SELECT COUNT(DISTINCT a.id) AS n
            FROM application a
            JOIN requisition r ON r.id = a.requisition_id
            WHERE r.hiring_manager_id = %s
              AND a.status NOT IN ({ph})
              AND COALESCE(r.approval_status, 'approved') = 'approved'""",
        [uid] + list(_TERMINAL_TUPLE),
    )
    avg_fb_row = query_one(
        """SELECT ROUND(AVG(
               EXTRACT(EPOCH FROM (now() - i.scheduled_at)) / 86400.0
           )::numeric, 1) AS avg_days
           FROM interview i
           JOIN application a  ON a.id = i.application_id
           JOIN requisition r  ON r.id = a.requisition_id
           JOIN interview_panel ip
                ON ip.interview_id = i.id AND ip.interviewer_id = %s
           LEFT JOIN scorecard s
                ON s.interview_id = i.id AND s.interviewer_id = %s
           WHERE r.hiring_manager_id = %s
             AND (s.id IS NULL OR s.status = 'draft')""",
        [uid, uid, uid],
    )
    breach_rows = query(
        f"""SELECT a.status,
                EXTRACT(EPOCH FROM (now() - COALESCE(
                    (SELECT se.occurred_at FROM stage_event se
                     WHERE se.application_id = a.id AND se.to_status = a.status
                     ORDER BY se.occurred_at DESC LIMIT 1),
                    a.applied_at
                ))) / 86400.0 AS elapsed_days
            FROM application a
            JOIN requisition r ON r.id = a.requisition_id
            WHERE r.hiring_manager_id = %s
              AND a.status NOT IN ({ph})""",
        [uid] + list(_TERMINAL_TUPLE),
    )
    red_count = amber_count = 0
    for row in (breach_rows or []):
        sla_key = STAGE_SLA_KEY.get(row["status"], "stage_default")
        target  = sla_cfg.get(sla_key, sla_cfg.get("stage_default", 5))
        rag     = compute_rag(row["elapsed_days"], target)
        if   rag["status"] == "red":   red_count   += 1
        elif rag["status"] == "amber": amber_count += 1

    kpi_strip = {
        "open_reqs":               int(kpi_open["n"])     if kpi_open     else 0,
        "candidates_in_pipeline":  int(kpi_pipeline["n"]) if kpi_pipeline else 0,
        "avg_days_pending_feedback": (
            float(avg_fb_row["avg_days"])
            if avg_fb_row and avg_fb_row["avg_days"] is not None else None
        ),
        "sla_breaches_red":   red_count,
        "sla_breaches_amber": amber_count,
    }

    return {
        "action_queue": {
            "pending_scorecards": pending_scorecards,
            "pending_approvals":  pending_approvals,
            "awaiting_decision":  awaiting_decision,
        },
        "my_reqs":   my_reqs,
        "kpi_strip": kpi_strip,
    }


# ── GET /api/hm/ta-pending-count (TA manager: badge count) ───────────────────

@router.get("/hm/ta-pending-count")
def ta_pending_count(user: dict = Depends(get_current_user)):
    _require_ta(user)
    row = query_one(
        "SELECT COUNT(*) AS n FROM requisition "
        "WHERE COALESCE(approval_status, 'approved') = 'pending_ta_approval'",
        [],
    )
    return {"count": int(row["n"]) if row else 0}


# ── GET /api/hm/ta-pending-reqs (TA manager: approval list) ──────────────────

@router.get("/hm/ta-pending-reqs")
def ta_pending_reqs(user: dict = Depends(get_current_user)):
    _require_ta(user)
    rows = query(
        """
        SELECT r.id, r.title, r.req_code, r.created_at, r.openings,
               COALESCE(r.created_by_role, '') AS created_by_role,
               r.job_description,
               r.min_experience, r.max_experience,
               r.key_skills,
               b.code  AS band,
               bu.name AS business_unit,
               creator.full_name AS created_by_name,
               creator.email    AS created_by_email,
               hm.full_name     AS hm_name,
               hm.email         AS hm_email
        FROM requisition r
        JOIN band b          ON b.id  = r.band_id
        JOIN business_unit bu ON bu.id = r.bu_id
        LEFT JOIN app_user creator ON creator.id = r.created_by
        LEFT JOIN app_user hm      ON hm.id      = r.hiring_manager_id
        WHERE COALESCE(r.approval_status, 'approved') = 'pending_ta_approval'
        ORDER BY r.created_at DESC
        """,
        [],
    )
    return rows or []


# ── POST /api/requisitions/{req_id}/ta-approve ────────────────────────────────

@router.post("/requisitions/{req_id}/ta-approve")
def ta_approve_requisition(req_id: str, user: dict = Depends(get_current_user)):
    _require_ta(user)

    req = query_one(
        "SELECT id, title, approval_status, hiring_manager_id FROM requisition WHERE id=%s",
        [req_id],
    )
    if not req:
        raise HTTPException(404, "Requisition not found")

    current = req.get("approval_status") or "approved"
    if current != "pending_ta_approval":
        raise HTTPException(
            400, f"Requisition is not pending TA approval (current: {current!r})"
        )

    query(
        "UPDATE requisition SET approval_status='approved', status='open' WHERE id=%s",
        [req_id], fetch=False,
    )

    if req.get("hiring_manager_id"):
        hm_email, hm_name = _user_email_name(str(req["hiring_manager_id"]))
        if hm_email:
            _send_safe("hm_req_approved", {
                "hm_name":   hm_name,
                "req_title": req["title"],
            }, [hm_email], req_id=req_id, actor=user)

    return {"ok": True, "approval_status": "approved"}


# ── POST /api/requisitions/{req_id}/ta-reject ─────────────────────────────────

class TARejectIn(BaseModel):
    reason: str


@router.post("/requisitions/{req_id}/ta-reject")
def ta_reject_requisition(
    req_id: str,
    body: TARejectIn,
    user: dict = Depends(get_current_user),
):
    _require_ta(user)

    req = query_one(
        "SELECT id, title, approval_status, hiring_manager_id FROM requisition WHERE id=%s",
        [req_id],
    )
    if not req:
        raise HTTPException(404, "Requisition not found")

    current = req.get("approval_status") or "approved"
    if current != "pending_ta_approval":
        raise HTTPException(
            400, f"Requisition is not pending TA approval (current: {current!r})"
        )

    query(
        "UPDATE requisition SET approval_status='rejected', status='closed' WHERE id=%s",
        [req_id], fetch=False,
    )

    if req.get("hiring_manager_id"):
        hm_email, hm_name = _user_email_name(str(req["hiring_manager_id"]))
        if hm_email:
            _send_safe("hm_req_rejected", {
                "hm_name":   hm_name,
                "req_title": req["title"],
                "reason":    body.reason,
            }, [hm_email], req_id=req_id, actor=user)

    return {"ok": True, "approval_status": "rejected"}
