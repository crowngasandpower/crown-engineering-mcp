"""Crown Engineering Dashboard API.

Authenticates users via LDAP, enforces role-based access control, proxies
requests to upstream services (Unleash, Jenkins, GitHub), and logs all
mutations to an audit trail.

Endpoints:
  POST /auth/login         — LDAP login, set session cookie
  POST /auth/logout        — clear session
  GET  /auth/me            — current user + permissions

  GET  /api/flags          — list flags (any user)
  GET  /api/flags/apps     — flag app lookup (any user)
  GET  /api/tokens         — unleash tokens (any user)
  POST /api/flags          — create flag (engineer+)
  POST /api/flags/{n}/toggle — toggle flag (env-dependent)
  DELETE /api/flags/{n}    — archive flag (flag_admin+)
  POST /api/flags/{n}/cleanup-ticket — create cleanup ticket (engineer+)
  GET  /api/flags/{n}      — get flag (any user)

  POST /deploy/preview     — deploy to preview (deploy_admin+)
  POST /deploy/promote     — promote to live (deploy_admin+)
  POST /deploy/rollback    — rollback (deploy_admin+)

  GET  /github/{path}      — proxy to GitHub API (any user)

  GET  /admin/users        — list users (admin only)
  POST /admin/users/{u}/permissions — update permissions (admin only)
  GET  /admin/audit        — audit log (admin only)
"""

import os
from datetime import datetime, timezone

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session as DBSession

from auth import (
    authenticate_user,
    create_session,
    delete_session,
    purge_expired_sessions,
    upsert_user,
)
from database import create_tables, get_db
from models import AuditLog, User
from rbac import (
    compute_permissions,
    compute_role,
    get_current_user,
    require_role,
)

UNLEASH_API_URL = os.environ.get("UNLEASH_API_URL", "http://unleash-api:3000")
JENKINS_BASE_URL = os.environ.get("JENKINS_BASE_URL", "http://host.docker.internal:8090")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_ORG = os.environ.get("GITHUB_ORG", "crowngasandpower")

app = FastAPI(title="Crown Dashboard API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://poc-containers:9512"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.on_event("startup")
def startup():
    create_tables()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip", request.client.host if request.client else "")


def _audit(db: DBSession, username: str, action: str, target: str, detail: str, ip: str):
    db.add(AuditLog(
        timestamp=datetime.now(timezone.utc).isoformat(),
        username=username,
        action=action,
        target=target,
        detail=detail,
        ip=ip,
    ))
    db.commit()


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
async def login(body: LoginRequest, request: Request, response: Response, db: DBSession = Depends(get_db)):
    ldap_info = authenticate_user(body.username, body.password)
    if not ldap_info:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user = upsert_user(db, body.username, ldap_info)
    ip = _client_ip(request)
    token = create_session(db, user.id, ip)

    purge_expired_sessions(db)

    _audit(db, user.username, "login", user.username, f"Login from {ip}", ip)

    response.set_cookie(
        key="crown_session",
        value=token,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=8 * 3600,
        # secure=True when HTTPS is enabled; currently HTTP-only internal network
    )

    return {
        "user": {
            "username": user.username,
            "display_name": user.display_name,
            "role": compute_role(user),
        },
        "message": "Logged in",
    }


@app.post("/auth/logout")
async def logout(request: Request, response: Response, db: DBSession = Depends(get_db)):
    token = request.cookies.get("crown_session")
    if token:
        from auth import validate_session
        user = validate_session(db, token)
        if user:
            _audit(db, user.username, "logout", user.username, "", _client_ip(request))
        delete_session(db, token)

    response.delete_cookie("crown_session", path="/")
    return {"message": "Logged out"}


@app.get("/auth/me")
async def me(response: Response, user: User = Depends(get_current_user)):
    # X-Auth-User header is consumed by the Grafana auth proxy (nginx
    # auth_request_set reads it from the subrequest response).
    response.headers["X-Auth-User"] = user.username
    return {
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "role": compute_role(user),
        "permissions": compute_permissions(user),
    }


# ---------------------------------------------------------------------------
# Flag proxy routes
# ---------------------------------------------------------------------------

async def _proxy_unleash(method: str, path: str, body: dict | None = None) -> dict:
    """Proxy a request to the Unleash API wrapper."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        url = f"{UNLEASH_API_URL}/{path}"
        if method == "GET":
            resp = await client.get(url)
        elif method == "POST":
            resp = await client.post(url, json=body or {})
        elif method == "DELETE":
            resp = await client.delete(url)
        else:
            raise HTTPException(405, f"Unsupported method: {method}")

        if not 200 <= resp.status_code < 300:
            raise HTTPException(resp.status_code, resp.text[:500])

        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()


@app.get("/api/flags")
async def list_flags(_: User = Depends(get_current_user)):
    return await _proxy_unleash("GET", "flags")


@app.get("/api/flags/apps")
async def flags_apps(_: User = Depends(get_current_user)):
    return await _proxy_unleash("GET", "flags/apps")


@app.get("/api/tokens")
async def get_tokens(_: User = Depends(get_current_user)):
    return await _proxy_unleash("GET", "tokens")


@app.get("/api/flags/{name}")
async def get_flag(name: str, _: User = Depends(get_current_user)):
    return await _proxy_unleash("GET", f"flags/{name}")


class CreateFlagRequest(BaseModel):
    ticket_key: str
    short_name: str
    description: str = ""
    type: str = "release"


@app.post("/api/flags")
async def create_flag(
    body: CreateFlagRequest,
    request: Request,
    user: User = Depends(require_role("engineer", "flag_admin", "deploy_admin", "admin")),
    db: DBSession = Depends(get_db),
):
    result = await _proxy_unleash("POST", "flags", body.model_dump())
    _audit(db, user.username, "flag_create", f"{body.ticket_key}-{body.short_name}",
           f"type={body.type}", _client_ip(request))
    return result


class ToggleFlagRequest(BaseModel):
    environment: str
    enabled: bool


@app.post("/api/flags/{name}/toggle")
async def toggle_flag(
    name: str,
    body: ToggleFlagRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    perms = compute_permissions(user)
    env_lower = body.environment.lower()

    if env_lower in ("development", "uat", "dev") and not perms["can_toggle_dev"]:
        raise HTTPException(403, "You don't have permission to toggle dev/UAT flags")
    if env_lower in ("production", "prod") and not perms["can_toggle_prod"]:
        raise HTTPException(403, "Only flag admins and admins can toggle production flags")

    result = await _proxy_unleash("POST", f"flags/{name}/toggle", body.model_dump())
    _audit(db, user.username, "flag_toggle", name,
           f"{body.environment}: {'enabled' if body.enabled else 'disabled'}",
           _client_ip(request))
    return result


@app.delete("/api/flags/{name}")
async def archive_flag(
    name: str,
    request: Request,
    user: User = Depends(require_role("flag_admin", "admin")),
    db: DBSession = Depends(get_db),
):
    result = await _proxy_unleash("DELETE", f"flags/{name}")
    _audit(db, user.username, "flag_archive", name, "", _client_ip(request))
    return result


@app.post("/api/flags/{name}/cleanup-ticket")
async def cleanup_ticket(
    name: str,
    request: Request,
    user: User = Depends(require_role("engineer", "flag_admin", "deploy_admin", "admin")),
    db: DBSession = Depends(get_db),
):
    result = await _proxy_unleash("POST", f"flags/{name}/cleanup-ticket")
    _audit(db, user.username, "flag_cleanup_ticket", name, "", _client_ip(request))
    return result


# ---------------------------------------------------------------------------
# Deploy proxy routes
# ---------------------------------------------------------------------------

ALLOWED_APPS = {
    'aerio', 'billy', 'ces', 'ces-elec', 'complaintstracker',
    'cva-dataflow-validation', 'dds', 'dds-elec', 'doc-master',
    'doc-master-elec', 'eps', 'esema', 'gateway', 'gateway-elec',
    'gps', 'jigsaw', 'jigsaw-elec', 'meter-data-manager',
    'multisite-manager', 'pims', 'pims-elec', 'portal-gas-admin',
    'portal-power-admin', 'reporting', 'revenue-dollar',
    'revenue-dollar-elec', 'sanctions', 'settlements',
    'settlements-elec', 'synergy', 'wolfy', 'wolfy-elec',
}


class DeployRequest(BaseModel):
    app: str
    branch: str = ""


def _validate_deploy(body: DeployRequest):
    if body.app not in ALLOWED_APPS:
        raise HTTPException(422, f"Unknown app: {body.app}")
    if body.branch and not all(c.isalnum() or c in '-_./+' for c in body.branch):
        raise HTTPException(422, "Invalid branch name")


@app.post("/deploy/preview")
async def deploy_preview(
    body: DeployRequest,
    request: Request,
    user: User = Depends(require_role("deploy_admin", "admin")),
    db: DBSession = Depends(get_db),
):
    _validate_deploy(body)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{JENKINS_BASE_URL}/job/prod-deploy/buildWithParameters",
            params={"APP": body.app, "BRANCH": body.branch},
        )
    if resp.status_code not in (200, 201):
        raise HTTPException(502, f"Jenkins returned HTTP {resp.status_code}")

    _audit(db, user.username, "deploy", body.app, f"branch={body.branch}", _client_ip(request))
    return {"message": f"Deploy triggered for {body.app} branch {body.branch}"}


@app.post("/deploy/promote")
async def deploy_promote(
    body: DeployRequest,
    request: Request,
    user: User = Depends(require_role("deploy_admin", "admin")),
    db: DBSession = Depends(get_db),
):
    _validate_deploy(body)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{JENKINS_BASE_URL}/job/prod-promote/buildWithParameters",
            params={"APP": body.app},
        )
    if resp.status_code not in (200, 201):
        raise HTTPException(502, f"Jenkins returned HTTP {resp.status_code}")

    _audit(db, user.username, "promote", body.app, "", _client_ip(request))
    return {"message": f"Promote triggered for {body.app}"}


@app.post("/deploy/rollback")
async def deploy_rollback(
    body: DeployRequest,
    request: Request,
    user: User = Depends(require_role("deploy_admin", "admin")),
    db: DBSession = Depends(get_db),
):
    _validate_deploy(body)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{JENKINS_BASE_URL}/job/prod-rollback/buildWithParameters",
            params={"APP": body.app},
        )
    if resp.status_code not in (200, 201):
        raise HTTPException(502, f"Jenkins returned HTTP {resp.status_code}")

    _audit(db, user.username, "rollback", body.app, "", _client_ip(request))
    return {"message": f"Rollback triggered for {body.app}"}


# ---------------------------------------------------------------------------
# GitHub proxy
# ---------------------------------------------------------------------------

@app.get("/github/{path:path}")
async def proxy_github(path: str, request: Request, _: User = Depends(get_current_user)):
    """Proxy GET requests to the GitHub API with the server-side token.
    Only allows repos/{org}/* paths to prevent token misuse."""
    if not path.startswith(f"repos/{GITHUB_ORG}/"):
        raise HTTPException(403, "Only repository endpoints are allowed")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"https://api.github.com/{path}",
            params=dict(request.query_params),
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
        )
    if not 200 <= resp.status_code < 300:
        raise HTTPException(resp.status_code, resp.text[:500])
    return resp.json()


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@app.get("/admin/users")
async def list_users(
    _: User = Depends(require_role("admin")),
    db: DBSession = Depends(get_db),
):
    users = db.query(User).order_by(User.last_login.desc()).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name,
            "email": u.email,
            "role": compute_role(u),
            "is_engineer": bool(u.is_engineer),
            "is_flag_admin": bool(u.is_flag_admin),
            "is_deploy_admin": bool(u.is_deploy_admin),
            "is_admin": bool(u.is_admin),
            "first_login": u.first_login,
            "last_login": u.last_login,
        }
        for u in users
    ]


class PermissionsUpdate(BaseModel):
    is_flag_admin: bool | None = None
    is_deploy_admin: bool | None = None
    is_admin: bool | None = None


@app.post("/admin/users/{username}/permissions")
async def update_permissions(
    username: str,
    body: PermissionsUpdate,
    request: Request,
    admin: User = Depends(require_role("admin")),
    db: DBSession = Depends(get_db),
):
    target = db.query(User).filter(User.username == username).first()
    if not target:
        raise HTTPException(404, f"User {username} not found")

    changes = []
    if body.is_flag_admin is not None and bool(target.is_flag_admin) != body.is_flag_admin:
        changes.append(f"is_flag_admin: {bool(target.is_flag_admin)} -> {body.is_flag_admin}")
        target.is_flag_admin = int(body.is_flag_admin)
    if body.is_deploy_admin is not None and bool(target.is_deploy_admin) != body.is_deploy_admin:
        changes.append(f"is_deploy_admin: {bool(target.is_deploy_admin)} -> {body.is_deploy_admin}")
        target.is_deploy_admin = int(body.is_deploy_admin)
    if body.is_admin is not None and bool(target.is_admin) != body.is_admin:
        changes.append(f"is_admin: {bool(target.is_admin)} -> {body.is_admin}")
        target.is_admin = int(body.is_admin)

    if changes:
        db.commit()
        db.refresh(target)
        action = "permission_grant" if any("True" in c for c in changes) else "permission_revoke"
        _audit(db, admin.username, action, username, "; ".join(changes), _client_ip(request))

    return {
        "username": target.username,
        "role": compute_role(target),
        "is_flag_admin": bool(target.is_flag_admin),
        "is_deploy_admin": bool(target.is_deploy_admin),
        "is_admin": bool(target.is_admin),
    }


@app.get("/admin/audit")
async def audit_log(
    page: int = 1,
    per_page: int = 50,  # capped at 200
    username: str | None = None,
    action: str | None = None,
    since: str | None = None,
    until: str | None = None,
    _: User = Depends(require_role("admin")),
    db: DBSession = Depends(get_db),
):
    query = db.query(AuditLog)
    if username:
        query = query.filter(AuditLog.username == username)
    if action:
        query = query.filter(AuditLog.action == action)
    if since:
        query = query.filter(AuditLog.timestamp >= since)
    if until:
        query = query.filter(AuditLog.timestamp <= until)

    per_page = min(per_page, 200)
    total = query.count()
    entries = (
        query.order_by(AuditLog.timestamp.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return {
        "entries": [
            {
                "id": e.id,
                "timestamp": e.timestamp,
                "username": e.username,
                "action": e.action,
                "target": e.target,
                "detail": e.detail,
                "ip": e.ip,
            }
            for e in entries
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3000)
