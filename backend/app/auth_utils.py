"""
JWT + bcrypt utilities shared across auth and admin routers.
"""
import os
from datetime import datetime, timedelta

import bcrypt as _bcrypt

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

_bearer = HTTPBearer(auto_error=False)

SECRET_KEY = os.environ.get("JWT_SECRET", "").strip()
if not SECRET_KEY:
    # Allow a dev default ONLY when not in production.
    if os.environ.get("ENV", "").lower() in ("prod", "production"):
        raise RuntimeError(
            "JWT_SECRET is not set. Add a long random JWT_SECRET to .env.prod "
            "before starting in production."
        )
    SECRET_KEY = "egnex-dev-secret-change-in-prod"
ALGORITHM = "HS256"
TOKEN_HOURS = 8


def hash_password(plain: str) -> str:
    return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt(rounds=10)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def create_token(user: dict) -> str:
    expire = datetime.utcnow() + timedelta(hours=TOKEN_HOURS)
    return jwt.encode(
        {
            "sub": str(user["id"]),
            "email": user["email"],
            "role": user["role"],
            "name": user["full_name"],
            "exp": expire,
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def _decode(token: str) -> dict:
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    if not creds:
        raise HTTPException(401, "Not authenticated")
    try:
        return _decode(creds.credentials)
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin access required")
    return user


def require_admin_or_manager(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") not in ("admin", "ta_manager"):
        raise HTTPException(403, "Admin or TA Manager access required")
    return user
