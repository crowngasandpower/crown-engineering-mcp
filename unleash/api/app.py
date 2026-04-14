"""Unleash Feature Flag API.

Wraps the Unleash admin API so engineers can create and manage flags
without holding the admin token on their machines. The admin token lives
in this service's env; engineers call the HTTP endpoints (or go through
the MCP server for Claude-driven workflows).

Endpoints:
  GET  /tokens                 — list API tokens (client + frontend)
  GET  /flags                  — list all flags in the project
  POST /flags                  — create a flag for a Jira ticket
  GET  /flags/{name}           — full flag details, including per-env state
  POST /flags/{name}/toggle    — enable/disable in one environment
  DELETE /flags/{name}         — archive (soft-delete) a flag
  POST /flags/{name}/cleanup-ticket — create Jira ticket to remove flag code

Flag naming convention: `CT-<ticket>-<ShortName>`, enforced server-side.
Flags are created off in every environment. Engineers code the else
branch as the safe/existing behaviour (see Unleash onboarding notes).
"""

import base64
import os
import re
from datetime import date, timedelta

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="Unleash Flag API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UNLEASH_BASE_URL = os.environ["UNLEASH_BASE_URL"].rstrip("/")
UNLEASH_PROJECT = os.environ.get("UNLEASH_PROJECT", "core-products")
UNLEASH_ADMIN_TOKEN = os.environ["UNLEASH_ADMIN_TOKEN"]

# Jira integration — optional. When set, the cleanup-ticket endpoint
# creates a ticket to remove feature flag code after production rollout.
JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "https://crowngasandpower-team-delivery.atlassian.net")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")

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


class ApiToken(BaseModel):
    name: str
    type: str  # "client", "frontend", or "admin"
    environment: str | None = None
    project: str | None = None
    secret: str


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


class CleanupTicket(BaseModel):
    key: str
    url: str
    summary: str
    due_date: str
    parent_epic: str | None = None


@app.get("/health")
async def health():
    return {"status": "ok", "project": UNLEASH_PROJECT}


@app.get("/tokens")
async def list_tokens(type: str | None = None):
    """List API tokens, optionally filtered by type (client, frontend, admin).

    Returns tokens for the current project, the Unleash API URL, and the
    correct API path for each token type. Admin tokens (which are not
    project-scoped) are excluded — they should never leave this service.
    """
    headers = {"Authorization": UNLEASH_ADMIN_TOKEN}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{UNLEASH_BASE_URL}/api/admin/api-tokens",
            headers=headers,
        )
        if not 200 <= resp.status_code < 300:
            raise HTTPException(
                status_code=502,
                detail=f"Unleash tokens error (HTTP {resp.status_code}): {resp.text[:500]}",
            )
        data = resp.json()
        tokens = []
        for t in data.get("tokens", []):
            # Skip admin tokens — they must never leave this service
            if t.get("type") == "admin":
                continue
            # Only include tokens scoped to our project (or wildcard)
            token_project = t.get("project") or t.get("projects", ["*"])[0] if isinstance(t.get("projects"), list) else t.get("project", "*")
            if token_project not in (UNLEASH_PROJECT, "*"):
                continue
            if type and t.get("type") != type:
                continue
            tokens.append(ApiToken(
                name=t.get("tokenName", t.get("username", "unknown")),
                type=t["type"],
                environment=t.get("environment"),
                project=token_project,
                secret=t["secret"],
            ))
        return {
            "unleash_url": UNLEASH_BASE_URL,
            "client_api_url": f"{UNLEASH_BASE_URL}/api/client",
            "frontend_api_url": f"{UNLEASH_BASE_URL}/api/frontend",
            "tokens": tokens,
        }


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


@app.get("/flags", response_model=list[FeatureFlag])
async def list_flags():
    """List all feature flags in the project with per-environment state."""
    headers = {"Authorization": UNLEASH_ADMIN_TOKEN}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{UNLEASH_BASE_URL}/api/admin/projects/{UNLEASH_PROJECT}/features",
            headers=headers,
        )
        if not 200 <= resp.status_code < 300:
            raise HTTPException(
                status_code=502,
                detail=f"Unleash list error (HTTP {resp.status_code}): {resp.text[:500]}",
            )
        data = resp.json()
        return [
            FeatureFlag(
                name=f["name"],
                project=f.get("project", UNLEASH_PROJECT),
                type=f.get("type", "release"),
                description=f.get("description", "") or "",
                created_at=f.get("createdAt"),
                environments=[
                    EnvironmentState(name=e["name"], enabled=e.get("enabled", False))
                    for e in f.get("environments", [])
                ],
            )
            for f in data.get("features", [])
        ]


@app.get("/flags/apps")
async def get_flag_apps(flags: str = ""):
    """Batch-lookup parent epic for each flag's Jira ticket.

    Accepts a comma-separated list of flag names (or, if empty, fetches all
    flags from Unleash first). Extracts ticket keys (CT-NNNN) from each name,
    queries Jira in a single JQL call, and returns a mapping of flag name to
    app info derived from the parent epic.

    Returns: {flag_name: {ticket_key, epic_key, epic_summary}} for each flag.
    Flags whose ticket has no parent epic return null for epic fields.
    """
    if not JIRA_EMAIL or not JIRA_API_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Jira credentials not configured (JIRA_EMAIL / JIRA_API_TOKEN)",
        )

    # Parse flag names — either from query param or by fetching all flags
    if flags:
        flag_names = [f.strip() for f in flags.split(",") if f.strip()]
    else:
        all_flags = await list_flags()
        flag_names = [f.name for f in all_flags]

    # Extract ticket keys from flag names
    ticket_to_flags: dict[str, list[str]] = {}
    for name in flag_names:
        match = re.match(r"^(CT-\d+)", name)
        if match:
            key = match.group(1)
            ticket_to_flags.setdefault(key, []).append(name)

    if not ticket_to_flags:
        return {}

    auth_value = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    jira_headers = {
        "Authorization": f"Basic {auth_value}",
        "Content-Type": "application/json",
    }

    # Single JQL query for all ticket keys
    keys_csv = ", ".join(ticket_to_flags.keys())
    jql = f"key in ({keys_csv})"

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{JIRA_BASE_URL}/rest/api/3/search",
            params={"jql": jql, "maxResults": 100, "fields": "parent,summary"},
            headers=jira_headers,
        )
        if not 200 <= resp.status_code < 300:
            raise HTTPException(
                status_code=502,
                detail=f"Jira search error (HTTP {resp.status_code}): {resp.text[:500]}",
            )

        issues = resp.json().get("issues", [])
        ticket_info: dict[str, dict] = {}
        for issue in issues:
            key = issue["key"]
            parent = issue.get("fields", {}).get("parent")
            ticket_info[key] = {
                "epic_key": parent.get("key") if parent else None,
                "epic_summary": parent.get("fields", {}).get("summary") if parent else None,
            }

    # Map back to flag names
    result = {}
    for ticket_key, flag_list in ticket_to_flags.items():
        info = ticket_info.get(ticket_key, {})
        for flag_name in flag_list:
            result[flag_name] = {
                "ticket_key": ticket_key,
                "epic_key": info.get("epic_key"),
                "epic_summary": info.get("epic_summary"),
            }

    return result


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


@app.post("/flags/{name}/cleanup-ticket", response_model=CleanupTicket)
async def create_cleanup_ticket(name: str):
    """Create a Jira ticket to remove feature flag code.

    Extracts the original ticket key from the flag name (e.g. CT-1929 from
    CT-1929-Batch-Pricing), looks up its parent epic, and creates a cleanup
    task under the same epic with a due date 4 weeks from today.

    Idempotent: if a matching open cleanup ticket already exists, returns it.
    """
    validate_flag_name(name)

    if not JIRA_EMAIL or not JIRA_API_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Jira credentials not configured (JIRA_EMAIL / JIRA_API_TOKEN)",
        )

    # Extract ticket key: CT-1929-Batch-Pricing -> CT-1929
    match = re.match(r"^(CT-\d+)", name)
    if not match:
        raise HTTPException(status_code=422, detail="Cannot extract ticket key from flag name")
    original_key = match.group(1)

    auth_value = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    jira_headers = {
        "Authorization": f"Basic {auth_value}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. Look up the original ticket to find its parent epic
        resp = await client.get(
            f"{JIRA_BASE_URL}/rest/api/3/issue/{original_key}",
            params={"fields": "parent"},
            headers=jira_headers,
        )
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Jira ticket {original_key} not found")
        if not 200 <= resp.status_code < 300:
            raise HTTPException(
                status_code=502,
                detail=f"Jira fetch error (HTTP {resp.status_code}): {resp.text[:500]}",
            )

        original = resp.json()
        parent_epic = original.get("fields", {}).get("parent", {}).get("key")

        # 2. Check for existing open cleanup ticket (idempotent)
        summary = f"Remove feature flag {name}"
        jql = f'project = CT AND summary ~ "Remove feature flag {name}" AND statusCategory != Done'
        resp = await client.get(
            f"{JIRA_BASE_URL}/rest/api/3/search",
            params={"jql": jql, "maxResults": 1, "fields": "key,summary,duedate,parent"},
            headers=jira_headers,
        )
        if 200 <= resp.status_code < 300:
            results = resp.json()
            if results.get("total", 0) > 0:
                existing = results["issues"][0]
                return CleanupTicket(
                    key=existing["key"],
                    url=f"{JIRA_BASE_URL}/browse/{existing['key']}",
                    summary=existing["fields"]["summary"],
                    due_date=existing["fields"].get("duedate") or "",
                    parent_epic=parent_epic,
                )

        # 3. Create the cleanup ticket
        due = (date.today() + timedelta(weeks=4)).isoformat()
        issue_fields: dict = {
            "project": {"key": "CT"},
            "issuetype": {"name": "Task"},
            "summary": summary,
            "description": {
                "version": 1,
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": f"Feature flag {name} has been enabled in production. "
                                f"Remove the flag check and the else (old-behaviour) branch "
                                f"from the codebase, then archive the flag in Unleash.",
                            },
                        ],
                    },
                ],
            },
            "duedate": due,
        }

        if parent_epic:
            issue_fields["parent"] = {"key": parent_epic}

        resp = await client.post(
            f"{JIRA_BASE_URL}/rest/api/3/issue",
            headers=jira_headers,
            json={"fields": issue_fields},
        )
        if not 200 <= resp.status_code < 300:
            raise HTTPException(
                status_code=502,
                detail=f"Jira create error (HTTP {resp.status_code}): {resp.text[:500]}",
            )

        created = resp.json()
        ticket_key = created["key"]
        return CleanupTicket(
            key=ticket_key,
            url=f"{JIRA_BASE_URL}/browse/{ticket_key}",
            summary=summary,
            due_date=due,
            parent_epic=parent_epic,
        )


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
