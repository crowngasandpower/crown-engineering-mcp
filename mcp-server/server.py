"""Crown Engineering MCP Server.

Exposes engineering tools to Claude Code via MCP. Each tool is a thin wrapper
around an HTTP service in this same compose stack — the HTTP service is the
source of truth, MCP is one consumer among several (Jenkins, scripts, curl
also call the HTTP APIs directly).

Current tools:
  - review_diff: AI code review (wraps review-api at port 3000 internally,
    9506 externally)

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


if __name__ == "__main__":
    # SSE transport — broadly supported by Claude Code and other MCP clients.
    # mcp.sse_app() returns a Starlette app; uvicorn serves it on the chosen port.
    uvicorn.run(mcp.sse_app(), host="0.0.0.0", port=PORT)
