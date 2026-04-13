# Crown Engineering MCP

Engineering self-service tools for Crown Gas & Power, exposed via HTTP APIs and an MCP server.

## Purpose

This repository hosts a growing set of small services that engineers (and Claude, on their behalf) can call to do common operational tasks — code review, feature flag management, future things — without each engineer needing direct credentials or admin access to the underlying systems.

Each tool is a self-contained sub-directory with its own service code, scripts, and documentation. Tools can be reached via:

- **HTTP API** — for Jenkins, scripts, ad-hoc curl, and any non-Claude integrations.
- **MCP server** — single endpoint registered in engineers' `~/.claude/settings.json`, exposing every tool's relevant operations as MCP tools to Claude Code.

The HTTP API is the source of truth; the MCP server is a thin adapter on top.

## Current contents

| Directory | Tool | Status |
| --- | --- | --- |
| [`review/`](review/) | AI Code Review — FastAPI for engineer pre-push reviews + Jenkins polling pipeline that posts review comments on PRs | Live (port 9506) |

## Planned contents

| Tool | Purpose | Status |
| --- | --- | --- |
| `unleash/` | Wraps the Unleash admin API so engineers can create feature flags from Claude Code without holding the admin token. See [Unleash MCP Server design notes](https://crowngasandpower-team-delivery.atlassian.net/wiki/spaces/~71202085b1657528bb4defa1a115cc367829fb/pages/110788617/Unleash+MCP+Server+Design+Notes) | Planned |
| `mcp-server/` | Single MCP server exposing tools backed by every other directory's HTTP API | Planned (added when the first MCP-only consumer lands) |

## Repo layout

```
crown-engineering-mcp/
├── docker-compose.yml      # postgres + postgrest + each tool's HTTP service
├── init.sql                # shared DB schema (one schema per tool as it grows)
├── review/                 # AI Code Review tool
│   ├── api/                # FastAPI service
│   ├── scripts/            # poll-and-review, review-pr, backfill, etc.
│   ├── Jenkinsfile         # Jenkins polling pipeline
│   └── README.md
└── README.md               # you are here
```

When adding a new tool:

1. Create a top-level directory named after the tool (e.g. `unleash/`).
2. Put its HTTP service under `<tool>/api/`, scripts under `<tool>/scripts/`, and any pipeline definition (`Jenkinsfile`, GitHub Actions yaml) at the tool root.
3. Add the service to `docker-compose.yml`.
4. Document it in this README's *Current contents* table.
5. If the tool needs a database, add its schema to `init.sql` (or split that file as it grows).
6. Once the MCP server exists, register a tool category under `mcp-server/tools/<name>.py` mapping MCP tool calls to the new HTTP API.

## Infrastructure

Everything runs on `poc-containers` (192.168.173.140). Monitoring (Prometheus, Grafana, Loki, etc.) lives in a separate [`monitoring`](https://github.com/crowngasandpower/monitoring) repo — Grafana reaches into this stack's PostgREST (port 9505) for the Code Review dashboard.

| Service | Port | Container |
| --- | --- | --- |
| PostgreSQL | 9504 | `crown-mcp-postgres` |
| PostgREST | 9505 | `crown-mcp-postgrest` |
| Review API | 9506 | `crown-mcp-review-api` |

## Running locally

```bash
cp .env.example .env   # populate ANTHROPIC_API_KEY
docker compose up -d
```

## History

This repo evolved out of [`crowngasandpower/ai-code-review`](https://github.com/crowngasandpower/ai-code-review), which was originally a single-purpose AI code review system. As the monitoring stack grew up alongside it, the two concerns were split: monitoring moved to its own repo, and the review service moved here as the first inhabitant of a broader engineering-tools surface.
