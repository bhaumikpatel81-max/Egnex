from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import hash_password, require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])

_VALID_ROLES = {
    "admin", "ta_manager", "recruiter",
    "hiring_manager", "bu_head", "director", "interviewer",
}

_USER_COLS = "id, full_name, email, role, is_active, created_at"


class CreateUserIn(BaseModel):
    full_name: str
    email: str
    role: str
    password: str


class UpdateUserIn(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


class ResetPasswordIn(BaseModel):
    new_password: str


@router.get("/users")
def list_users(admin=Depends(require_admin)):
    return query(
        f"SELECT {_USER_COLS} FROM app_user ORDER BY created_at DESC"
    )


@router.post("/users", status_code=201)
def create_user(body: CreateUserIn, admin=Depends(require_admin)):
    if body.role not in _VALID_ROLES:
        raise HTTPException(400, f"Invalid role. Choose from: {sorted(_VALID_ROLES)}")
    if len(body.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if query_one("SELECT id FROM app_user WHERE email = %s", [body.email.lower()]):
        raise HTTPException(400, "A user with that email already exists")
    row = query_one(
        f"""INSERT INTO app_user (full_name, email, role, password_hash)
            VALUES (%s, %s, %s, %s) RETURNING {_USER_COLS}""",
        [body.full_name, body.email.lower(), body.role, hash_password(body.password)],
    )
    return row


@router.patch("/users/{user_id}")
def update_user(user_id: str, body: UpdateUserIn, admin=Depends(require_admin)):
    if body.role and body.role not in _VALID_ROLES:
        raise HTTPException(400, f"Invalid role. Choose from: {sorted(_VALID_ROLES)}")
    sets, params = [], []
    if body.full_name is not None:
        sets.append("full_name = %s"); params.append(body.full_name)
    if body.role is not None:
        sets.append("role = %s"); params.append(body.role)
    if body.is_active is not None:
        sets.append("is_active = %s"); params.append(body.is_active)
    if not sets:
        raise HTTPException(400, "Nothing to update")
    params.append(user_id)
    row = query_one(
        f"UPDATE app_user SET {', '.join(sets)} WHERE id = %s RETURNING {_USER_COLS}",
        params,
    )
    if not row:
        raise HTTPException(404, "User not found")
    return row


@router.delete("/users/{user_id}")
def deactivate_user(user_id: str, admin=Depends(require_admin)):
    row = query_one(
        "UPDATE app_user SET is_active = false WHERE id = %s RETURNING id", [user_id]
    )
    if not row:
        raise HTTPException(404, "User not found")
    return {"deactivated": True}


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: str, body: ResetPasswordIn, admin=Depends(require_admin)):
    if len(body.new_password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    row = query_one(
        "UPDATE app_user SET password_hash = %s WHERE id = %s RETURNING id",
        [hash_password(body.new_password), user_id],
    )
    if not row:
        raise HTTPException(404, "User not found")
    return {"ok": True}
