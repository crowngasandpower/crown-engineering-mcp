# Crown Engineering MCP — Claude guide

## What this repo is

A growing set of small services that provide engineering self-service. Each top-level directory (e.g. `review/`, future `unleash/`) is one tool. Tools expose an HTTP API; an MCP server (planned at `mcp-server/`) sits in front and translates MCP tool calls into HTTP calls.

When asked to add a new engineering tool, default to creating a new top-level directory in this repo rather than a new repo, unless the tool genuinely needs a different stack or release cadence.

## Engineering standards

Follow the central standards in `../engineering-standards/CLAUDE.md` — applies to all Crown repos.

## How to add a new tool

1. New top-level directory `<tool>/`.
2. HTTP service under `<tool>/api/` (FastAPI by default, matching `review/api/`).
3. Any pipeline (`Jenkinsfile` etc.) at `<tool>/` root.
4. Add the service to the top-level `docker-compose.yml`.
5. Add a `<tool>/README.md` describing what the tool does and its HTTP contract.
6. Update the *Current contents* table in the top-level `README.md`.
7. If the MCP server exists, add a tool category file under `mcp-server/tools/<name>.py` that wraps the HTTP API.

Don't pre-create empty directories for hypothetical tools. The README documents the intent; new directories arrive with their first real content.

## Cross-cutting things to remember

- **Secrets** never go in committed files. Each tool reads its credentials from environment variables; production values come from the `.env` file on `poc-containers` (uncommitted).
- **Source of truth is the HTTP API.** The MCP server is a thin adapter — never put business logic in MCP tool handlers that's not also reachable via HTTP.
- **Naming convention for containers:** `crown-mcp-<tool>-<role>` (e.g. `crown-mcp-review-api`).
- **Port allocation:** review-api 9506, future tools claim sequentially upward from 9510 (9504/9505 used by postgres/postgrest, 9506 review-api, 9508/9509 reserved for monitoring stack postgres/postgrest).

## Review system prompt sync

The Review tool's system prompt currently lives in three places that must stay in sync:

1. `review/scripts/review-pr.sh` — used by the Jenkins bot
2. `review/scripts/review-local.sh` — used by engineers locally
3. `review/api/app.py` — used by the HTTP review API

If review criteria change, update all three. (Long-term: extract to a single source. See `review/README.md`.)

## Pre-push review

Before pushing any change to this repo, run the review API:

```bash
DIFF=$(git diff origin/main...HEAD)
curl -s -X POST http://poc-containers:9506/review \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg diff "$DIFF" '{diff: $diff}')" | jq .
```

Fix HIGH/MEDIUM issues before pushing.
