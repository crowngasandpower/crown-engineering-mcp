# Memory — shared team knowledge store

A small HTTP API over Qdrant + Voyage AI embeddings that gives Claude Code
(and any other client) a persistent, queryable team memory.

Engineers rarely call this API directly. The usual entry points are the
`remember` and `recall` MCP tools exposed by the MCP server.

## HTTP contract

Port `9514` externally, `3000` internally.

### `POST /remember`

Store a piece of knowledge. Returns the new point ID and the timestamp.

```json
{
  "content": "The datasheet schema uses `data` for quotes, not `quotes`.",
  "tags": ["eps", "datasheet", "gotcha"],
  "source": "CT-2037 post-mortem",
  "author": "paul.berry-briggs"
}
```

### `POST /recall`

Retrieve the `top_k` most semantically similar entries. Optional `tags`
filters to entries tagged with *any* of the supplied tags.

```json
{
  "query": "How is the datasheet quote schema laid out?",
  "top_k": 5,
  "tags": ["eps"]
}
```

### `GET /stats` / `GET /health`

Lightweight introspection — point count, configured models, and Qdrant
reachability. Useful for smoke tests and dashboards.

## Architecture

```
MCP tool (remember / recall)
        │  HTTP
        ▼
memory-api (this service)
        │           │
        ▼           ▼
    Voyage AI    Qdrant
  (embeddings)  (vector DB)
```

Voyage produces the embeddings; Qdrant stores and retrieves them. The API
keeps the two clients behind one HTTP interface so MCP (and future
consumers — scripts, Jenkins, the dashboard) don't need the Voyage or
Qdrant SDKs, and neither key escapes the container.

## Embedding model choice

We default to `voyage-3-large` (1024 dims) on both ingest and query.
Mixing `voyage-3-large` with `voyage-3-lite` silently corrupts retrieval —
they do not share an embedding space. `INGEST_MODEL` and `QUERY_MODEL` are
env-configurable if we later move to a model family that does share space
(e.g. voyage-4) and want the cost optimisation of a lighter query model.

Changing `EMBED_DIM` requires recreating the Qdrant collection.

## Environment variables

| Var | Default | Notes |
| --- | --- | --- |
| `VOYAGE_API_KEY` | — | **Required.** From voyageai.com. |
| `QDRANT_URL` | `http://qdrant:6333` | Service name on the compose network. |
| `QDRANT_API_KEY` | — | Matches the key set on the Qdrant service. |
| `COLLECTION_NAME` | `team-knowledge` | |
| `INGEST_MODEL` | `voyage-3-large` | |
| `QUERY_MODEL` | `voyage-3-large` | |
| `EMBED_DIM` | `1024` | Must match the model's output. |

Production values live in `/home/docker/crown-engineering-mcp/.env` on
`poc-containers` and are never committed.

## Security notes

- Qdrant and memory-api are bound to the internal docker network. Only the
  MCP server and (via SSH tunnel, if needed) the host can reach them.
- Do not store secrets, credentials, or customer PII in team memory.
  Treat it as "commit-log-equivalent" — anything shareable in a PR
  description is fine; anything in a `.env` is not.
