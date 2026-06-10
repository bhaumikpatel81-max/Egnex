"""
Google OAuth 2.0 endpoints — per-recruiter Calendar integration.

  GET  /api/google/connect?recruiter_id=   redirect to Google consent screen
  GET  /api/google/callback                receive code, store tokens, redirect to /
  GET  /api/google/status?recruiter_id=    {linked, google_email}
  POST /api/google/disconnect?recruiter_id= revoke + delete stored tokens

The recruiter_id is carried through the OAuth round-trip via the `state`
parameter (base64-encoded JSON) so no server-side session is needed.
"""
import os
import json
import base64
from urllib.parse import quote as _urlquote
from datetime import timezone as _tz

import requests as _requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow

from ..db import query, query_one

router = APIRouter(prefix="/api/google", tags=["google-oauth"])

_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    # drive.readonly lets Egnex find Meet transcript docs saved to the organizer's Drive.
    # Existing users who connected before this change must reconnect (one click) to grant it.
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


def _client_config() -> dict:
    return {
        "web": {
            "client_id":     os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "redirect_uris": [os.environ["GOOGLE_REDIRECT_URI"]],
            "auth_uri":  "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def _encode_state(recruiter_id: str) -> str:
    raw = json.dumps({"rid": recruiter_id}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_state(state: str) -> str:
    pad = (4 - len(state) % 4) % 4
    payload = json.loads(base64.urlsafe_b64decode(state + "=" * pad))
    return payload["rid"]


@router.get("/connect")
def connect(recruiter_id: str):
    """Redirect the recruiter to Google's OAuth consent screen."""
    user = query_one(
        "SELECT id FROM app_user WHERE id = %s AND is_active = true",
        [recruiter_id],
    )
    if not user:
        raise HTTPException(404, "recruiter not found")

    flow = Flow.from_client_config(_client_config(), scopes=_SCOPES)
    flow.redirect_uri = os.environ["GOOGLE_REDIRECT_URI"]
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",              # force refresh_token on every connect
        state=_encode_state(recruiter_id),
    )
    return RedirectResponse(auth_url)


@router.get("/callback")
def callback(state: str, code: str | None = None, error: str | None = None):
    """Google redirects here. Exchange the code, fetch the email, store tokens."""
    if error:
        return RedirectResponse(f"/?gcal_error={_urlquote(error)}")
    if not code:
        raise HTTPException(400, "no authorisation code received from Google")

    try:
        recruiter_id = _decode_state(state)
    except Exception:
        raise HTTPException(400, "invalid OAuth state — restart the connect flow")

    flow = Flow.from_client_config(_client_config(), scopes=_SCOPES)
    flow.redirect_uri = os.environ["GOOGLE_REDIRECT_URI"]
    flow.fetch_token(code=code)
    creds = flow.credentials

    resp = _requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=10,
    )
    resp.raise_for_status()
    google_email = resp.json()["email"]

    # Store expiry as UTC-aware so Postgres TIMESTAMPTZ is unambiguous
    expiry_aware = (
        creds.expiry.replace(tzinfo=_tz.utc) if creds.expiry else None
    )
    scope_str = " ".join(creds.scopes) if creds.scopes else ""

    query(
        """INSERT INTO recruiter_google_token
             (user_id, google_email, access_token, refresh_token, token_expiry, scope)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (user_id) DO UPDATE
             SET google_email  = EXCLUDED.google_email,
                 access_token  = EXCLUDED.access_token,
                 refresh_token = COALESCE(EXCLUDED.refresh_token,
                                          recruiter_google_token.refresh_token),
                 token_expiry  = EXCLUDED.token_expiry,
                 scope         = EXCLUDED.scope,
                 updated_at    = now()""",
        [recruiter_id, google_email, creds.token,
         creds.refresh_token, expiry_aware, scope_str],
        fetch=False,
    )
    return RedirectResponse("/?connected=1")


@router.get("/status")
def status(recruiter_id: str):
    """Return link state for a recruiter: {linked, google_email, has_drive_scope}."""
    row = query_one(
        "SELECT google_email, token_expiry, scope FROM recruiter_google_token WHERE user_id = %s",
        [recruiter_id],
    )
    if not row:
        return {"linked": False, "google_email": None, "has_drive_scope": False}
    scope_str = row.get("scope") or ""
    return {
        "linked":          True,
        "google_email":    row["google_email"],
        "token_expiry":    row["token_expiry"].isoformat() if row["token_expiry"] else None,
        "has_drive_scope": _DRIVE_SCOPE in scope_str.split(),
    }


@router.get("/scope-check")
def scope_check(recruiter_id: str):
    """
    Return whether the recruiter's token includes the Drive read-only scope.
    Used by the Interviews screen to decide whether to show a 'reconnect' prompt
    before the 'Fetch Transcript' button.
    """
    row = query_one(
        "SELECT scope FROM recruiter_google_token WHERE user_id = %s",
        [recruiter_id],
    )
    if not row:
        return {"linked": False, "has_drive_scope": False}
    scope_str = row.get("scope") or ""
    has_drive = _DRIVE_SCOPE in scope_str.split()
    return {
        "linked":          True,
        "has_drive_scope": has_drive,
        "reconnect_url":   f"/api/google/connect?recruiter_id={recruiter_id}" if not has_drive else None,
    }


@router.post("/disconnect")
def disconnect(recruiter_id: str):
    """Revoke tokens on Google's side and delete them from the database."""
    row = query_one(
        "SELECT access_token, refresh_token FROM recruiter_google_token WHERE user_id = %s",
        [recruiter_id],
    )
    if not row:
        return {"disconnected": True}

    token = row["refresh_token"] or row["access_token"]
    try:
        _requests.post(
            "https://oauth2.googleapis.com/revoke",
            params={"token": token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
    except Exception:
        pass  # best-effort; delete locally regardless

    query(
        "DELETE FROM recruiter_google_token WHERE user_id = %s",
        [recruiter_id], fetch=False,
    )
    return {"disconnected": True}
