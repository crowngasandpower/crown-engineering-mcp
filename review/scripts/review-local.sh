#!/bin/bash
# Local AI Code Review — review uncommitted or branch changes before pushing.
#
# Usage:
#   bash review-local.sh                    # review staged + unstaged changes
#   bash review-local.sh main               # review diff against a base branch
#   bash review-local.sh origin/main HEAD   # review diff between two refs
#
# Required (one of):
#   REVIEW_API_URL    — Review API endpoint (no API key needed locally)
#   ANTHROPIC_API_KEY — Claude API key (direct mode, no server needed)
#
# Optional:
#   CLAUDE_MODEL — model to use (default: claude-sonnet-4-6)

set -euo pipefail

CLAUDE_MODEL="${CLAUDE_MODEL:-claude-sonnet-4-6}"
API_URL="https://api.anthropic.com/v1/messages"
REVIEW_API_URL="${REVIEW_API_URL:-}"

if ! command -v jq &> /dev/null; then
    echo "ERROR: jq is not installed."
    exit 2
fi

if [ -z "$REVIEW_API_URL" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ERROR: Set REVIEW_API_URL or ANTHROPIC_API_KEY"
    exit 1
fi

# Build the diff
if [ $# -eq 0 ]; then
    echo "Reviewing: working tree changes (staged + unstaged)"
    DIFF=$(git diff HEAD 2>/dev/null || git diff)
elif [ $# -eq 1 ]; then
    BASE="$1"
    echo "Reviewing: diff against ${BASE}"
    DIFF=$(git diff "${BASE}"...HEAD)
else
    echo "Reviewing: diff ${1}..${2}"
    DIFF=$(git diff "$1" "$2")
fi

if [ -z "$DIFF" ]; then
    echo "No changes to review."
    exit 0
fi

DIFF_LINES=$(echo "$DIFF" | wc -l)
echo "Diff: ${DIFF_LINES} lines"
echo "Sending to Claude for review..."

SYSTEM_PROMPT=$(cat <<'SYSPROMPT'
You are an expert code reviewer for Crown Gas and Power's Laravel applications. Review the provided pull request diff and identify issues in these categories:

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
SYSPROMPT
)

# Write diff to temp file to avoid argument length limits
DIFF_FILE=$(mktemp)
echo "$DIFF" > "$DIFF_FILE"

SYS_FILE=$(mktemp)
echo "$SYSTEM_PROMPT" > "$SYS_FILE"

REQUEST_FILE=$(mktemp)
jq -n \
    --rawfile sys "$SYS_FILE" \
    --rawfile diff "$DIFF_FILE" \
    --arg model "$CLAUDE_MODEL" \
    '{
        model: $model,
        max_tokens: 4096,
        system: $sys,
        messages: [{role: "user", content: ("Review this diff:\n\n" + $diff)}]
    }' > "$REQUEST_FILE"

rm -f "$SYS_FILE" "$DIFF_FILE"

RESPONSE_FILE=$(mktemp)
HTTP_CODE=$(curl -s -o "$RESPONSE_FILE" -w "%{http_code}" \
    -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "x-api-key: ${ANTHROPIC_API_KEY}" \
    -H "anthropic-version: 2023-06-01" \
    -d @"$REQUEST_FILE")

rm -f "$REQUEST_FILE"

if [ "$HTTP_CODE" -lt 200 ] || [ "$HTTP_CODE" -ge 300 ]; then
    echo "ERROR: Claude API returned HTTP ${HTTP_CODE}"
    cat "$RESPONSE_FILE"
    rm -f "$RESPONSE_FILE"
    exit 1
fi

RAW_TEXT=$(cat "$RESPONSE_FILE" | jq -r '.content[0].text // empty')
rm -f "$RESPONSE_FILE"

# Claude sometimes wraps JSON in markdown code fences — strip them
REVIEW_TEXT=$(echo "$RAW_TEXT" | sed '/^```json$/d; /^```$/d')

if [ -z "$REVIEW_TEXT" ]; then
    echo "ERROR: Empty response from Claude"
    exit 1
fi

# Parse and display
SUMMARY=$(echo "$REVIEW_TEXT" | jq -r '.summary // "No summary"')
ISSUE_COUNT=$(echo "$REVIEW_TEXT" | jq '[.issues // [] | .[]] | length')
HIGH_COUNT=$(echo "$REVIEW_TEXT" | jq '[.issues[]? | select(.severity == "high")] | length')
MEDIUM_COUNT=$(echo "$REVIEW_TEXT" | jq '[.issues[]? | select(.severity == "medium")] | length')
LOW_COUNT=$(echo "$REVIEW_TEXT" | jq '[.issues[]? | select(.severity == "low")] | length')

echo ""
echo "================================================"
echo "  Review: ${ISSUE_COUNT} issues (${HIGH_COUNT} high, ${MEDIUM_COUNT} medium, ${LOW_COUNT} low)"
echo "================================================"
echo ""
echo "$SUMMARY"
echo ""

if [ "$ISSUE_COUNT" -gt 0 ]; then
    for i in $(seq 0 $((ISSUE_COUNT - 1))); do
        FILE=$(echo "$REVIEW_TEXT" | jq -r ".issues[$i].path // \"unknown\"")
        LINE=$(echo "$REVIEW_TEXT" | jq -r ".issues[$i].line // \"?\"")
        SEV=$(echo "$REVIEW_TEXT" | jq -r ".issues[$i].severity // \"low\"")
        MSG=$(echo "$REVIEW_TEXT" | jq -r ".issues[$i].message // \"\"")

        case "$SEV" in
            "high")   ICON="[HIGH]" ;;
            "medium") ICON="[MED] " ;;
            *)        ICON="[LOW] " ;;
        esac

        echo "${ICON} ${FILE}:${LINE}"
        echo "       ${MSG}"
        echo ""
    done
fi
