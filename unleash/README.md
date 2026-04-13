# Unleash Feature Flag API

Thin wrapper over the Unleash admin API. Lets engineers (and Claude, via the MCP server) create, inspect, and toggle feature flags without holding the admin token on their machines.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/flags` | Create a new flag for a ticket. Idempotent (409 → returns existing). |
| `GET` | `/flags/{name}` | Full flag state including per-env enabled flags. |
| `POST` | `/flags/{name}/toggle` | Enable or disable in one environment. Body: `{environment, enabled}`. |

### Naming

Server-enforced format: `CT-<ticket-number>-<ShortName>`, e.g. `CT-2037-Deadlock-Retry`. Rejected if the shape is wrong.

### Environments

`development`, `UAT`, `production`. Case-insensitive on the wire (`dev`, `uat`, `prod` also accepted); canonicalised to Unleash's exact names before the API call.

### Flag types

`release` (default), `experiment`, `operational`, `kill-switch`, `permission`.

## Configuration

Reads from environment:

| Var | Required | Notes |
| --- | --- | --- |
| `UNLEASH_BASE_URL` | Yes | e.g. `https://eu.app.getunleash.io/eull0051` |
| `UNLEASH_PROJECT` | No | Default `core-products` |
| `UNLEASH_ADMIN_TOKEN` | Yes | Global admin token (format `user:...`) |

## Example

```bash
# Create a flag (returns shape with 3 environment entries, all enabled=false)
curl -s -X POST http://poc-containers:9511/flags \
  -H 'Content-Type: application/json' \
  -d '{"ticket_key":"CT-2037","short_name":"Deadlock-Retry","description":"Retry on deadlock for site_nhh writes"}'

# Enable in development
curl -s -X POST http://poc-containers:9511/flags/CT-2037-Deadlock-Retry/toggle \
  -H 'Content-Type: application/json' \
  -d '{"environment":"development","enabled":true}'

# Check state across all envs
curl -s http://poc-containers:9511/flags/CT-2037-Deadlock-Retry
```

## Out of scope (for now)

- **Strategies** — new flags are created with whatever default Unleash provides. Fine-grained strategy config (gradual rollout, user-targeting, constraints) is done in the Unleash UI. Could be exposed here later if there's demand.
- **Archive / list** — not exposed yet; engineers can use the Unleash UI. Cheap to add if needed.
- **Per-environment tokens** — intentionally not used. The admin token handles everything; single source of credentials, single point of rotation.
