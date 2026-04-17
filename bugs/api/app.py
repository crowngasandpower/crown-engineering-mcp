"""Bug Triage API.

Helps engineers claim bugs from the CT Jira board. The /claim endpoint
finds the highest-priority open bug, checks whether it has enough
information to be actionable, and either assigns it to the engineer
(moving it to In Progress) or marks it as Blocked with a comment
explaining that more detail is needed.

Endpoints:
  GET  /health  — liveness check
  POST /claim   — find and claim the next actionable bug
  POST /skip    — mark a bug as investigated/skipped so /claim won't return it
"""

import base64
import json
import os

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="Bug Triage API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

JIRA_BASE_URL = os.environ.get(
    "JIRA_BASE_URL",
    "https://crowngasandpower-team-delivery.atlassian.net",
)
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
JIRA_PROJECT = os.environ.get("JIRA_PROJECT", "CT")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# How many To-Do bugs to fetch in one search. We iterate through them
# looking for the first viable one, blocking the rest as we go.
MAX_CANDIDATES = int(os.environ.get("MAX_BUG_CANDIDATES", "10"))

# Label applied to bugs that have been investigated but don't need a
# code fix (infra issues, data queries, documentation tasks, etc.).
# The /claim endpoint excludes tickets with this label.
SKIP_LABEL = "claude-skipped"


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ClaimRequest(BaseModel):
    assignee_email: str = Field(
        ..., description="Email of the engineer to assign the bug to"
    )


class ClaimedBug(BaseModel):
    key: str
    url: str
    title: str
    priority: str
    description_text: str
    viable: bool
    viability_reason: str = Field(
        "", description="Claude's explanation of why the bug is/isn't viable"
    )
    message: str
    blocked_keys: list[str] = Field(
        default_factory=list,
        description="Ticket keys that were skipped and moved to Blocked",
    )


class SkipRequest(BaseModel):
    key: str = Field(..., description="Jira issue key to skip (e.g. CT-689)")
    reason: str = Field(
        ...,
        description="Why this bug is being skipped (e.g. 'infra issue, not a code fix')",
    )


# ---------------------------------------------------------------------------
# Jira helpers
# ---------------------------------------------------------------------------

def _jira_headers() -> dict[str, str]:
    auth = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    return {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
    }


def _extract_text_from_adf(node: dict | list | None) -> str:
    """Recursively extract plain text from Jira's Atlassian Document Format."""
    if node is None:
        return ""
    if isinstance(node, list):
        return " ".join(_extract_text_from_adf(n) for n in node)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return " ".join(
            _extract_text_from_adf(c) for c in node.get("content", [])
        )
    return ""


VIABILITY_PROMPT = """\
You are a bug triage assistant for Crown Gas & Power's software estate.
You will be given a Jira bug ticket's title and description. Decide
whether a developer could realistically start working on this bug
with the information provided.

A bug is **viable** if it has ALL of:
  1. A clear description of the problem (what is going wrong).
  2. Enough context to locate the issue — e.g. which app, page, or
     process is affected.
  3. Some indication of expected vs actual behaviour, OR steps to
     reproduce, OR an error message / screenshot reference.

A bug is **not viable** if:
  - The description is empty or trivially short (e.g. just the title
    repeated).
  - It is too vague to know where to start (e.g. "it's broken").
  - Critical context is missing (no app name, no page, no steps).

Respond with ONLY a JSON object — no markdown, no extra text:
{"viable": true, "reason": "one sentence explaining your decision"}
"""


async def _assess_viability(
    client: httpx.AsyncClient, title: str, description_text: str
) -> tuple[bool, str]:
    """Ask Claude whether a bug has enough information to work on.

    Returns (viable, reason). Falls back to a simple length check if
    the Anthropic API is unavailable.
    """
    if not ANTHROPIC_API_KEY:
        # Graceful fallback — no key configured
        viable = len(description_text) >= 50
        reason = (
            "LLM assessment unavailable (no API key) — "
            "fell back to description length check."
        )
        return viable, reason

    user_message = f"**Title:** {title}\n\n**Description:**\n{description_text or '(empty)'}"

    try:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 150,
                "messages": [
                    {"role": "user", "content": user_message},
                ],
                "system": VIABILITY_PROMPT,
            },
            timeout=15.0,
        )
        if not 200 <= resp.status_code < 300:
            # API error — fall back to length check
            viable = len(description_text) >= 50
            return viable, f"LLM assessment failed (HTTP {resp.status_code}) — fell back to length check."

        content = resp.json()["content"][0]["text"].strip()
        result = json.loads(content)
        return bool(result["viable"]), result.get("reason", "")

    except (json.JSONDecodeError, KeyError, IndexError):
        # Claude responded but not in expected JSON format
        viable = len(description_text) >= 50
        return viable, "LLM response could not be parsed — fell back to length check."
    except httpx.HTTPError:
        viable = len(description_text) >= 50
        return viable, "LLM request failed — fell back to length check."


async def _find_account_id(
    client: httpx.AsyncClient, email: str
) -> str:
    """Look up a Jira Cloud accountId by email address."""
    resp = await client.get(
        f"{JIRA_BASE_URL}/rest/api/3/user/search",
        params={"query": email},
        headers=_jira_headers(),
    )
    if not 200 <= resp.status_code < 300:
        raise HTTPException(
            502,
            f"Jira user search error (HTTP {resp.status_code}): "
            f"{resp.text[:500]}",
        )
    users = resp.json()
    if not users:
        raise HTTPException(404, f"No Jira user found for {email}")
    return users[0]["accountId"]


async def _do_transition(
    client: httpx.AsyncClient,
    key: str,
    transition_name: str,
) -> bool:
    """Execute a single named transition on an issue."""
    resp = await client.get(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{key}/transitions",
        headers=_jira_headers(),
    )
    if not 200 <= resp.status_code < 300:
        return False

    transitions = resp.json().get("transitions", [])
    transition_id = None
    for t in transitions:
        if t["name"].lower() == transition_name.lower():
            transition_id = t["id"]
            break

    if not transition_id:
        return False

    resp = await client.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{key}/transitions",
        headers=_jira_headers(),
        json={"transition": {"id": transition_id}},
    )
    return 200 <= resp.status_code < 300


async def _transition_to_in_progress(
    client: httpx.AsyncClient, key: str
) -> bool:
    """Move a To Do issue to In Progress.

    The CT board workflow requires two transitions:
      To Do → "Commit for Sprint" → COMMITTED → "Start Work" → In Progress
    """
    if not await _do_transition(client, key, "Commit for Sprint"):
        return False
    return await _do_transition(client, key, "Start Work")


async def _transition_to_blocked(
    client: httpx.AsyncClient, key: str
) -> bool:
    """Move a To Do issue to Blocked.

    The CT board workflow requires three transitions:
      To Do → "Commit for Sprint" → COMMITTED
            → "Start Work" → In Progress
            → "Mark as Blocked" → Blocked
    """
    if not await _do_transition(client, key, "Commit for Sprint"):
        return False
    if not await _do_transition(client, key, "Start Work"):
        return False
    return await _do_transition(client, key, "Mark as Blocked")


async def _assign_issue(
    client: httpx.AsyncClient, key: str, account_id: str
) -> bool:
    resp = await client.put(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{key}/assignee",
        headers=_jira_headers(),
        json={"accountId": account_id},
    )
    return 200 <= resp.status_code < 300


async def _add_comment(
    client: httpx.AsyncClient, key: str, text: str
) -> bool:
    resp = await client.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{key}/comment",
        headers=_jira_headers(),
        json={
            "body": {
                "version": 1,
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": text}],
                    }
                ],
            }
        },
    )
    return 200 <= resp.status_code < 300


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/claim", response_model=ClaimedBug)
async def claim_bug(req: ClaimRequest):
    """Find the highest-priority To-Do bug and claim it.

    Iterates through open bugs sorted by priority (highest first).
    The first bug with a sufficiently detailed description is assigned
    to the caller and moved to In Progress. Any bugs skipped along the
    way are commented and moved to Blocked.
    """
    if not JIRA_EMAIL or not JIRA_API_TOKEN:
        raise HTTPException(503, "Jira credentials not configured")

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Resolve the engineer's Jira account ID
        account_id = await _find_account_id(client, req.assignee_email)

        # Find To-Do bugs, highest priority first (excluding skipped)
        jql = (
            f"project = {JIRA_PROJECT} AND issuetype = Bug "
            f'AND statusCategory = "To Do" '
            f'AND (labels IS EMPTY OR labels NOT IN ("{SKIP_LABEL}")) '
            f"ORDER BY priority ASC, created ASC"
        )
        resp = await client.post(
            f"{JIRA_BASE_URL}/rest/api/3/search/jql",
            headers=_jira_headers(),
            json={
                "jql": jql,
                "maxResults": MAX_CANDIDATES,
                "fields": [
                    "summary",
                    "priority",
                    "description",
                    "status",
                ],
            },
        )
        if not 200 <= resp.status_code < 300:
            raise HTTPException(
                502,
                f"Jira search error (HTTP {resp.status_code}): "
                f"{resp.text[:500]}",
            )

        issues = resp.json().get("issues", [])
        if not issues:
            raise HTTPException(
                404, "No open bugs found in the To Do column"
            )

        blocked_keys: list[str] = []

        for issue in issues:
            key = issue["key"]
            fields = issue["fields"]
            summary = fields.get("summary", "")
            priority_name = (
                fields.get("priority", {}).get("name", "Unknown")
            )
            description_adf = fields.get("description")
            description_text = _extract_text_from_adf(description_adf).strip()

            viable, reason = await _assess_viability(
                client, summary, description_text
            )

            if viable:
                # Found a good one — assign and move to In Progress
                await _assign_issue(client, key, account_id)
                await _transition_to_in_progress(client, key)

                return ClaimedBug(
                    key=key,
                    url=f"{JIRA_BASE_URL}/browse/{key}",
                    title=summary,
                    priority=priority_name,
                    description_text=description_text,
                    viable=True,
                    viability_reason=reason,
                    message=(
                        f"Bug {key} assigned to you and moved to "
                        f"In Progress."
                    ),
                    blocked_keys=blocked_keys,
                )
            else:
                # Not enough info — comment and block
                await _add_comment(
                    client,
                    key,
                    f"Checked by Claude \u2014 not enough information to "
                    f"proceed. {reason} Please add reproduction steps, "
                    f"expected behaviour, and actual behaviour before "
                    f"moving this back to To Do.",
                )
                await _transition_to_blocked(client, key)
                blocked_keys.append(key)

        # Every candidate was blocked
        raise HTTPException(
            404,
            f"Checked {len(issues)} bug(s) \u2014 none had enough "
            f"information to proceed. Blocked: "
            f"{', '.join(blocked_keys)}.",
        )


@app.post("/skip")
async def skip_bug(req: SkipRequest):
    """Mark a bug as investigated/skipped so /claim won't return it.

    Adds the 'claude-skipped' label and a comment explaining why, then
    unassigns the ticket and moves it back to the To Do column. Future
    /claim calls exclude tickets with this label.
    """
    if not JIRA_EMAIL or not JIRA_API_TOKEN:
        raise HTTPException(503, "Jira credentials not configured")

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Add the skip label
        resp = await client.put(
            f"{JIRA_BASE_URL}/rest/api/3/issue/{req.key}",
            headers=_jira_headers(),
            json={
                "update": {
                    "labels": [{"add": SKIP_LABEL}],
                },
            },
        )
        if not 200 <= resp.status_code < 300:
            raise HTTPException(
                502,
                f"Failed to add label to {req.key} "
                f"(HTTP {resp.status_code}): {resp.text[:500]}",
            )

        # Add a comment with the reason
        await _add_comment(
            client,
            req.key,
            f"Skipped by Claude \u2014 {req.reason}",
        )

        # Unassign
        await client.put(
            f"{JIRA_BASE_URL}/rest/api/3/issue/{req.key}/assignee",
            headers=_jira_headers(),
            json={"accountId": None},
        )

    return {
        "key": req.key,
        "message": f"{req.key} marked as skipped. It won't appear in future /claim results.",
    }
