from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import query, query_one
from ..auth_utils import create_token, get_current_user, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str
    password: str


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str


@router.post("/login")
def login(body: LoginIn):
    user = query_one(
        "SELECT id, full_name, email, role, password_hash, is_active "
        "FROM app_user WHERE email = %s",
        [body.email.lower().strip()],
    )
    if not user or not user["is_active"]:
        raise HTTPException(401, "Invalid email or password")
    if not user["password_hash"]:
        raise HTTPException(401, "Account not set up — contact your admin")
    if not verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    return {
        "token": create_token(dict(user)),
        "role": user["role"],
        "name": user["full_name"],
    }


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return user


@router.post("/change-password")
def change_password(body: ChangePasswordIn, user: dict = Depends(get_current_user)):
    if len(body.new_password) < 6:
        raise HTTPException(400, "New password must be at least 6 characters")
    row = query_one(
        "SELECT password_hash FROM app_user WHERE id = %s", [user["sub"]]
    )
    if not row or not verify_password(body.old_password, row["password_hash"]):
        raise HTTPException(400, "Current password is incorrect")
    query(
        "UPDATE app_user SET password_hash = %s WHERE id = %s",
        [hash_password(body.new_password), user["sub"]],
        fetch=False,
    )
    return {"ok": True}
