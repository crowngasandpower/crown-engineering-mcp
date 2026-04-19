"""Crown Engineering MCP Server.

Exposes engineering tools to Claude Code via MCP. Each tool is a thin wrapper
around an HTTP service in this same compose stack — the HTTP service is the
source of truth, MCP is one consumer among several (Jenkins, scripts, curl
also call the HTTP APIs directly).

Current tools:
  - review_diff: AI code review of a unified diff (wraps review-api
    POST /review at port 3000 internally, 9506 externally).
  - list_reviews: query past reviews — find PRs with outstanding
    high/medium/low issues, filter by engineer or repo (wraps
    review-api GET /reviews).
  - get_api_tokens: retrieve Unleash API tokens (client for backends,
    frontend for SPAs) scoped by environment (wraps unleash-api
    GET /tokens).
  - list_feature_flags: list all flags in the project with per-env
    state (wraps unleash-api GET /flags).
  - create_feature_flag: create an Unleash feature flag for a ticket
    (wraps unleash-api POST /flags).
  - get_feature_flag: show a flag's per-environment state (wraps
    unleash-api GET /flags/{name}).
  - toggle_feature_flag: enable/disable a flag in one environment
    (wraps unleash-api POST /flags/{name}/toggle).
  - archive_feature_flag: archive (soft-delete) a flag whose rollout
    is complete (wraps unleash-api DELETE /flags/{name}).
  - claim_bug: find and claim the next actionable bug from the CT
    Jira board (wraps bugs-api POST /claim at port 9513).
  - remember: store a piece of knowledge in shared team memory
    (wraps memory-api POST /remember at port 9514).
  - recall: retrieve semantically similar entries from team memory
    (wraps memory-api POST /recall).

Engineers register this server in ~/.claude/settings.json:
  {
    "mcpServers": {
      "crown-engineering": {
        "url": "http://poc-containers:9510/sse"
      }
    }
  }
"""

import os

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount

REVIEW_API_URL = os.environ.get("REVIEW_API_URL", "http://review-api:3000")
UNLEASH_API_URL = os.environ.get("UNLEASH_API_URL", "http://unleash-api:3000")
BUGS_API_URL = os.environ.get("BUGS_API_URL", "http://bugs-api:3000")
MEMORY_API_URL = os.environ.get("MEMORY_API_URL", "http://memory-api:3000")
PORT = int(os.environ.get("PORT", "3000"))
REVIEW_TIMEOUT_SECONDS = float(os.environ.get("REVIEW_TIMEOUT_SECONDS", "180"))

# host="0.0.0.0" disables FastMCP's Host-header validation — required because
# engineers connect via `http://poc-containers:9510/sse`, not localhost. Without
# this, the SSE handler raises "Invalid Host header" / "Request validation
# failed" for every external request.
mcp = FastMCP("crown-engineering", host="0.0.0.0")


@mcp.tool()
async def review_diff(diff: str) -> dict:
    """Run AI code review against a unified git diff.

    Use this BEFORE pushing changes to any Crown repository — it catches
    HIGH/MEDIUM severity issues per the Crown Engineering Commandments
    (security vulnerabilities, env() in app/ code, hardcoded paths,
    missing error handling on external calls, etc.) that should be
    fixed before push.

    Typical workflow:
      1. Stage and review your local changes.
      2. Capture the diff: `git diff origin/main...HEAD`
      3. Pass that diff to this tool.
      4. If the response has high > 0 or medium > 0, address those before pushing.

    Args:
        diff: Unified diff text. Output of `git diff origin/main...HEAD` or
              equivalent. Must be non-empty.

    Returns:
        Structured review result:
          - summary: One-paragraph prose review.
          - verdict: Always "comment" (the review API runs in advisory mode).
          - issues: List of {path, line, severity, message}.
          - high, medium, low, total: Severity counts.
          - error: Set only if the review couldn't be completed.
    """
    async with httpx.AsyncClient(timeout=REVIEW_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"{REVIEW_API_URL}/review",
            json={"diff": diff},
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def list_reviews(
    engineer: str | None = None,
    repo: str | None = None,
    min_high: int = 0,
    min_medium: int = 0,
    min_low: int = 0,
    since: str | None = None,
    limit: int = 50,
) -> dict:
    """Query past AI code reviews. Use this to answer questions like:

      "Show me my outstanding high-severity issues."
        → list_reviews(engineer="<their name>", min_high=1)

      "Which eps PRs still have medium issues?"
        → list_reviews(repo="eps", min_medium=1)

      "Top 10 PRs across the org with the most issues right now."
        → list_reviews(limit=10)

    Returns the latest review per PR (one row per repo+PR), sorted
    most-problematic first (high desc, medium desc, low desc, total desc).
    "Outstanding" semantics fall out for free: if a fix removed the issue,
    the row's counts would have been updated to zero on the next review.

    Args:
        engineer: Filter to one engineer. The bot stores the GitHub login of
                  the PR author. If unsure of the exact value, omit and
                  inspect the results to find the correct casing.
        repo: Filter to one repo (e.g. "eps", "gps").
        min_high: Only PRs with at least this many high-severity issues.
        min_medium: Same for medium-severity.
        min_low: Same for low-severity.
        since: Only PRs created on/after this ISO date (e.g. "2026-03-01").
        limit: Cap on rows returned (1–500, default 50).

    Returns:
        {
          "total_matched": <int — total rows matching filters, may exceed limit>,
          "reviews": [
            {
              "repo": "...", "pr": 123, "pr_url": "https://github.com/...",
              "engineer": "...", "pr_title": "...", "pr_created_at": "...",
              "high": N, "medium": N, "low": N, "total": N,
              "lines_changed": N, "reviewed_at": "..."
            }, ...
          ]
        }
    """
    params: dict[str, str | int] = {
        "min_high": min_high,
        "min_medium": min_medium,
        "min_low": min_low,
        "limit": limit,
    }
    if engineer:
        params["engineer"] = engineer
    if repo:
        params["repo"] = repo
    if since:
        params["since"] = since

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{REVIEW_API_URL}/reviews", params=params)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def get_api_tokens(type: str | None = None) -> dict:
    """Retrieve Unleash API tokens and connection URLs for app configuration.

    Crown uses two types of environment-scoped token — choose the right
    one for the context:

    **client** tokens (server-side / backend):
      - Used by Laravel apps via `j-webb/laravel-unleash` or `crown/unleash`
      - Connect to the **client_api_url** returned in the response
      - Safe on servers, NEVER safe in browser code
      - Each token is scoped to one environment (development, UAT, production)

    **frontend** tokens (browser / Vue SPA):
      - Used by JavaScript browser SDKs (`@unleash/proxy-client-react`,
        `unleash-proxy-client`)
      - Connect to the **frontend_api_url** returned in the response
      - Designed to be safe in browser code — only returns evaluated
        true/false results, never strategies or internal details
      - Each token is scoped to one environment

    When configuring an app, pick the token that matches:
      1. The app's runtime: backend → client, frontend → frontend
      2. The target environment: development, UAT, or production

    Use the returned URLs when configuring the Unleash SDK. For example,
    in a Laravel app's .env: UNLEASH_URL=<client_api_url> and
    UNLEASH_API_KEY=<token secret>. Never hardcode tokens in committed
    files — put them in .env.

    Admin tokens are never returned — they stay inside the wrapper service.

    Args:
        type: Optional filter — "client" or "frontend". Omit to get both.

    Returns:
        {
            "unleash_url": "https://...",
            "client_api_url": "https://.../api/client",
            "frontend_api_url": "https://.../api/frontend",
            "tokens": [{"name", "type", "environment", "project", "secret"}]
        }
    """
    params = {}
    if type:
        params["type"] = type
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{UNLEASH_API_URL}/tokens", params=params)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def list_feature_flags() -> dict:
    """List all feature flags in the project with per-environment state.

    Use this to answer questions like "what flags do we have?" or "show me
    all feature flags" or to get a full overview of flag states across
    environments.

    Returns:
        List of flags, each with the same shape as get_feature_flag —
        includes name, type, description, and per-environment enabled state.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{UNLEASH_API_URL}/flags")
        resp.raise_for_status()
        return {"flags": resp.json()}


@mcp.tool()
async def create_feature_flag(
    ticket_key: str,
    short_name: str,
    description: str = "",
    type: str = "release",
) -> dict:
    """Create an Unleash feature flag for a Jira ticket.

    Use this when starting work on a ticket whose changes should be gated
    behind a flag. Good candidates: behavioural changes to existing code
    paths, performance-sensitive changes, anything that benefits from quick
    rollback in production. Skip for: pure refactors with no behaviour
    change, doc-only changes, test-only changes, trivial bug fixes.

    At the start of work on a non-trivial ticket, it's reasonable to ask
    the engineer: "This work should probably be behind a feature flag — want
    me to create one?". If they say yes, call this with the ticket key and
    a short, meaningful feature name.

    The flag is created OFF in every environment (development, UAT,
    production). Engineers write the new behaviour inside the flag
    condition and keep the existing behaviour in the else branch.

    Idempotent: if a flag with the same name already exists, returns the
    existing flag with `existed=true`.

    Args:
        ticket_key: Jira ticket key, e.g. "CT-2037".
        short_name: Short feature name, alphanumeric + hyphens. Starts with
                    alphanumeric. Example: "Deadlock-Retry".
        description: Human description of what the flag gates.
        type: Unleash flag type. Default "release"; others: "experiment",
              "operational", "kill-switch", "permission".

    Returns:
        {
            "name": "CT-2037-Deadlock-Retry",
            "project": "core-products",
            "type": "release",
            "description": "...",
            "environments": [
                {"name": "development", "enabled": false},
                {"name": "UAT", "enabled": false},
                {"name": "production", "enabled": false}
            ],
            "existed": false
        }
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{UNLEASH_API_URL}/flags",
            json={
                "ticket_key": ticket_key,
                "short_name": short_name,
                "description": description,
                "type": type,
            },
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def get_feature_flag(name: str) -> dict:
    """Retrieve a feature flag's full state across all environments.

    Use this to answer questions like "is CT-2037-Deadlock-Retry enabled
    in production?" or "show me the current state of my flag".

    Args:
        name: Flag name, e.g. "CT-2037-Deadlock-Retry".

    Returns:
        Same shape as create_feature_flag — includes the per-environment
        `enabled` flag under `.environments`.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{UNLEASH_API_URL}/flags/{name}")
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def toggle_feature_flag(name: str, environment: str, enabled: bool) -> dict:
    """Enable or disable a feature flag in one environment.

    Be careful with `environment="production"` — that's a live change. For
    non-trivial rollouts, flip development first, verify, then UAT, then
    production.

    Args:
        name: Flag name, e.g. "CT-2037-Deadlock-Retry".
        environment: "development", "UAT", or "production" (case-insensitive;
                     "dev" and "prod" also accepted).
        enabled: True to enable, False to disable.

    Returns:
        {"name": "<canonical env>", "enabled": <bool>}
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{UNLEASH_API_URL}/flags/{name}/toggle",
            json={"environment": environment, "enabled": enabled},
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def archive_feature_flag(name: str) -> dict:
    """Archive a feature flag.

    Use this when the flag's rollout is complete and the surrounding code
    (and the else branch) has been cleaned up — i.e. the flag is no longer
    serving any purpose. Soft-delete: the flag moves to Unleash's archived
    list and stops evaluating, but can be revived from the Unleash UI if
    you change your mind.

    Don't archive a flag whose code branches are still in the codebase —
    the next deploy would hit the unflagged path with no gate.

    Args:
        name: Flag name, e.g. "CT-2037-Deadlock-Retry".

    Returns:
        {"name": "<name>", "archived": true} on success.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.delete(f"{UNLEASH_API_URL}/flags/{name}")
        resp.raise_for_status()
    return {"name": name, "archived": True}


@mcp.tool()
async def claim_bug(assignee_email: str) -> dict:
    """Find and claim the next actionable bug from the CT Jira board.

    Use this when an engineer says "give me a bug", "let's tackle a bug",
    or otherwise asks for the next bug to work on. The tool searches the
    CT board for open bugs in the To Do column, sorted by priority
    (highest first), and:

      1. If the bug has a sufficiently detailed description → assigns it
         to the engineer, moves it to In Progress, and returns the ticket
         key, title, priority, and description so work can begin.

      2. If the bug lacks enough information → adds a comment explaining
         what's missing and moves it to Blocked, then tries the next bug.

    The response includes `blocked_keys` — any tickets that were skipped
    and blocked along the way.

    Args:
        assignee_email: The engineer's email address (used to look up
                        their Jira account and assign the ticket). This
                        is the email they use to log in to Jira — e.g.
                        "first.last@crowngasandpower.co.uk".

    Returns:
        {
          "key": "CT-1234",
          "url": "https://...atlassian.net/browse/CT-1234",
          "title": "Bug summary from Jira",
          "priority": "High",
          "description_text": "Plain-text description...",
          "viable": true,
          "message": "Bug CT-1234 assigned to you and moved to In Progress.",
          "blocked_keys": ["CT-1230", "CT-1231"]
        }

        If no viable bug is found, returns an error with the list of
        tickets that were blocked.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{BUGS_API_URL}/claim",
            json={"assignee_email": assignee_email},
        )
        if resp.status_code == 404:
            # Business-logic 404 — no viable bugs found. Return the
            # detail from the bugs-api so the caller sees what happened
            # (e.g. how many were checked, which were blocked).
            detail = resp.json().get("detail", "No viable bugs found")
            return {"viable": False, "message": detail, "blocked_keys": []}
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def skip_bug(key: str, reason: str) -> dict:
    """Mark a bug as investigated but not actionable, so claim_bug skips it.

    Use this when a bug has been looked at but doesn't need a code fix —
    e.g. infrastructure issues, data queries, documentation tasks, or
    tickets that were miscategorised as bugs. Adds a 'claude-skipped'
    label and a comment with the reason, then unassigns the ticket.

    Args:
        key: The Jira issue key (e.g. "CT-689").
        reason: Why this bug is being skipped (e.g. "infra issue — DNS
                resolves fine now, no code fix needed").

    Returns:
        {"key": "CT-689", "message": "CT-689 marked as skipped..."}
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{BUGS_API_URL}/skip",
            json={"key": key, "reason": reason},
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def remember(
    content: str,
    tags: list[str] | None = None,
    source: str | None = None,
    author: str | None = None,
) -> dict:
    """Store a piece of knowledge in the shared team memory.

    Use this to persist non-obvious information that future sessions (for
    you or other engineers) will benefit from. Good candidates:
      - architectural decisions and the reasoning behind them
      - gotchas discovered while debugging (e.g. schema quirks, race
        conditions, cross-app coupling surprises)
      - post-incident findings and their fixes
      - onboarding context that isn't captured elsewhere

    Bad candidates:
      - secrets, credentials, or customer PII (memory is team-wide)
      - ephemeral task state (use a plan or the conversation)
      - information already documented in code or a CLAUDE.md

    Args:
        content: The knowledge to store. Self-contained — a later reader
                 shouldn't need the conversation it came from.
        tags: Optional topical tags, e.g. ["eps", "datasheet", "gotcha"].
              Used to filter recall() results.
        source: Optional pointer to where this came from (Jira key, PR
                URL, Confluence page, etc.). Helps verify currency.
        author: Optional — who added it. Defaults to unattributed.

    Returns:
        {"id": "<uuid>", "stored_at": "<iso8601>"}
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{MEMORY_API_URL}/remember",
            json={
                "content": content,
                "tags": tags or [],
                "source": source,
                "author": author,
            },
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def recall(
    query: str,
    top_k: int = 5,
    tags: list[str] | None = None,
) -> dict:
    """Retrieve semantically similar entries from team memory.

    Use this when you're about to work on something and suspect someone
    (you or another engineer) has already hit related ground. Example
    prompts that should trigger recall:

      - "Has anyone dealt with the datasheet CoT path before?"
      - "What did we decide about Horizon vs supervisor for queues?"
      - "Why is EPS single-threaded on the trading queue?"

    Results are scored by cosine similarity of Voyage embeddings — higher
    score = closer match. Treat the top hit as a lead, not a verdict:
    verify against current code before acting on it.

    Args:
        query: Natural-language question or topic.
        top_k: How many hits to return (1–50, default 5).
        tags: Optional — restrict to entries tagged with any of these.

    Returns:
        {"hits": [
          {"id", "score", "content", "tags", "source", "author", "stored_at"},
          ...
        ]}
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{MEMORY_API_URL}/recall",
            json={"query": query, "top_k": top_k, "tags": tags},
        )
        resp.raise_for_status()
        return resp.json()


if __name__ == "__main__":
    # Serve both transports so existing /sse clients keep working while
    # new clients can use the more reliable /mcp streamable HTTP endpoint.
    # The SSE app must be mounted as a complete sub-application (not merged
    # routes) so its internal session manager stays intact.
    app = Starlette(
        routes=[
            Mount("/", app=mcp.sse_app()),
            Mount("/mcp", app=mcp.streamable_http_app()),
        ],
    )
    uvicorn.run(app, host="0.0.0.0", port=PORT)
