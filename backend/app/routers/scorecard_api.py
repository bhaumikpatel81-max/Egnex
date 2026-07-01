"""
Panel interview scorecard / feedback API.

Endpoints
---------
GET  /api/interviews/{interview_id}/scorecard-form   form schema + caller's draft/submitted scorecard
POST /api/interviews/{interview_id}/scorecard        save draft or submit (panelists only)
GET  /api/interviews/{interview_id}/panel-feedback   aggregated panel results (role-gated, bias-guarded)
GET  /api/applications/{app_id}/panel-feedback       same, across all rounds for an application
"""
import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import get_current_user

router = APIRouter(prefix="/api", tags=["scorecard"])

# ── Default feedback form ─────────────────────────────────────────────────────

_DEFAULT_FORM_NAME = "Default Panel Scorecard"

_DEFAULT_FORM_SCHEMA = [
    {"key": "tech_skills",          "label": "Technical / Role Skills",  "type": "rating_5",     "required": True},
    {"key": "tech_skills_note",     "label": "Notes",                    "type": "text",         "required": False, "parent": "tech_skills"},
    {"key": "communication",        "label": "Communication",            "type": "rating_5",     "required": True},
    {"key": "communication_note",   "label": "Notes",                    "type": "text",         "required": False, "parent": "communication"},
    {"key": "problem_solving",      "label": "Problem-Solving",          "type": "rating_5",     "required": True},
    {"key": "problem_solving_note", "label": "Notes",                    "type": "text",         "required": False, "parent": "problem_solving"},
    {"key": "domain_fit",           "label": "Domain / Experience Fit",  "type": "rating_5",     "required": True},
    {"key": "domain_fit_note",      "label": "Notes",                    "type": "text",         "required": False, "parent": "domain_fit"},
    {"key": "culture_fit",          "label": "Culture / Values Fit",     "type": "rating_5",     "required": True},
    {"key": "culture_fit_note",     "label": "Notes",                    "type": "text",         "required": False, "parent": "culture_fit"},
    {"key": "overall_rating",       "label": "Overall Rating",           "type": "rating_5",     "required": True},
    {"key": "recommendation",       "label": "Recommendation",           "type": "single_choice","required": True,
     "options": ["Strong Hire", "Hire", "No Hire", "Strong No Hire"]},
    {"key": "strengths",            "label": "Strengths",                "type": "textarea",     "required": False},
    {"key": "concerns",             "label": "Concerns",                 "type": "textarea",     "required": False},
]

_VERDICT_MAP = {
    "Strong Hire":    "strong_yes",
    "Hire":           "yes",
    "No Hire":        "no",
    "Strong No Hire": "strong_no",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _j(val):
    """Safely parse JSONB — psycopg2 may return dict/list or a string."""
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except Exception:
        return None


def _ensure_default_form() -> dict:
    """Return the default feedback form, inserting it into DB if absent."""
    row = query_one(
        "SELECT id, schema FROM feedback_form WHERE name = %s AND is_active = TRUE LIMIT 1",
        [_DEFAULT_FORM_NAME],
    )
    if row:
        return {"id": str(row["id"]), "schema": _j(row["schema"]) or _DEFAULT_FORM_SCHEMA}
    inserted = query_one(
        "INSERT INTO feedback_form (name, schema) VALUES (%s, %s::jsonb) RETURNING id",
        [_DEFAULT_FORM_NAME, json.dumps(_DEFAULT_FORM_SCHEMA)],
    )
    if inserted:
        return {"id": str(inserted["id"]), "schema": _DEFAULT_FORM_SCHEMA}
    # Race: another request inserted it simultaneously
    row2 = query_one("SELECT id, schema FROM feedback_form WHERE name = %s LIMIT 1", [_DEFAULT_FORM_NAME])
    return {"id": str(row2["id"]), "schema": _j(row2["schema"]) or _DEFAULT_FORM_SCHEMA}


def _form_for_interview(interview_id: str) -> dict:
    """Return the feedback form for this interview (per round_config, or default)."""
    rc = query_one(
        """SELECT rc.feedback_form_id
           FROM interview i
           JOIN round_config rc ON rc.id = i.round_config_id
           WHERE i.id = %s""",
        [interview_id],
    )
    if rc and rc.get("feedback_form_id"):
        form_row = query_one(
            "SELECT id, schema FROM feedback_form WHERE id = %s AND is_active = TRUE",
            [str(rc["feedback_form_id"])],
        )
        if form_row:
            return {"id": str(form_row["id"]), "schema": _j(form_row["schema"]) or _DEFAULT_FORM_SCHEMA}
    return _ensure_default_form()


def _is_panelist(interview_id: str, user_id: str) -> bool:
    return bool(query_one(
        "SELECT 1 FROM interview_panel WHERE interview_id = %s AND interviewer_id = %s",
        [interview_id, user_id],
    ))


def _check_visibility(interview_id: str, user: dict) -> dict:
    """
    Return the interview row if caller is authorised to view data for this interview.
    Raises 404/403 otherwise.

    Visibility rules
    ----------------
    admin / ta_manager  → always
    recruiter           → application must be on one of their requisitions
    hiring_manager      → must be HM for the interview's requisition
    interviewer / other → must be in interview_panel for this interview
    """
    row = query_one(
        """SELECT i.id, i.application_id, i.status, i.scheduled_at,
                  c.full_name  AS candidate_name,
                  r.title      AS requisition,
                  r.id         AS req_id,
                  r.hiring_manager_id,
                  rc.name      AS round_name,
                  rc.id        AS round_config_id
           FROM interview i
           JOIN application  a  ON a.id  = i.application_id
           JOIN candidate    c  ON c.id  = a.candidate_id
           JOIN requisition  r  ON r.id  = a.requisition_id
           JOIN round_config rc ON rc.id = i.round_config_id
           WHERE i.id = %s""",
        [interview_id],
    )
    if not row:
        raise HTTPException(404, "Interview not found")

    role = user["role"]
    uid  = user["sub"]

    if role in ("admin", "ta_manager"):
        return row

    if role == "recruiter":
        if not query_one(
            "SELECT 1 FROM requisition_recruiter WHERE requisition_id = %s AND recruiter_id = %s",
            [str(row["req_id"]), uid],
        ):
            raise HTTPException(403, "Not authorised for this interview")
        return row

    if role == "hiring_manager":
        if str(row.get("hiring_manager_id")) != uid:
            raise HTTPException(403, "Not authorised for this interview")
        return row

    # interviewer / other role: must be on the panel
    if not _is_panelist(interview_id, uid):
        raise HTTPException(403, "You are not on the panel for this interview")
    return row


def _validate_required(schema: list, form_data: dict) -> list:
    """Return labels of required fields that are missing a value."""
    return [
        f["label"] for f in schema
        if f.get("required") and not form_data.get(f["key"])
    ]


def _overall_score(schema: list, form_data: dict) -> Optional[float]:
    rating_keys = {f["key"] for f in schema if f["type"] == "rating_5"}
    vals = [v for k, v in form_data.items()
            if k in rating_keys and isinstance(v, (int, float)) and 1 <= float(v) <= 5]
    return round(sum(vals) / len(vals), 2) if vals else None


# ── Panel consensus → combined score update (Improvement 5) ──────────────────

def _recompute_panel_combined(application_id: str) -> None:
    """
    Called after each scorecard submission.

    Consensus rule (configurable later; currently hardcoded):
      ≥ 60 % 'Strong Hire' or 'Hire'        → panel_consensus = 'advance'
      ≥ 60 % 'No Hire' or 'Strong No Hire'  → panel_consensus = 'reject'
      otherwise                              → panel_consensus = 'split'

    Updated combined score (when ≥ 1 scorecard AND bot_score exists):
      0.35 × match_score + 0.50 × bot_score + 0.15 × panel_numeric

    Both panel_consensus and panel_numeric are written into score_breakdown
    JSONB and panel_consensus is also stored as a dedicated column so list
    queries can surface the badge without parsing JSONB.
    """
    app_row = query_one(
        "SELECT match_score, bot_score, score_breakdown FROM application WHERE id = %s",
        [application_id],
    )
    if not app_row:
        return

    scs = query(
        """SELECT s.overall_score, s.verdict
           FROM scorecard s
           JOIN interview i ON i.id = s.interview_id
           WHERE i.application_id = %s AND s.status = 'submitted'""",
        [application_id],
    )
    if not scs:
        return

    scores = [float(sc["overall_score"]) for sc in scs if sc.get("overall_score") is not None]
    if not scores:
        return

    # Convert avg 1–5 → 0–100
    panel_numeric = round(sum(scores) / len(scores) / 5.0 * 100.0, 1)

    advance_verdicts = {"strong_yes", "yes"}
    reject_verdicts  = {"strong_no",  "no"}
    total  = len(scs)
    adv_ct = sum(1 for sc in scs if sc.get("verdict") in advance_verdicts)
    rej_ct = sum(1 for sc in scs if sc.get("verdict") in reject_verdicts)

    if total > 0 and adv_ct / total >= 0.60:
        panel_consensus = "advance"
    elif total > 0 and rej_ct / total >= 0.60:
        panel_consensus = "reject"
    else:
        panel_consensus = "split"

    # Update score_breakdown
    bd = app_row.get("score_breakdown") or {}
    if isinstance(bd, str):
        bd = json.loads(bd)
    bd["panel_numeric"]        = panel_numeric
    bd["panel_consensus"]      = panel_consensus
    bd["panel_submitted_count"] = total

    # Recompute combined_score only when bot_score is available
    match = float(app_row.get("match_score") or 0)
    bot   = app_row.get("bot_score")
    if bot is not None:
        combined = round(0.35 * match + 0.50 * float(bot) + 0.15 * panel_numeric, 1)
        query(
            """UPDATE application
               SET combined_score  = %s,
                   score_breakdown = %s::jsonb,
                   panel_consensus = %s
               WHERE id = %s""",
            [combined, json.dumps(bd), panel_consensus, application_id],
            fetch=False,
        )
    else:
        # Bot interview not done yet — persist panel info but don't touch combined_score
        query(
            """UPDATE application
               SET score_breakdown = %s::jsonb,
                   panel_consensus = %s
               WHERE id = %s""",
            [json.dumps(bd), panel_consensus, application_id],
            fetch=False,
        )


# ── GET scorecard form + caller's existing scorecard ─────────────────────────

@router.get("/interviews/{interview_id}/scorecard-form")
def get_scorecard_form(interview_id: str, user: dict = Depends(get_current_user)):
    uid  = user["sub"]
    role = user["role"]

    interview = _check_visibility(interview_id, user)
    form      = _form_for_interview(interview_id)

    my_sc = query_one(
        """SELECT id, form_data, overall_score, verdict, status, submitted_at, created_at
           FROM scorecard
           WHERE interview_id = %s AND interviewer_id = %s""",
        [interview_id, uid],
    )
    fd = _j(my_sc["form_data"]) if my_sc else {}

    is_panel     = _is_panelist(interview_id, uid)
    submitted_own = my_sc and my_sc.get("status") == "submitted"

    # Bias control: panelist may only see others after submitting their own.
    # Recruiters / HMs / TA / Admin who are NOT on the panel can always see all scores.
    can_see_others = (
        (is_panel and submitted_own) or
        role in ("admin", "ta_manager") or
        (role in ("recruiter", "hiring_manager") and not is_panel)
    )

    return {
        "interview": {
            "id":             str(interview["id"]),
            "candidate_name": interview["candidate_name"],
            "requisition":    interview["requisition"],
            "round_name":     interview["round_name"],
            "status":         interview["status"],
            "scheduled_at":   interview["scheduled_at"].isoformat() if interview.get("scheduled_at") else None,
        },
        "form":        form,
        "my_scorecard": {
            "id":           str(my_sc["id"]) if my_sc else None,
            "form_data":    fd or {},
            "overall_score": float(my_sc["overall_score"]) if my_sc and my_sc.get("overall_score") else None,
            "verdict":      my_sc.get("verdict") if my_sc else None,
            "status":       my_sc.get("status", "not_started") if my_sc else "not_started",
            "submitted_at": my_sc["submitted_at"].isoformat() if my_sc and my_sc.get("submitted_at") else None,
        },
        "is_panelist":    is_panel,
        "can_see_others": can_see_others,
    }


# ── POST save draft / submit scorecard ───────────────────────────────────────

class ScorecardIn(BaseModel):
    form_data: dict
    action: str  # "draft" or "submit"


@router.post("/interviews/{interview_id}/scorecard")
def save_scorecard(
    interview_id: str,
    body: ScorecardIn,
    user: dict = Depends(get_current_user),
):
    uid = user["sub"]

    if not _is_panelist(interview_id, uid):
        raise HTTPException(403, "Only panel members can submit scorecards")

    if not query_one("SELECT id FROM interview WHERE id = %s", [interview_id]):
        raise HTTPException(404, "Interview not found")

    existing = query_one(
        "SELECT id, status FROM scorecard WHERE interview_id = %s AND interviewer_id = %s",
        [interview_id, uid],
    )
    if existing and existing.get("status") == "submitted":
        raise HTTPException(409, "Scorecard already submitted and locked")

    form   = _form_for_interview(interview_id)
    action = (body.action or "draft").lower()

    if action == "submit":
        missing = _validate_required(form["schema"], body.form_data)
        if missing:
            raise HTTPException(422, f"Required fields missing: {', '.join(missing)}")

    score    = _overall_score(form["schema"], body.form_data)
    verdict  = _VERDICT_MAP.get(body.form_data.get("recommendation"))
    form_json = json.dumps(body.form_data)
    status   = "submitted" if action == "submit" else "draft"

    if existing:
        if action == "submit":
            query(
                """UPDATE scorecard
                   SET form_data = %s::jsonb, overall_score = %s, verdict = %s,
                       status = %s, feedback_form_id = %s, submitted_at = now()
                   WHERE interview_id = %s AND interviewer_id = %s""",
                [form_json, score, verdict, status, form["id"], interview_id, uid],
                fetch=False,
            )
        else:
            query(
                """UPDATE scorecard
                   SET form_data = %s::jsonb, overall_score = %s, verdict = %s,
                       status = %s, feedback_form_id = %s
                   WHERE interview_id = %s AND interviewer_id = %s""",
                [form_json, score, verdict, status, form["id"], interview_id, uid],
                fetch=False,
            )
    else:
        if action == "submit":
            query(
                """INSERT INTO scorecard
                   (interview_id, interviewer_id, feedback_form_id,
                    form_data, overall_score, verdict, status, submitted_at)
                   VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, now())""",
                [interview_id, uid, form["id"], form_json, score, verdict, status],
                fetch=False,
            )
        else:
            query(
                """INSERT INTO scorecard
                   (interview_id, interviewer_id, feedback_form_id,
                    form_data, overall_score, verdict, status)
                   VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)""",
                [interview_id, uid, form["id"], form_json, score, verdict, status],
                fetch=False,
            )

    updated = query_one(
        """SELECT id, overall_score, verdict, status, submitted_at
           FROM scorecard
           WHERE interview_id = %s AND interviewer_id = %s""",
        [interview_id, uid],
    )

    # Improvement 5: recompute panel consensus + combined score on every submit
    if action == "submit":
        iv_row = query_one(
            """SELECT i.application_id, a.requisition_id
               FROM interview i
               JOIN application a ON a.id = i.application_id
               WHERE i.id = %s""",
            [interview_id],
        )
        if iv_row and iv_row.get("application_id"):
            try:
                _recompute_panel_combined(str(iv_row["application_id"]))
            except Exception as _pc_exc:
                print(f"[scorecard] panel combined recompute failed: {_pc_exc}")
            # Gamification: panel_pass when positive recommendation
            try:
                from ..services.gamification import award as _gam_award
                req_id_str = str(iv_row["requisition_id"]) if iv_row.get("requisition_id") else None
                app_id_str = str(iv_row["application_id"])
                if verdict in ("strong_yes", "yes"):
                    _gam_award("recruiter", uid, "panel_pass", req_id_str, app_id_str)
                _gam_award("recruiter", uid, "feedback_on_time", req_id_str, app_id_str)
            except Exception as _ge_exc:
                print(f"[scorecard] gamification award failed: {_ge_exc}")

    return {
        "ok": True,
        "scorecard": {
            "id":           str(updated["id"]),
            "status":       updated["status"],
            "overall_score": float(updated["overall_score"]) if updated.get("overall_score") else None,
            "verdict":      updated.get("verdict"),
            "submitted_at": updated["submitted_at"].isoformat() if updated.get("submitted_at") else None,
        },
    }


def _derive_consensus(verdict_counts: dict, total: int) -> Optional[str]:
    """Return panel_consensus label from verdict histogram, or None if no scorecards."""
    if total == 0:
        return None
    adv = sum(v for k, v in verdict_counts.items() if k in ("Strong Hire", "Hire"))
    rej = sum(v for k, v in verdict_counts.items() if k in ("No Hire", "Strong No Hire"))
    if adv / total >= 0.60:
        return "advance"
    if rej / total >= 0.60:
        return "reject"
    return "split"


# ── GET aggregated panel feedback for an interview ───────────────────────────

@router.get("/interviews/{interview_id}/panel-feedback")
def get_panel_feedback(interview_id: str, user: dict = Depends(get_current_user)):
    uid  = user["sub"]
    role = user["role"]

    interview = _check_visibility(interview_id, user)

    # Bias guard: a panelist who hasn't submitted cannot see others' scores
    if _is_panelist(interview_id, uid) and role not in ("admin", "ta_manager"):
        my_sc = query_one(
            "SELECT status FROM scorecard WHERE interview_id = %s AND interviewer_id = %s",
            [interview_id, uid],
        )
        if not my_sc or my_sc.get("status") != "submitted":
            raise HTTPException(403, "Submit your own scorecard before viewing others")

    form         = _form_for_interview(interview_id)
    rating_keys  = [f["key"]   for f in form["schema"] if f["type"] == "rating_5"]
    rating_labels = {f["key"]: f["label"] for f in form["schema"] if f["type"] == "rating_5"}

    scs = query(
        """SELECT s.form_data, s.overall_score, s.verdict, s.submitted_at,
                  u.full_name AS interviewer_name, u.role AS interviewer_role
           FROM scorecard s
           JOIN app_user u ON u.id = s.interviewer_id
           WHERE s.interview_id = %s AND s.status = 'submitted'
           ORDER BY s.submitted_at""",
        [interview_id],
    )

    entries = []
    for sc in (scs or []):
        fd = _j(sc["form_data"]) or {}
        entries.append({
            "interviewer_name": sc["interviewer_name"],
            "interviewer_role": sc["interviewer_role"],
            "verdict":          sc["verdict"],
            "overall_score":    float(sc["overall_score"]) if sc.get("overall_score") else None,
            "recommendation":   fd.get("recommendation"),
            "strengths":        fd.get("strengths"),
            "concerns":         fd.get("concerns"),
            "ratings":          {k: fd[k] for k in rating_keys if fd.get(k)},
            "submitted_at":     sc["submitted_at"].isoformat() if sc.get("submitted_at") else None,
        })

    # Roll-up
    verdict_counts: dict = {}
    for e in entries:
        r = e.get("recommendation")
        if r:
            verdict_counts[r] = verdict_counts.get(r, 0) + 1

    scores    = [e["overall_score"] for e in entries if e.get("overall_score")]
    avg_all   = round(sum(scores) / len(scores), 2) if scores else None

    avg_ratings: dict = {}
    for k in rating_keys:
        vals = [e["ratings"][k] for e in entries if e["ratings"].get(k)]
        if vals:
            avg_ratings[k] = round(sum(vals) / len(vals), 2)

    panel = query(
        """SELECT u.full_name, u.role,
                  (s.id IS NOT NULL)        AS has_scorecard,
                  (s.status = 'submitted')  AS submitted
           FROM interview_panel ip
           JOIN app_user u ON u.id = ip.interviewer_id
           LEFT JOIN scorecard s
             ON s.interview_id = ip.interview_id AND s.interviewer_id = ip.interviewer_id
           WHERE ip.interview_id = %s
           ORDER BY u.full_name""",
        [interview_id],
    )

    return {
        "interview": {
            "id":             str(interview["id"]),
            "candidate_name": interview["candidate_name"],
            "requisition":    interview["requisition"],
            "round_name":     interview["round_name"],
            "status":         interview["status"],
        },
        "panel_members": [
            {"full_name": m["full_name"], "role": m["role"], "submitted": bool(m["submitted"])}
            for m in (panel or [])
        ],
        "scorecards": entries,
        "rollup": {
            "total_submitted":   len(entries),
            "verdict_counts":    verdict_counts,
            "avg_overall_score": avg_all,
            "avg_ratings":       avg_ratings,
            "rating_labels":     rating_labels,
            "panel_consensus":   _derive_consensus(verdict_counts, len(entries)),
        },
    }


# ── GET aggregated panel feedback across all rounds for an application ────────

@router.get("/applications/{app_id}/panel-feedback")
def get_application_panel_feedback(app_id: str, user: dict = Depends(get_current_user)):
    role = user["role"]
    uid  = user["sub"]

    app_row = query_one(
        """SELECT a.id, a.requisition_id, r.title AS requisition,
                  r.hiring_manager_id, c.full_name AS candidate_name
           FROM application a
           JOIN requisition r ON r.id = a.requisition_id
           JOIN candidate   c ON c.id = a.candidate_id
           WHERE a.id = %s""",
        [app_id],
    )
    if not app_row:
        raise HTTPException(404, "Application not found")

    if role == "recruiter":
        if not query_one(
            "SELECT 1 FROM requisition_recruiter WHERE requisition_id = %s AND recruiter_id = %s",
            [str(app_row["requisition_id"]), uid],
        ):
            raise HTTPException(403, "Not authorised")
    elif role == "hiring_manager":
        if str(app_row.get("hiring_manager_id")) != uid:
            raise HTTPException(403, "Not authorised")
    elif role not in ("admin", "ta_manager"):
        raise HTTPException(403, "Not authorised")

    interviews = query(
        """SELECT i.id, i.status, i.scheduled_at,
                  rc.name AS round_name, rc.sequence
           FROM interview i
           JOIN round_config rc ON rc.id = i.round_config_id
           WHERE i.application_id = %s
           ORDER BY rc.sequence, i.scheduled_at""",
        [app_id],
    )

    rounds = []
    for iv in (interviews or []):
        iv_id = str(iv["id"])
        scs   = query(
            """SELECT s.form_data, s.overall_score, s.verdict, s.submitted_at,
                      u.full_name AS interviewer_name, u.role AS interviewer_role
               FROM scorecard s
               JOIN app_user u ON u.id = s.interviewer_id
               WHERE s.interview_id = %s AND s.status = 'submitted'
               ORDER BY s.submitted_at""",
            [iv_id],
        )
        form       = _form_for_interview(iv_id)
        rating_keys = [f["key"] for f in form["schema"] if f["type"] == "rating_5"]

        entries = []
        for sc in (scs or []):
            fd = _j(sc["form_data"]) or {}
            entries.append({
                "interviewer_name": sc["interviewer_name"],
                "interviewer_role": sc["interviewer_role"],
                "verdict":          sc["verdict"],
                "overall_score":    float(sc["overall_score"]) if sc.get("overall_score") else None,
                "recommendation":   fd.get("recommendation"),
                "ratings":          {k: fd[k] for k in rating_keys if fd.get(k)},
                "strengths":        fd.get("strengths"),
                "concerns":         fd.get("concerns"),
                "submitted_at":     sc["submitted_at"].isoformat() if sc.get("submitted_at") else None,
            })

        vc: dict = {}
        for e in entries:
            r = e.get("recommendation")
            if r:
                vc[r] = vc.get(r, 0) + 1

        scores = [e["overall_score"] for e in entries if e.get("overall_score")]
        rounds.append({
            "interview_id": iv_id,
            "round_name":   iv["round_name"],
            "sequence":     iv["sequence"],
            "status":       iv["status"],
            "scheduled_at": iv["scheduled_at"].isoformat() if iv.get("scheduled_at") else None,
            "scorecards":   entries,
            "rollup": {
                "total_submitted":  len(entries),
                "verdict_counts":   vc,
                "avg_overall_score": round(sum(scores) / len(scores), 2) if scores else None,
            },
        })

    return {
        "candidate_name": app_row["candidate_name"],
        "requisition":    app_row["requisition"],
        "rounds":         rounds,
    }
