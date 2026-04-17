#!/bin/bash
# AI Code Review — Posts Claude-powered review comments on GitHub PRs
#
# Required environment variables:
#   ANTHROPIC_API_KEY  — Claude API key
#   GITHUB_TOKEN       — GitHub personal access token with repo scope
#   GITHUB_ORG         — GitHub organisation (e.g. crowngasandpower)
#   GITHUB_REPO        — Repository name (e.g. eps)
#   PR_NUMBER          — Pull request number
#
# Optional:
#   CLAUDE_MODEL       — Model to use (default: claude-sonnet-4-6)
#   MAX_DIFF_TOKENS    — Skip review if diff exceeds this (default: 150000)
#   REVIEW_CONFIG      — Path to custom review config (default: .ai-review.yml in repo root)
#
# Requires: curl, jq
#   Install jq on Ubuntu/Debian: sudo apt-get install -y jq
#   Install jq on CentOS/RHEL:   sudo yum install -y jq

# --- Dependency check ---
if ! command -v jq &> /dev/null; then
    echo "ERROR: jq is not installed. Install it with: sudo apt-get install -y jq"
    exit 2
fi

set -euo pipefail

CLAUDE_MODEL="${CLAUDE_MODEL:-claude-sonnet-4-6}"
MAX_DIFF_TOKENS="${MAX_DIFF_TOKENS:-150000}"
API_URL="https://api.anthropic.com/v1/messages"
GITHUB_API="https://api.github.com"

# Load rate limit utilities
source "$(dirname "$0")/github-utils.sh"

echo "=== AI Code Review ==="
echo "Repo: ${GITHUB_ORG}/${GITHUB_REPO} PR #${PR_NUMBER}"
echo "Model: ${CLAUDE_MODEL}"

# --- Step 1: Fetch the PR diff ---
echo "Fetching PR diff..."
DIFF=$(github_api GET "/repos/${GITHUB_ORG}/${GITHUB_REPO}/pulls/${PR_NUMBER}" \
  -H "Accept: application/vnd.github.v3.diff")

if [ -z "$DIFF" ]; then
  echo "ERROR: Empty diff — PR may have no changes"
  exit 1
fi

DIFF_LINES=$(echo "$DIFF" | wc -l)
echo "Diff: ${DIFF_LINES} lines"

# --- Step 2: Fetch PR metadata ---
echo "Fetching PR metadata..."
PR_META=$(github_api GET "/repos/${GITHUB_ORG}/${GITHUB_REPO}/pulls/${PR_NUMBER}")

PR_TITLE=$(echo "$PR_META" | jq -r '.title')
PR_ADDITIONS=$(echo "$PR_META" | jq -r '.additions // 0')
PR_DELETIONS=$(echo "$PR_META" | jq -r '.deletions // 0')
LINES_CHANGED=$((PR_ADDITIONS + PR_DELETIONS))
PR_BODY=$(echo "$PR_META" | jq -r '.body // ""' | head -50)
echo "PR: ${PR_TITLE}"

# --- Step 3: Build the review prompt ---
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
- Cross-app database references: if code uses a non-default database connection (e.g. ->on('econtracts'), DB::connection('ces_elec'), \$connection = 'synergy') to query or create records in a table, flag that the migration for that table lives in the OTHER app's repo and must exist there. Models that reference another app's database are a coupling risk — the reviewer should verify the target table exists and the migration is not missing from the target repo.

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
  - Flagging routes in routes/web.php as "missing auth middleware" just because you don't see an auth middleware group in the diff. In Laravel, auth middleware is often applied globally to the entire web.php file in RouteServiceProvider (e.g. Route::middleware(['web', 'auth:synergy'])->group(base_path('routes/web.php'))). The diff won't show this — it's in a file that isn't changed. Unless you can see from the diff that routes are explicitly placed OUTSIDE a middleware group that other routes in the same file are inside, do not assume routes lack auth.
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

If there ARE issues, end the summary with this line:
"If you disagree with any of these findings, you can suppress them by adding a comment before the flagged code: \`// @ai-review-ignore: <reason>\`"

Only comment on lines that are ADDED (lines starting with +) in the diff. Do not comment on removed lines or unchanged context.
Respond ONLY with the JSON object, no markdown fences or other text.
SYSPROMPT
)

# --- Step 4: Send to Claude for review ---
echo "Sending to Claude for review..."

# Build the user message content
USER_MESSAGE="Review this pull request.

Title: ${PR_TITLE}
Description: ${PR_BODY}

Diff:
${DIFF}"

# Build the JSON request via temp files to avoid shell argument length limits.
# Large diffs can exceed the OS argument size limit for both shell variables
# and jq --arg parameters.
REVIEW_REQUEST_FILE=$(mktemp)
USER_MESSAGE_FILE=$(mktemp)
SYSTEM_PROMPT_FILE=$(mktemp)
trap "rm -f $REVIEW_REQUEST_FILE $USER_MESSAGE_FILE $SYSTEM_PROMPT_FILE" EXIT

echo "$USER_MESSAGE" > "$USER_MESSAGE_FILE"
echo "$SYSTEM_PROMPT" > "$SYSTEM_PROMPT_FILE"

jq -n \
  --arg model "$CLAUDE_MODEL" \
  --rawfile system "$SYSTEM_PROMPT_FILE" \
  --rawfile content "$USER_MESSAGE_FILE" \
  '{
    model: $model,
    max_tokens: 4096,
    system: $system,
    messages: [
      {
        role: "user",
        content: $content
      }
    ]
  }' > "$REVIEW_REQUEST_FILE"

echo "Request size: $(wc -c < "$REVIEW_REQUEST_FILE") bytes"

RESPONSE=$(curl -s \
  -X POST "${API_URL}" \
  -H "x-api-key: ${ANTHROPIC_API_KEY}" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d @"$REVIEW_REQUEST_FILE")

if [ -z "$RESPONSE" ]; then
  echo "ERROR: Empty response from Claude API"
  exit 1
fi

# Extract the text content from Claude's response
REVIEW_RAW=$(echo "$RESPONSE" | jq -r '.content[0].text // empty')

if [ -z "$REVIEW_RAW" ]; then
  echo "ERROR: No text in Claude response"
  echo "Response: $(echo "$RESPONSE" | head -5)"
  exit 1
fi

# Strip markdown code fences if Claude wrapped the JSON in ```json ... ```
REVIEW_TEXT=$(echo "$REVIEW_RAW" | sed 's/^```json//; s/^```//; s/```$//' | sed '/^$/d')
echo "Review received."

# --- Step 5: Parse the review ---
VERDICT=$(echo "$REVIEW_TEXT" | jq -r '.verdict // "comment"')
SUMMARY=$(echo "$REVIEW_TEXT" | jq -r '.summary // "Review completed."')
ISSUE_COUNT=$(echo "$REVIEW_TEXT" | jq '.issues | length')
HIGH_COUNT=$(echo "$REVIEW_TEXT" | jq '[.issues[] | select(.severity == "high")] | length')

echo "Verdict: ${VERDICT}"
echo "Issues: ${ISSUE_COUNT} (${HIGH_COUNT} high priority)"

# --- Step 6: Post review to GitHub ---
echo "Posting review to GitHub..."

# Advisory mode — always post as COMMENT, never block
case "$VERDICT" in
  "approve")
    GH_EVENT="COMMENT"
    ;;
  *)
    GH_EVENT="COMMENT"
    ;;
esac

# Build the review body with all issues inline.
# Use a temp file and write with actual newlines — jq --arg treats \n literally.
REVIEW_BODY_FILE=$(mktemp)

{
  echo "## AI Code Review (Claude)"
  echo ""
  echo "$SUMMARY"
  echo ""

  if [ "$ISSUE_COUNT" -gt 0 ]; then
    echo "**${ISSUE_COUNT} issues found** (${HIGH_COUNT} high priority)"
    echo ""

    for i in $(seq 0 $((ISSUE_COUNT - 1))); do
      FILE=$(echo "$REVIEW_TEXT" | jq -r ".issues[$i].path // \"unknown\"")
      LINE=$(echo "$REVIEW_TEXT" | jq -r ".issues[$i].line // \"?\"")
      SEV=$(echo "$REVIEW_TEXT" | jq -r ".issues[$i].severity // \"low\"")
      MSG=$(echo "$REVIEW_TEXT" | jq -r ".issues[$i].message // \"\"")

      case "$SEV" in
        "high")   ICON=":red_circle:" ;;
        "medium") ICON=":orange_circle:" ;;
        *)        ICON=":white_circle:" ;;
      esac

      SEV_UPPER=$(echo "$SEV" | tr '[:lower:]' '[:upper:]')
      echo "${ICON} **${SEV_UPPER}** \`${FILE}:${LINE}\`"
      echo "${MSG}"
      echo ""
    done

    echo "---"
    echo "_This review is advisory only — it will not block your PR._"
    echo ""
    echo "<sub>Reviewed commit: $(echo "$PR_META" | jq -r '.head.sha // "unknown"')</sub>"
  else
    echo "No issues found. Looks good! :white_check_mark:"
    echo ""
    echo "<sub>Reviewed commit: $(echo "$PR_META" | jq -r '.head.sha // "unknown"')</sub>"
  fi
} > "$REVIEW_BODY_FILE"

# Post the review — use rawfile so newlines are preserved
REVIEW_PAYLOAD_FILE=$(mktemp)
jq -n --rawfile body "$REVIEW_BODY_FILE" --arg event "$GH_EVENT" \
  '{body: $body, event: $event}' > "$REVIEW_PAYLOAD_FILE"

REVIEW_BODY_CONTENT=$(cat "$REVIEW_BODY_FILE")
rm -f "$REVIEW_BODY_FILE"

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -X POST "${GITHUB_API}/repos/${GITHUB_ORG}/${GITHUB_REPO}/pulls/${PR_NUMBER}/reviews" \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -d @"$REVIEW_PAYLOAD_FILE")

rm -f "$REVIEW_PAYLOAD_FILE"

if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
  echo "Review posted successfully (HTTP ${HTTP_CODE})"
else
  echo "ERROR: Failed to post review (HTTP ${HTTP_CODE})"
fi

# --- Step 7: Push metrics ---
PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-}"

if [ -n "$PUSHGATEWAY_URL" ]; then
  # Get the PR author
  PR_AUTHOR=$(echo "$PR_META" | jq -r '.user.login // "unknown"')
  MEDIUM_COUNT=$(echo "$REVIEW_TEXT" | jq '[.issues[] | select(.severity == "medium")] | length')
  LOW_COUNT=$(echo "$REVIEW_TEXT" | jq '[.issues[] | select(.severity == "low")] | length')

  cat <<METRICS | curl -s --data-binary @- "${PUSHGATEWAY_URL}/metrics/job/ai_code_review/engineer/${PR_AUTHOR}/repo/${GITHUB_REPO}"
# HELP code_review_prs_total Total PRs reviewed
# TYPE code_review_prs_total counter
code_review_prs_total 1
# HELP code_review_issues_total Total issues found by severity
# TYPE code_review_issues_total counter
code_review_issues_total{severity="high"} ${HIGH_COUNT}
code_review_issues_total{severity="medium"} ${MEDIUM_COUNT}
code_review_issues_total{severity="low"} ${LOW_COUNT}
METRICS

  echo "Metrics pushed to Pushgateway for ${PR_AUTHOR}"
else
  echo "Skipping metrics push (PUSHGATEWAY_URL not set)"
fi

# --- Step 8: Upsert review to PostgreSQL via PostgREST ---
POSTGREST_URL="${POSTGREST_URL:-}"

if [ -n "$POSTGREST_URL" ]; then
  PR_AUTHOR=$(echo "$PR_META" | jq -r '.user.login // "unknown"')
  MEDIUM_COUNT="${MEDIUM_COUNT:-$(echo "$REVIEW_TEXT" | jq '[.issues[] | select(.severity == "medium")] | length')}"
  LOW_COUNT="${LOW_COUNT:-$(echo "$REVIEW_TEXT" | jq '[.issues[] | select(.severity == "low")] | length')}"
  TOTAL_COUNT=$((HIGH_COUNT + MEDIUM_COUNT + LOW_COUNT))
  PR_CREATED_AT=$(echo "$PR_META" | jq -r '.created_at // empty')
  HEAD_SHA=$(echo "$PR_META" | jq -r '.head.sha // ""')

  UPSERT_FILE=$(mktemp)
  BODY_TMP=$(mktemp)
  printf '%s' "${REVIEW_BODY_CONTENT:-}" > "$BODY_TMP"

  jq -n \
    --arg repo "$GITHUB_REPO" \
    --argjson pr "$PR_NUMBER" \
    --arg engineer "$PR_AUTHOR" \
    --arg pr_title "$PR_TITLE" \
    --arg pr_created_at "$PR_CREATED_AT" \
    --arg head_sha "$HEAD_SHA" \
    --argjson high "$HIGH_COUNT" \
    --argjson medium "$MEDIUM_COUNT" \
    --argjson low "$LOW_COUNT" \
    --argjson total "$TOTAL_COUNT" \
    --argjson lines_changed "$LINES_CHANGED" \
    --rawfile review_body "$BODY_TMP" \
    '{repo: $repo, pr: $pr, engineer: $engineer, pr_title: $pr_title,
      pr_created_at: $pr_created_at, head_sha: $head_sha,
      high: $high, medium: $medium, low: $low, total: $total,
      lines_changed: $lines_changed, review_body: $review_body}' > "$UPSERT_FILE" 2>/dev/null

  rm -f "$BODY_TMP"

  # If jq failed (e.g. body encoding issue), retry without body
  if [ ! -s "$UPSERT_FILE" ]; then
    jq -n \
      --arg repo "$GITHUB_REPO" \
      --argjson pr "$PR_NUMBER" \
      --arg engineer "$PR_AUTHOR" \
      --arg pr_title "$PR_TITLE" \
      --arg pr_created_at "$PR_CREATED_AT" \
      --arg head_sha "$HEAD_SHA" \
      --argjson high "$HIGH_COUNT" \
      --argjson medium "$MEDIUM_COUNT" \
      --argjson low "$LOW_COUNT" \
      --argjson total "$TOTAL_COUNT" \
      --argjson lines_changed "$LINES_CHANGED" \
      '{repo: $repo, pr: $pr, engineer: $engineer, pr_title: $pr_title,
        pr_created_at: $pr_created_at, head_sha: $head_sha,
        high: $high, medium: $medium, low: $low, total: $total,
        lines_changed: $lines_changed, review_body: ""}' > "$UPSERT_FILE"
  fi

  DB_RESPONSE_FILE=$(mktemp)
  db_code=$(curl -s -o "$DB_RESPONSE_FILE" -w "%{http_code}" \
    -X POST "${POSTGREST_URL}/reviews" \
    -H "Content-Type: application/json" \
    -H "Prefer: resolution=merge-duplicates" \
    -d @"$UPSERT_FILE")

  rm -f "$UPSERT_FILE"

  if [ "$db_code" -ge 200 ] && [ "$db_code" -lt 300 ]; then
    echo "Review saved to DB for ${PR_AUTHOR}"
  else
    echo "WARNING: DB upsert failed (HTTP ${db_code})"
    cat "$DB_RESPONSE_FILE" >&2
  fi
  rm -f "$DB_RESPONSE_FILE"
else
  echo "Skipping DB push (POSTGREST_URL not set)"
fi

# --- Step 9: Advisory mode — always exit 0 ---
echo "Review complete (advisory mode — PR not blocked)"
exit 0
