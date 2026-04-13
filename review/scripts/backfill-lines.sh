#!/bin/bash
# Backfill lines_changed for existing reviews in PostgreSQL.
# Fetches additions+deletions from GitHub API for each review row
# and PATCHes the lines_changed column via PostgREST.
#
# Required:
#   GITHUB_TOKEN  — GitHub PAT
#   GITHUB_ORG    — GitHub org (crowngasandpower)
#   POSTGREST_URL — PostgREST URL

set -uo pipefail

GITHUB_API="https://api.github.com"

source "$(dirname "$0")/github-utils.sh"

UPDATED=0
SKIPPED=0
ERRORS=0

echo "=== Backfill lines_changed ==="
echo "PostgREST: ${POSTGREST_URL}"
echo ""

# Get all reviews that have lines_changed = 0
REVIEWS=$(curl -s -G "${POSTGREST_URL}/reviews" \
    --data-urlencode "lines_changed=eq.0" \
    --data-urlencode "select=repo,pr" \
    -H "Accept: application/json")

TOTAL=$(echo "$REVIEWS" | jq length)
echo "Found ${TOTAL} reviews to update"
echo ""

echo "$REVIEWS" | jq -r '.[] | "\(.repo)|\(.pr)"' | while IFS='|' read -r repo pr; do
    [ -z "$repo" ] && continue

    # Fetch PR stats from GitHub
    pr_json=$(github_api GET "/repos/${GITHUB_ORG}/${repo}/pulls/${pr}" 2>/dev/null || echo "{}")
    additions=$(echo "$pr_json" | jq '.additions // 0' 2>/dev/null)
    deletions=$(echo "$pr_json" | jq '.deletions // 0' 2>/dev/null)
    lines_changed=$((additions + deletions))

    if [ "$lines_changed" -eq 0 ]; then
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    # PATCH via PostgREST
    http_code=$(curl -s -o /dev/null -w "%{http_code}" \
        -X PATCH "${POSTGREST_URL}/reviews?repo=eq.${repo}&pr=eq.${pr}" \
        -H "Content-Type: application/json" \
        -d "{\"lines_changed\": ${lines_changed}}")

    if [ "$http_code" -ge 200 ] && [ "$http_code" -lt 300 ]; then
        UPDATED=$((UPDATED + 1))
        echo "[${repo}#${pr}] ${lines_changed} lines (+${additions}/-${deletions})"
    else
        ERRORS=$((ERRORS + 1))
        echo "[${repo}#${pr}] ERROR: PATCH failed (HTTP ${http_code})"
    fi
done

echo ""
echo "=== Complete ==="
echo "Updated: ${UPDATED}"
echo "Skipped (0 lines): ${SKIPPED}"
echo "Errors: ${ERRORS}"
