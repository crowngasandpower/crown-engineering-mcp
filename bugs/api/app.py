"""Bug Triage API.

Helps engineers claim bugs from the CT Jira board. The /claim endpoint
finds the highest-priority open bug, checks whether it has enough
information to be actionable, and either assigns it to the engineer
(moving it to In Progress) or marks it as Blocked with a comment
explaining that more detail is needed.

Endpoints:
  GET  /health  — liveness check
  POST /claim   — find and claim the next actionable bug
"""

import base64
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

# How many To-Do bugs to fetch in one search. We iterate through them
# looking for the first viable one, blocking the rest as we go.
MAX_CANDIDATES = int(os.environ.get("MAX_BUG_CANDIDATES", "10"))

# Minimum plain-text length in the description for a bug to be
# considered "viable" (i.e. has enough information to work on).
MIN_DESCRIPTION_LENGTH = int(os.environ.get("MIN_DESCRIPTION_LENGTH", "50"))


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
    message: str
    blocked_keys: list[str] = Field(
        default_factory=list,
        description="Ticket keys that were skipped and moved to Blocked",
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


def _is_viable(description_adf: dict | None) -> bool:
    """A bug is viable if its description has meaningful detail."""
    text = _extract_text_from_adf(description_adf).strip()
    return len(text) >= MIN_DESCRIPTION_LENGTH


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

        # Find To-Do bugs, highest priority first
        jql = (
            f"project = {JIRA_PROJECT} AND issuetype = Bug "
            f'AND statusCategory = "To Do" '
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

            if _is_viable(description_adf):
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
                    "Checked by Claude \u2014 not enough information to "
                    "proceed. Please add reproduction steps, expected "
                    "behaviour, and actual behaviour before moving this "
                    "back to To Do.",
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
