"""
SLA / RAG (Red-Amber-Green) helper.

RAG thresholds:
  GREEN  — elapsed < 80 % of target
  AMBER  — 80 % ≤ elapsed ≤ 100 % of target
  RED    — elapsed > 100 % (breached)

SLA config is stored in the sla_config table as (config_key, days).
Missing keys fall back to SLA_DEFAULTS.
"""
from ..db import query

# ── Defaults (editable via TA-manager settings screen) ───────────────────────

SLA_DEFAULTS = {
    "stage_applied":       5,
    "stage_screening":     3,
    "stage_screen_passed": 3,
    "stage_interviewing":  7,
    "stage_selected":      3,
    "stage_offer_stage":   5,
    "stage_default":       5,
    "req_time_to_fill":   45,
    "approval_step":       2,
}

# application.status  →  sla_config key
STAGE_SLA_KEY = {
    "applied":       "stage_applied",
    "screening":     "stage_screening",
    "screen_passed": "stage_screen_passed",
    "interviewing":  "stage_interviewing",
    "selected":      "stage_selected",
    "offer_stage":   "stage_offer_stage",
    "offered":       "stage_offer_stage",
    "offer_on_hold": "stage_offer_stage",
}

# Statuses for which SLA tracking is suspended (candidate no longer active)
TERMINAL = frozenset({
    "joined", "rejected", "screen_rejected", "dropped", "offer_cancelled",
})


# ── Config helpers ────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Return merged SLA config (DB overrides take precedence over defaults)."""
    rows = query("SELECT config_key, days FROM sla_config") or []
    cfg = dict(SLA_DEFAULTS)
    for r in rows:
        cfg[r["config_key"]] = int(r["days"])
    return cfg


# ── RAG computation ───────────────────────────────────────────────────────────

def compute_rag(elapsed_days, target_days) -> dict:
    """
    Return a RAG dict for elapsed time vs target.
    elapsed_days may be a float (from SQL EXTRACT).
    """
    if elapsed_days is None or target_days is None or target_days <= 0:
        return {
            "status": "green",
            "pct": 0.0,
            "elapsed_days": 0.0,
            "target_days": int(target_days) if target_days else 0,
        }
    elapsed_days = float(elapsed_days)
    target_days  = int(target_days)
    pct = (elapsed_days / target_days) * 100.0
    if pct < 80.0:
        status = "green"
    elif pct <= 100.0:
        status = "amber"
    else:
        status = "red"
    return {
        "status":       status,
        "pct":          round(pct, 1),
        "elapsed_days": round(elapsed_days, 1),
        "target_days":  target_days,
    }


# ── Bulk application-stage RAG ────────────────────────────────────────────────

def bulk_application_rag(app_ids: list[str], sla_cfg: dict | None = None) -> dict:
    """
    Given a list of application UUIDs, return {app_id: rag_dict} for each.
    Terminal-status applications get status='green' / no badge.
    sla_cfg is optional; if None, it is loaded from DB.
    """
    if not app_ids:
        return {}

    cfg = sla_cfg or load_config()

    # Pull status + time-in-current-stage for all requested apps in one query.
    # elapsed_days = time since the most-recent stage_event that set current status;
    # falls back to applied_at if no stage_event row exists yet.
    placeholders = ", ".join(["%s"] * len(app_ids))
    rows = query(
        f"""
        SELECT
            a.id AS app_id,
            a.status,
            EXTRACT(EPOCH FROM (
                now() - COALESCE(
                    (SELECT se.occurred_at
                     FROM stage_event se
                     WHERE se.application_id = a.id
                       AND se.to_status = a.status
                     ORDER BY se.occurred_at DESC
                     LIMIT 1),
                    a.applied_at
                )
            )) / 86400.0 AS elapsed_days
        FROM application a
        WHERE a.id IN ({placeholders})
        """,
        app_ids,
    )

    result = {}
    for r in (rows or []):
        app_id = str(r["app_id"])
        status = r["status"]
        if status in TERMINAL:
            result[app_id] = {"status": "green", "pct": 0.0,
                              "elapsed_days": 0.0, "target_days": 0}
            continue
        sla_key    = STAGE_SLA_KEY.get(status, "stage_default")
        target     = cfg.get(sla_key, cfg.get("stage_default", 5))
        rag        = compute_rag(r["elapsed_days"], target)
        rag["stage"] = status
        result[app_id] = rag

    return result


# ── Bulk requisition RAG ──────────────────────────────────────────────────────

def bulk_requisition_rag(req_ids: list[str], sla_cfg: dict | None = None) -> dict:
    """
    Given a list of requisition UUIDs, return {req_id: rag_dict}.
    Only 'open' requisitions get a non-green status; others return green.
    """
    if not req_ids:
        return {}

    cfg = sla_cfg or load_config()
    target = cfg.get("req_time_to_fill", SLA_DEFAULTS["req_time_to_fill"])

    placeholders = ", ".join(["%s"] * len(req_ids))
    rows = query(
        f"""
        SELECT
            id,
            status,
            EXTRACT(EPOCH FROM (
                now() - COALESCE(opened_at, created_at)
            )) / 86400.0 AS elapsed_days
        FROM requisition
        WHERE id IN ({placeholders})
        """,
        req_ids,
    )

    result = {}
    for r in (rows or []):
        req_id = str(r["id"])
        if r["status"] != "open":
            result[req_id] = {"status": "green", "pct": 0.0,
                              "elapsed_days": 0.0, "target_days": target}
        else:
            result[req_id] = compute_rag(r["elapsed_days"], target)

    return result
