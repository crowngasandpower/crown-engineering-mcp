#!/bin/bash
# Backfill review data from existing AI code reviews on GitHub.
#
# ONLY processes PRs that already have our bot review marker.
# Parses the review body and upserts stats to PostgreSQL via PostgREST.
#
# Required:
#   GITHUB_TOKEN  — GitHub PAT with repo scope
#   GITHUB_ORG    — GitHub org (crowngasandpower)
#   POSTGREST_URL — PostgREST URL (e.g. http://192.168.173.140:9505)
#
# Optional:
#   PUSHGATEWAY_URL — also restore Pushgateway data
#   PR_SINCE        — only process PRs created after this (default: 2025-04-01)

set -uo pipefail

GITHUB_API="https://api.github.com"
BOT_MARKER="AI Code Review (Claude)"
PR_SINCE="${PR_SINCE:-2025-04-01T00:00:00Z}"
PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-}"

source "$(dirname "$0")/github-utils.sh"

PUSHED=0
SKIPPED=0
ERRORS=0

echo "=== Backfill from Existing Reviews ==="
echo "Org: ${GITHUB_ORG}"
echo "PostgREST: ${POSTGREST_URL}"
echo "Pushgateway: ${PUSHGATEWAY_URL:-not set}"
echo "PRs created after: ${PR_SINCE}"
echo ""

# --- Get all repos ---
echo "Fetching repos..."
page=1
ALL_REPOS=""
while true; do
    repos_json=$(github_api GET "/orgs/${GITHUB_ORG}/repos?per_page=100&page=${page}&type=all" 2>/dev/null || echo "[]")
    repos=$(echo "$repos_json" | jq -r '.[].name // empty' 2>/dev/null || true)
    [ -z "$repos" ] && break
    ALL_REPOS="${ALL_REPOS}
${repos}"
    page=$((page + 1))
done

ALL_REPOS=$(echo "$ALL_REPOS" | sed '/^$/d')
REPO_COUNT=$(echo "$ALL_REPOS" | wc -l | tr -d ' ')
echo "Found ${REPO_COUNT} repos"
echo ""

# --- Process each repo ---
for repo in $ALL_REPOS; do
    [ -z "$repo" ] && continue

    # Fetch ALL PRs (open + closed), sorted newest first, paginate
    pr_page=1
    while true; do
        prs_json=$(github_api GET "/repos/${GITHUB_ORG}/${repo}/pulls?state=all&per_page=100&page=${pr_page}&sort=created&direction=desc" || true)

        pr_data=$(echo "$prs_json" | jq -r '.[]? | "\(.number)|\(.created_at)"' 2>/dev/null || true)

        [ -z "$pr_data" ] && break

        hit_cutoff=false

        for entry in $pr_data; do
            [ -z "$entry" ] && continue

            pr_number=$(echo "$entry" | cut -d'|' -f1)
            pr_created_at=$(echo "$entry" | cut -d'|' -f2)

            case "$pr_number" in
                ''|*[!0-9]*) continue ;;
            esac

            # Stop this repo if we've gone past the cutoff (sorted newest first)
            if [ "$pr_created_at" \< "$PR_SINCE" ]; then
                hit_cutoff=true
                break
            fi

            # Get reviews — skip if no bot marker
            reviews_json=$(github_api GET "/repos/${GITHUB_ORG}/${repo}/pulls/${pr_number}/reviews?per_page=100" || true)

            if ! echo "$reviews_json" | grep -q "${BOT_MARKER}"; then
                continue
            fi

            # Get PR metadata
            pr_meta=$(github_api GET "/repos/${GITHUB_ORG}/${repo}/pulls/${pr_number}" || true)
            engineer=$(echo "$pr_meta" | jq -r '.user.login // "unknown"')
            pr_title=$(echo "$pr_meta" | jq -r '.title // ""')
            head_sha=$(echo "$pr_meta" | jq -r '.head.sha // ""')
            pr_additions=$(echo "$pr_meta" | jq -r '.additions // 0')
            pr_deletions=$(echo "$pr_meta" | jq -r '.deletions // 0')
            lines_changed=$((pr_additions + pr_deletions))

            # Extract last review body
            review_body=$(echo "$reviews_json" | jq -r '
                [.[] | select(.body | contains("AI Code Review (Claude)"))]
                | last | .body // empty')

            if [ -z "$review_body" ]; then
                continue
            fi

            # Parse issue counts from emoji markers
            high=$(echo "$review_body" | grep -c ':red_circle:' 2>/dev/null | tr -d '[:space:]' || true)
            medium=$(echo "$review_body" | grep -c ':orange_circle:' 2>/dev/null | tr -d '[:space:]' || true)
            low=$(echo "$review_body" | grep -c ':white_circle:' 2>/dev/null | tr -d '[:space:]' || true)
            high=${high:-0}; medium=${medium:-0}; low=${low:-0}
            total=$((high + medium + low))

            # Upsert to PostgreSQL via PostgREST
            body_tmp=$(mktemp)
            echo "$review_body" > "$body_tmp"

            payload=$(jq -n \
                --arg repo "$repo" \
                --argjson pr "$pr_number" \
                --arg engineer "$engineer" \
                --arg pr_title "$pr_title" \
                --arg pr_created_at "$pr_created_at" \
                --arg head_sha "$head_sha" \
                --argjson high "$high" \
                --argjson medium "$medium" \
                --argjson low "$low" \
                --argjson total "$total" \
                --argjson lines_changed "$lines_changed" \
                --rawfile review_body "$body_tmp" \
                '{repo: $repo, pr: $pr, engineer: $engineer, pr_title: $pr_title,
                  pr_created_at: $pr_created_at, head_sha: $head_sha,
                  high: $high, medium: $medium, low: $low, total: $total,
                  lines_changed: $lines_changed, review_body: $review_body}')

            rm -f "$body_tmp"

            http_code=$(curl -s -o /dev/null -w "%{http_code}" \
                -X POST "${POSTGREST_URL}/reviews" \
                -H "Content-Type: application/json" \
                -H "Prefer: resolution=merge-duplicates" \
                -d "$payload")

            if [ "$http_code" -ge 200 ] && [ "$http_code" -lt 300 ]; then
                PUSHED=$((PUSHED + 1))
                echo "[${repo}#${pr_number}] ${engineer}: ${high}h/${medium}m/${low}l (opened ${pr_created_at})"
            else
                ERRORS=$((ERRORS + 1))
                echo "[${repo}#${pr_number}] ERROR: DB upsert failed (HTTP ${http_code})"
            fi

            # Push to Pushgateway if configured
            if [ -n "$PUSHGATEWAY_URL" ]; then
                cat <<METRICS | curl -s --data-binary @- "${PUSHGATEWAY_URL}/metrics/job/ai_code_review/engineer/${engineer}/repo/${repo}" > /dev/null
# HELP code_review_prs_total Total PRs reviewed
# TYPE code_review_prs_total counter
code_review_prs_total 1
# HELP code_review_issues_total Total issues found by severity
# TYPE code_review_issues_total counter
code_review_issues_total{severity="high"} ${high}
code_review_issues_total{severity="medium"} ${medium}
code_review_issues_total{severity="low"} ${low}
METRICS
            fi

            sleep 0.5
        done

        if [ "$hit_cutoff" = true ]; then
            break
        fi

        pr_page=$((pr_page + 1))
    done
done

echo ""
echo "=== Backfill Complete ==="
echo "Pushed: ${PUSHED}"
echo "Errors: ${ERRORS}"
