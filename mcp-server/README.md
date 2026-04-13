# MCP Server

The Crown Engineering MCP server. Exposes the tools in this repo (currently just the AI code review) to Claude Code via the [Model Context Protocol](https://modelcontextprotocol.io/).

## How it fits

Each tool in this repo is its own HTTP service (see `../review/`). The MCP server is a thin wrapper that translates MCP tool calls into HTTP calls against those services. It is one consumer among several — Jenkins, scripts, and `curl` all hit the same HTTP APIs directly, without going through MCP.

```
Claude Code  ──MCP──▶  mcp-server  ──HTTP──▶  review-api
                                  ──HTTP──▶  (future tools)
```

## Tools

| Tool | Backed by | Description |
| --- | --- | --- |
| `review_diff(diff)` | `review-api` (`POST /review`) | Run AI code review against a unified git diff. Returns summary, verdict, issues, severity counts. |

## Engineer setup

Add to `~/.claude/settings.json` (or use `claude mcp add`):

```json
{
  "mcpServers": {
    "crown-engineering": {
      "url": "http://poc-containers:9510/sse"
    }
  }
}
```

Restart Claude Code. The `review_diff` tool should appear in the tool list. From then on, just say "review my changes before I push" or similar — Claude finds the tool from its description and calls it with the local diff.

## Adding a new tool

1. Add a new HTTP service under a new top-level directory in this repo (e.g. `unleash/api/`).
2. Update the top-level `docker-compose.yml` to bring up the new service.
3. Add a new `@mcp.tool()` function in `server.py` that calls the new HTTP service. Keep the docstring rich — Claude reads it to decide when to invoke the tool.
4. If the tool list grows past ~5, split each category into its own module under `tools/` and register them in `server.py`.

## Local development

```bash
# Run the review-api stack first so this server has something to call
docker compose up -d postgres postgrest review-api
# Then run the MCP server locally (port 9510)
docker compose up -d mcp-server
```

Or all at once: `docker compose up -d`.

## Why MCP instead of just exposing the HTTP API directly?

The HTTP API IS exposed directly — Jenkins and `review-local.sh` use it. MCP adds:

- **Discoverability:** Claude reads `review_diff`'s docstring and knows when to use it. No need to write each engineer a CLAUDE.md instruction explaining the API URL and request shape.
- **Structured input/output:** MCP tools have typed schemas. Claude can call them confidently without parsing free-form HTTP responses.
- **One endpoint, many tools:** As the catalogue grows, engineers configure one MCP URL and get every tool. Without MCP, each tool would need its own discovery and invocation pattern.
