"""Role computation and FastAPI dependencies for RBAC."""

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session as DBSession

from auth import validate_session
from database import get_db
from models import User


def compute_role(user: User) -> str:
    """Return the highest applicable role name for a user."""
    if user.is_admin:
        return "admin"
    if user.is_deploy_admin:
        return "deploy_admin"
    if user.is_flag_admin:
        return "flag_admin"
    if user.is_engineer:
        return "engineer"
    return "viewer"


def compute_permissions(user: User) -> dict:
    """Return a flat permission dict for the frontend."""
    role = compute_role(user)
    return {
        "can_toggle_dev": role in ("engineer", "flag_admin", "deploy_admin", "admin"),
        "can_toggle_uat": role in ("engineer", "flag_admin", "deploy_admin", "admin"),
        "can_toggle_prod": role in ("flag_admin", "admin"),
        "can_create_flag": role in ("engineer", "flag_admin", "deploy_admin", "admin"),
        "can_archive_flag": role in ("flag_admin", "admin"),
        "can_deploy": role in ("deploy_admin", "admin"),
        "can_admin": role == "admin",
    }


async def get_current_user(
    request: Request, db: DBSession = Depends(get_db)
) -> User:
    """FastAPI dependency — extract the authenticated user from the session
    cookie. Raises 401 if no valid session."""
    token = request.cookies.get("crown_session")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = validate_session(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired or invalid")

    return user


def require_role(*roles: str):
    """Return a FastAPI dependency that checks the user has one of the
    specified roles. Raises 403 if not."""

    async def dependency(user: User = Depends(get_current_user)) -> User:
        role = compute_role(user)
        if role not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"Requires one of: {', '.join(roles)}. You have: {role}",
            )
        return user

    return dependency
