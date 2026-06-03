"""
Auth router — login, logout, and token verification.
Passwords hashed with bcrypt via pgcrypto (already in the DB).
Sessions are short-lived bearer tokens stored as SHA-256 hashes.
"""
import hashlib
import secrets

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..db import query, query_one

router = APIRouter(prefix="/api/auth", tags=["auth"])


def get_current_user(request: Request):
    """Validate Bearer token; return user dict or raise 401."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    token = auth[7:]
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    user = query_one(
        """SELECT u.id, u.full_name, u.email, u.role
           FROM user_session s
           JOIN app_user u ON u.id = s.user_id
           WHERE s.token_hash = %s AND s.expires_at > now()""",
        [token_hash],
    )
    if not user:
        raise HTTPException(401, "Session expired or invalid")
    return user


class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/login")
def login(payload: LoginIn):
    # pgcrypto's crypt() does the bcrypt comparison in one query
    user = query_one(
        """SELECT id, full_name, email, role
           FROM app_user
           WHERE email = %s
             AND is_active = true
             AND password_hash IS NOT NULL
             AND crypt(%s, password_hash) = password_hash""",
        [payload.email, payload.password],
    )
    if not user:
        raise HTTPException(401, "Invalid email or password")

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    query(
        "INSERT INTO user_session (user_id, token_hash, expires_at) "
        "VALUES (%s, %s, now() + interval '8 hours')",
        [str(user["id"]), token_hash],
        fetch=False,
    )
    return {
        "token": token,
        "user_id": str(user["id"]),
        "full_name": user["full_name"],
        "email": user["email"],
        "role": user["role"],
    }


@router.post("/logout")
def logout_ep(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        query("DELETE FROM user_session WHERE token_hash = %s", [token_hash], fetch=False)
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    return get_current_user(request)
