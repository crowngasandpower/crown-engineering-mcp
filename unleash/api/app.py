"""Unleash Feature Flag API.

Wraps the Unleash admin API so engineers can create and manage flags
without holding the admin token on their machines. The admin token lives
in this service's env; engineers call the HTTP endpoints (or go through
the MCP server for Claude-driven workflows).

Endpoints:
  POST /flags                  — create a flag for a Jira ticket
  GET  /flags/{name}           — full flag details, including per-env state
  POST /flags/{name}/toggle    — enable/disable in one environment

Flag naming convention: `CT-<ticket>-<ShortName>`, enforced server-side.
Flags are created off in every environment. Engineers code the else
branch as the safe/existing behaviour (see Unleash onboarding notes).
"""

import os
import re

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Unleash Flag API", version="1.0.0")

UNLEASH_BASE_URL = os.environ["UNLEASH_BASE_URL"].rstrip("/")
UNLEASH_PROJECT = os.environ.get("UNLEASH_PROJECT", "core-products")
UNLEASH_ADMIN_TOKEN = os.environ["UNLEASH_ADMIN_TOKEN"]

# Accept case-insensitive environment aliases; canonicalise to the names
# Unleash stores. Unleash environment names are case-sensitive on the wire,
# so a user typing "uat" needs to be mapped to "UAT".
ENV_CANONICAL = {
    "dev": "development",
    "development": "development",
    "uat": "UAT",
    "prod": "production",
    "production": "production",
}

TICKET_KEY_RE = re.compile(r"^CT-\d+$")
SHORT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
# Guards the `{name}` path parameter on GET/toggle. Matches the canonical
# shape produced by create_flag (CT-<digits>-<short>) so callers cannot
# slip `../` or other URL-unsafe characters into the upstream Unleash URL.
FLAG_NAME_RE = re.compile(r"^CT-\d+-[A-Za-z0-9][A-Za-z0-9-]*$")
VALID_FLAG_TYPES = {"release", "experiment", "operational", "kill-switch", "permission"}


def validate_flag_name(name: str) -> None:
    if not FLAG_NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail="flag name must match CT-<digits>-<short-name> with only alphanumerics and hyphens",
        )


def canonical_env(env: str) -> str:
    canonical = ENV_CANONICAL.get(env.lower())
    if not canonical:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown environment '{env}'. "
                "Valid: development, UAT, production (case-insensitive; "
                "'dev' and 'prod' also accepted)"
            ),
        )
    return canonical


class CreateFlagRequest(BaseModel):
    ticket_key: str = Field(..., description="Jira ticket key, e.g. CT-2037")
    short_name: str = Field(..., description="Feature short-name, e.g. Deadlock-Retry")
    description: str = Field("", description="Human description of the flag")
    type: str = Field("release", description="Unleash flag type")


class EnvironmentState(BaseModel):
    name: str
    enabled: bool


class FeatureFlag(BaseModel):
    name: str
    project: str
    type: str
    description: str
    created_at: str | None = None
    environments: list[EnvironmentState]
    existed: bool = False  # True when create was idempotent (flag already existed)


class ToggleRequest(BaseModel):
    environment: str
    enabled: bool


@app.get("/health")
async def health():
    return {"status": "ok", "project": UNLEASH_PROJECT}


@app.post("/flags", response_model=FeatureFlag)
async def create_flag(req: CreateFlagRequest):
    """Create a flag with name `{ticket_key}-{short_name}`.

    Validates inputs, then attempts to create. On 409 (name already taken),
    returns the existing flag with `existed=true` — idempotent so Claude can
    safely retry.
    """
    if not TICKET_KEY_RE.match(req.ticket_key):
        raise HTTPException(
            status_code=422,
            detail="ticket_key must match CT-<digits>, e.g. CT-2037",
        )
    if not SHORT_NAME_RE.match(req.short_name):
        raise HTTPException(
            status_code=422,
            detail="short_name must start with alphanumeric and contain only alphanumerics and hyphens",
        )
    if req.type not in VALID_FLAG_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"type must be one of: {sorted(VALID_FLAG_TYPES)}",
        )

    name = f"{req.ticket_key}-{req.short_name}"
    headers = {"Authorization": UNLEASH_ADMIN_TOKEN}

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{UNLEASH_BASE_URL}/api/admin/projects/{UNLEASH_PROJECT}/features",
            headers=headers,
            json={"name": name, "type": req.type, "description": req.description},
        )

        if resp.status_code == 409:
            existing = await _fetch_flag(client, name)
            existing.existed = True
            return existing

        if not 200 <= resp.status_code < 300:
            raise HTTPException(
                status_code=502,
                detail=f"Unleash create error (HTTP {resp.status_code}): {resp.text[:500]}",
            )

        return await _fetch_flag(client, name)


@app.get("/flags/{name}", response_model=FeatureFlag)
async def get_flag(name: str):
    validate_flag_name(name)
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await _fetch_flag(client, name)


@app.delete("/flags/{name}", status_code=204)
async def archive_flag(name: str):
    """Archive a feature flag.

    Unleash treats archive as soft-delete: the flag moves to the archived
    list and stops evaluating, but can be revived from the Unleash UI if
    needed. Use this when the flag's rollout is complete and the
    surrounding code (and the else branch) has been cleaned up.
    """
    validate_flag_name(name)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.delete(
            f"{UNLEASH_BASE_URL}/api/admin/projects/{UNLEASH_PROJECT}/features/{name}",
            headers={"Authorization": UNLEASH_ADMIN_TOKEN},
        )
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Flag '{name}' not found")
        if not 200 <= resp.status_code < 300:
            raise HTTPException(
                status_code=502,
                detail=f"Unleash archive error (HTTP {resp.status_code}): {resp.text[:500]}",
            )
    return None


@app.post("/flags/{name}/toggle", response_model=EnvironmentState)
async def toggle_flag(name: str, req: ToggleRequest):
    """Enable or disable a flag in one environment."""
    validate_flag_name(name)
    env = canonical_env(req.environment)
    action = "on" if req.enabled else "off"

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{UNLEASH_BASE_URL}/api/admin/projects/{UNLEASH_PROJECT}"
            f"/features/{name}/environments/{env}/{action}",
            headers={"Authorization": UNLEASH_ADMIN_TOKEN},
        )
        if resp.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail=f"Flag '{name}' or environment '{env}' not found",
            )
        if not 200 <= resp.status_code < 300:
            raise HTTPException(
                status_code=502,
                detail=f"Unleash toggle error (HTTP {resp.status_code}): {resp.text[:500]}",
            )

    return EnvironmentState(name=env, enabled=req.enabled)


async def _fetch_flag(client: httpx.AsyncClient, name: str) -> FeatureFlag:
    resp = await client.get(
        f"{UNLEASH_BASE_URL}/api/admin/projects/{UNLEASH_PROJECT}/features/{name}",
        headers={"Authorization": UNLEASH_ADMIN_TOKEN},
    )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Flag '{name}' not found")
    if not 200 <= resp.status_code < 300:
        raise HTTPException(
            status_code=502,
            detail=f"Unleash fetch error (HTTP {resp.status_code}): {resp.text[:500]}",
        )

    data = resp.json()
    return FeatureFlag(
        name=data["name"],
        project=data.get("project", UNLEASH_PROJECT),
        type=data.get("type", "release"),
        description=data.get("description", "") or "",
        created_at=data.get("createdAt"),
        environments=[
            EnvironmentState(name=e["name"], enabled=e.get("enabled", False))
            for e in data.get("environments", [])
        ],
    )
