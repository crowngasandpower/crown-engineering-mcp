"""
AI Code Review API — accepts a diff, returns a Claude review.

POST /review
  Body: {"diff": "...git diff output..."}
  Response: {"summary": "...", "verdict": "comment", "issues": [...]}

The Anthropic API key is stored server-side. Engineers only need
the endpoint URL — no credentials on their machine.
"""

import os
import json
import re
import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

app = FastAPI(title="AI Code Review API", version="1.0.0")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_DIFF_CHARS = int(os.environ.get("MAX_DIFF_CHARS", "500000"))
POSTGREST_URL = os.environ.get("POSTGREST_URL", "http://postgrest:3000")
GITHUB_ORG = os.environ.get("GITHUB_ORG", "crowngasandpower")

SYSTEM_PROMPT = """You are an expert code reviewer for Crown Gas and Power's Laravel applications. Review the provided pull request diff and identify issues in these categories:

**HIGH (important issues to flag):**
- Security vulnerabilities (SQL injection, XSS, command injection, hardcoded secrets)
- Data loss risks (destructive database migrations without expand/contract pattern)
- Missing authentication or authorization checks
- env() calls in app/ code (should use config() — breaks under php artisan config:cache). env() is ONLY permitted inside config/ files.
- Hardcoded filesystem paths in application code (/mnt/genus, /mnt/open-accounts, UNC paths like \\\\192.168.x.x\\, etc.). Paths must come from config/env, not be hardcoded in app/, routes/, or resource/ files. Hardcoded paths in config/ file defaults are acceptable.

**MEDIUM (should fix):**
- shell_exec/exec without error checking
- N+1 query patterns (queries inside loops)
- Missing try/catch on external API calls or database calls to external connections
- Unescaped user input in SQL queries

**LOW (nice to have):**
- Code style improvements
- Better naming
- Opportunities to use Laravel features (Storage facade, collections, etc.)
- Commented-out code that should be removed

IMPORTANT RULES:
- Always use verdict "comment" — never "request_changes" or "approve". We are in advisory mode only.
- Only flag concrete bugs or mistakes in the application code being reviewed. Do NOT flag:
  - Defence-in-depth suggestions ("consider adding X as an extra layer", "add a guard in the controller as backup")
  - Risks that depend on misconfiguration of other components (TrustProxies, web server, DNS, etc.)
  - Issues systemic to all Laravel applications (e.g. $request->ip() proxy trust, CSRF token handling, session driver limitations)
  - Hypothetical attack vectors that require the attacker to already control infrastructure (DNS spoofing, proxy header injection)
  - Missing features or enhancements ("consider also checking X", "you could add Y")
  - Suggesting auth checks should be duplicated in controllers when route middleware already provides auth — Laravel's middleware IS the auth layer
  - Speculation about what might happen "if the route were ever changed" or "if middleware were removed" — review the code as it IS, not hypothetical future states
  - "config() might return null" warnings when the PR is moving env() calls into config files — this is the same null risk env() already had, not a new bug. The migration from env() to config() is intentionally mechanical.
  - config values used as database/table names in cross-database joins (e.g. config('database.external_databases.synergy') . '.users') — these cannot be parameterised in SQL and are trusted infrastructure config, not user input
  - shell_exec/SSH commands where ALL arguments come from config values and/or database-generated integer IDs — these are trusted values, not user input
- DO flag: actual bugs, real security holes in the code as written, env() misuse, hardcoded secrets, SQL injection with USER INPUT, routes that genuinely lack auth middleware, data loss risks.
- If a line or block has an @ai-review-ignore comment (e.g. "// @ai-review-ignore: unauthenticated by design for customer-facing links"), do NOT flag that code. The team has explicitly accepted the risk. Mention the suppression in the summary but do not create an issue for it.

For each issue found, respond with a JSON object in this exact format:
{
  "summary": "One paragraph summary of the review",
  "verdict": "comment",
  "issues": [
    {
      "path": "relative/file/path.php",
      "line": 42,
      "severity": "high" | "medium" | "low",
      "message": "Clear explanation of the issue and how to fix it"
    }
  ]
}

If the diff is clean with no issues, respond with verdict "comment", a positive summary, and an empty issues array.
"""


class ReviewRequest(BaseModel):
    diff: str


class ReviewIssue(BaseModel):
    path: str = "unknown"
    line: int | str = "?"
    severity: str = "low"
    message: str = ""


class ReviewResponse(BaseModel):
    summary: str
    verdict: str = "comment"
    issues: list[ReviewIssue] = []
    high: int = 0
    medium: int = 0
    low: int = 0
    total: int = 0
    error: str | None = None


@app.get("/health")
async def health():
    return {"status": "ok", "model": CLAUDE_MODEL}


class ReviewSummary(BaseModel):
    repo: str
    pr: int
    pr_url: str
    engineer: str
    pr_title: str
    pr_created_at: str
    head_sha: str
    high: int
    medium: int
    low: int
    total: int
    lines_changed: int
    reviewed_at: str


class ReviewListResponse(BaseModel):
    total_matched: int
    reviews: list[ReviewSummary]


@app.get("/reviews", response_model=ReviewListResponse)
async def list_reviews(
    engineer: str | None = Query(None, description="Filter by engineer (exact match)"),
    repo: str | None = Query(None, description="Filter by repo name (exact match)"),
    min_high: int = Query(0, ge=0, description="Minimum high-severity count"),
    min_medium: int = Query(0, ge=0, description="Minimum medium-severity count"),
    min_low: int = Query(0, ge=0, description="Minimum low-severity count"),
    since: str | None = Query(None, description="Only PRs created on/after this ISO date"),
    limit: int = Query(50, ge=1, le=500, description="Max rows returned"),
):
    """List reviewed PRs, sorted most-problematic first.

    Wraps PostgREST so callers don't need to know the table or its query
    syntax. Default sort is high desc, medium desc, low desc, total desc —
    most problematic at the top. All filters are AND-ed.
    """
    pg_params: list[tuple[str, str]] = []
    if engineer:
        pg_params.append(("engineer", f"eq.{engineer}"))
    if repo:
        pg_params.append(("repo", f"eq.{repo}"))
    if min_high > 0:
        pg_params.append(("high", f"gte.{min_high}"))
    if min_medium > 0:
        pg_params.append(("medium", f"gte.{min_medium}"))
    if min_low > 0:
        pg_params.append(("low", f"gte.{min_low}"))
    if since:
        if not ISO_DATE_RE.match(since):
            raise HTTPException(
                status_code=422,
                detail="`since` must be an ISO date in YYYY-MM-DD format",
            )
        pg_params.append(("pr_created_at", f"gte.{since}"))

    pg_params.append(("order", "high.desc,medium.desc,low.desc,total.desc"))
    pg_params.append(("limit", str(limit)))

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{POSTGREST_URL}/reviews",
            params=pg_params,
            headers={"Prefer": "count=exact"},
        )

        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"PostgREST error (HTTP {resp.status_code}): {resp.text[:500]}",
            )

        rows = resp.json()
        # Content-Range looks like "0-12/47" — last segment is the total match count
        total_matched = len(rows)
        content_range = resp.headers.get("content-range", "")
        if "/" in content_range:
            try:
                total_matched = int(content_range.rsplit("/", 1)[1])
            except (ValueError, IndexError):
                pass

    reviews = [
        ReviewSummary(
            repo=row["repo"],
            pr=row["pr"],
            pr_url=f"https://github.com/{GITHUB_ORG}/{row['repo']}/pull/{row['pr']}",
            engineer=row["engineer"],
            pr_title=row.get("pr_title", "") or "",
            pr_created_at=row.get("pr_created_at", "") or "",
            head_sha=row.get("head_sha", "") or "",
            high=row.get("high", 0) or 0,
            medium=row.get("medium", 0) or 0,
            low=row.get("low", 0) or 0,
            total=row.get("total", 0) or 0,
            lines_changed=row.get("lines_changed", 0) or 0,
            reviewed_at=row.get("reviewed_at", "") or "",
        )
        for row in rows
    ]

    return ReviewListResponse(total_matched=total_matched, reviews=reviews)


@app.post("/review", response_model=ReviewResponse)
async def review(req: ReviewRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    if not req.diff or not req.diff.strip():
        return ReviewResponse(summary="No changes to review.", issues=[])

    if len(req.diff) > MAX_DIFF_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"Diff too large ({len(req.diff)} chars, max {MAX_DIFF_CHARS})",
        )

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": f"Review this diff:\n\n{req.diff}"}],
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json=payload,
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Claude API error (HTTP {resp.status_code}): {resp.text[:500]}",
        )

    response_data = resp.json()
    raw_text = response_data.get("content", [{}])[0].get("text", "")

    if not raw_text:
        raise HTTPException(status_code=502, detail="Empty response from Claude")

    # Strip markdown code fences if present
    cleaned = raw_text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        review_data = json.loads(cleaned)
    except json.JSONDecodeError:
        return ReviewResponse(
            summary="Failed to parse review response",
            error=raw_text[:1000],
        )

    issues = review_data.get("issues", [])
    high = sum(1 for i in issues if i.get("severity") == "high")
    medium = sum(1 for i in issues if i.get("severity") == "medium")
    low = sum(1 for i in issues if i.get("severity") == "low")

    return ReviewResponse(
        summary=review_data.get("summary", ""),
        verdict=review_data.get("verdict", "comment"),
        issues=[ReviewIssue(**i) for i in issues],
        high=high,
        medium=medium,
        low=low,
        total=len(issues),
    )
