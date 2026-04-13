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

REVIEW_API_URL = os.environ.get("REVIEW_API_URL", "http://review-api:3000")
PORT = int(os.environ.get("PORT", "3000"))
REVIEW_TIMEOUT_SECONDS = float(os.environ.get("REVIEW_TIMEOUT_SECONDS", "180"))

mcp = FastMCP("crown-engineering")


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


if __name__ == "__main__":
    # SSE transport — broadly supported by Claude Code and other MCP clients.
    # mcp.sse_app() returns a Starlette app; uvicorn serves it on the chosen port.
    uvicorn.run(mcp.sse_app(), host="0.0.0.0", port=PORT)
