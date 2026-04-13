#!/bin/bash
# AI Code Review — Poll and review PRs
#
# Two-phase approach:
#   Phase 1: Find open PRs updated today, review/re-review any with new commits
#            (compares head SHA against the SHA stored in PostgreSQL)
#   Phase 2: Backfill one historical day per run, working backwards
#
# Uses GitHub Search API — one call finds all PRs for a given day
# across the entire org (instead of querying each repo individually).
#
# Required environment variables:
#   ANTHROPIC_API_KEY, GITHUB_TOKEN, GITHUB_ORG, CLAUDE_MODEL, BOT_MARKER
#
# Optional:
#   POSTGREST_URL       — PostgREST URL for SHA lookups and review storage
#   STATE_DIR           — where to persist backfill state (default: /tmp/ai-code-review)
#   MAX_REVIEWS_PER_RUN — cap total reviews per run (default: 100)
#   PUSHGATEWAY_URL     — push metrics to Pushgateway

set -uo pipefail

GITHUB_API="https://api.github.com"
STATE_DIR="${STATE_DIR:-/tmp/ai-code-review}"
MAX_REVIEWS_PER_RUN="${MAX_REVIEWS_PER_RUN:-100}"
POSTGREST_URL="${POSTGREST_URL:-}"

source "$(dirname "$0")/github-utils.sh"

mkdir -p "$STATE_DIR"

REVIEWED=0
SKIPPED=0
ERRORS=0

echo "=== AI Code Review Poll ==="
echo "Org: ${GITHUB_ORG}"
echo "State dir: ${STATE_DIR}"
echo "Max reviews this run: ${MAX_REVIEWS_PER_RUN}"
echo ""

# --- Helper: get the stored head SHA for a PR from PostgreSQL ---
get_stored_sha() {
    local repo="$1"
    local pr_number="$2"

    if [ -z "$POSTGREST_URL" ]; then
        echo ""
        return
    fi

    curl -s -G "${POSTGREST_URL}/reviews" \
        --data-urlencode "repo=eq.${repo}" \
        --data-urlencode "pr=eq.${pr_number}" \
        --data-urlencode "select=head_sha" \
        -H "Accept: application/json" \
        | jq -r '.[0].head_sha // empty' 2>/dev/null || true
}

# --- Helper: review a single PR if needed ---
# Reviews if: (a) no record in DB, or (b) head SHA changed since last review
review_pr_if_needed() {
    local repo="$1"
    local pr_number="$2"
    local current_head_sha="${3:-}"

    # If we don't have the head SHA yet, fetch the PR to get it
    if [ -z "$current_head_sha" ]; then
        current_head_sha=$(github_api GET "/repos/${GITHUB_ORG}/${repo}/pulls/${pr_number}" 2>/dev/null \
            | jq -r '.head.sha // empty' 2>/dev/null || true)
    fi

    # Check PostgreSQL for stored SHA (fast path, no GitHub API call)
    local stored_sha
    stored_sha=$(get_stored_sha "$repo" "$pr_number")
    if [ -n "$stored_sha" ] && [ "$stored_sha" = "$current_head_sha" ]; then
        SKIPPED=$((SKIPPED + 1))
        return 0
    fi

    if [ -n "$stored_sha" ]; then
        echo "  [${repo}#${pr_number}] New commits (was ${stored_sha:0:7}, now ${current_head_sha:0:7}), re-reviewing..."
    else
        echo "  [${repo}#${pr_number}] Reviewing..."
    fi

    export GITHUB_REPO="$repo"
    export PR_NUMBER="$pr_number"

    if bash "$(dirname "$0")/review-pr.sh" 2>&1; then
        REVIEWED=$((REVIEWED + 1))
        echo "  [${repo}#${pr_number}] Review posted"
    else
        ERRORS=$((ERRORS + 1))
        echo "  [${repo}#${pr_number}] ERROR: Review failed"
    fi

    sleep 2
}

# --- Helper: find and review all PRs created on a specific date ---
review_prs_created_on_date() {
    local target_date="$1"
    local label="$2"

    echo "=== ${label}: PRs created ${target_date} ==="

    local search_result
    search_result=$(github_api GET "/search/issues?q=org:${GITHUB_ORG}+is:pr+created:${target_date}&per_page=100&sort=created&order=desc" 2>/dev/null || echo "")

    local total
    total=$(echo "$search_result" | jq '.total_count // 0' 2>/dev/null || echo "0")
    echo "Found ${total} PRs"

    if [ "$total" -eq 0 ] || [ "$total" = "null" ]; then
        return
    fi

    local pr_list
    pr_list=$(echo "$search_result" | jq -r '.items[]? | "\(.repository_url | split("/") | .[-1])|\(.number)"' 2>/dev/null || true)

    for entry in $pr_list; do
        [ -z "$entry" ] && continue
        [ "$REVIEWED" -ge "$MAX_REVIEWS_PER_RUN" ] && break

        local repo=$(echo "$entry" | cut -d'|' -f1)
        local pr_number=$(echo "$entry" | cut -d'|' -f2)

        case "$pr_number" in ''|*[!0-9]*) continue ;; esac

        review_pr_if_needed "$repo" "$pr_number"
    done
}

# --- Helper: find open PRs with activity today and re-review if new commits ---
review_updated_prs_today() {
    echo "=== Phase 1: Open PRs with activity today ==="

    local search_result
    search_result=$(github_api GET "/search/issues?q=org:${GITHUB_ORG}+is:pr+is:open+updated:>=${TODAY}&per_page=100&sort=updated&order=desc" 2>/dev/null || echo "")

    local total
    total=$(echo "$search_result" | jq '.total_count // 0' 2>/dev/null || echo "0")
    echo "Found ${total} open PRs updated today"

    if [ "$total" -eq 0 ] || [ "$total" = "null" ]; then
        return
    fi

    local pr_list
    pr_list=$(echo "$search_result" | jq -r '.items[]? | "\(.repository_url | split("/") | .[-1])|\(.number)"' 2>/dev/null || true)

    for entry in $pr_list; do
        [ -z "$entry" ] && continue
        [ "$REVIEWED" -ge "$MAX_REVIEWS_PER_RUN" ] && break

        local repo=$(echo "$entry" | cut -d'|' -f1)
        local pr_number=$(echo "$entry" | cut -d'|' -f2)

        case "$pr_number" in ''|*[!0-9]*) continue ;; esac

        review_pr_if_needed "$repo" "$pr_number"
    done
}

# ============================================================
# PHASE 1: Review open PRs with activity today
# Catches both new PRs and existing PRs with new commits.
# Uses head SHA comparison to skip PRs where only
# comments/labels changed (no new code).
# ============================================================
TODAY=$(date +%Y-%m-%d)
review_updated_prs_today
echo "Phase 1 complete: ${REVIEWED} reviewed, ${SKIPPED} skipped"
echo ""

# ============================================================
# PHASE 2: Backfill one historical day (progressive)
# ============================================================
BACKFILL_STATE_FILE="${STATE_DIR}/backfill_date"
OLDEST_REPO_FILE="${STATE_DIR}/oldest_repo_date"

# Cache the oldest repo date so we only fetch it once
if [ -f "$OLDEST_REPO_FILE" ]; then
    OLDEST_REPO_DATE=$(cat "$OLDEST_REPO_FILE")
else
    echo "Fetching oldest repo date..."
    repos_json=$(github_api GET "/orgs/${GITHUB_ORG}/repos?per_page=100&sort=created&direction=asc&type=all" 2>/dev/null || echo "[]")
    OLDEST_REPO_DATE=$(echo "$repos_json" | jq -r '.[0]?.created_at // empty' 2>/dev/null | cut -dT -f1 || true)
    OLDEST_REPO_DATE=${OLDEST_REPO_DATE:-2020-01-01}
    echo "$OLDEST_REPO_DATE" > "$OLDEST_REPO_FILE"
    echo "Oldest repo created: ${OLDEST_REPO_DATE}"
fi

if [ -f "$BACKFILL_STATE_FILE" ]; then
    BACKFILL_DATE=$(cat "$BACKFILL_STATE_FILE")
else
    BACKFILL_DATE=$(date -d "yesterday" +%Y-%m-%d 2>/dev/null || date -v-1d +%Y-%m-%d 2>/dev/null)
fi

if [ "$BACKFILL_DATE" = "complete" ]; then
    echo "=== Phase 2: Backfill complete — nothing to do ==="
elif [ "$BACKFILL_DATE" \< "$OLDEST_REPO_DATE" ]; then
    echo "=== Phase 2: Backfill reached oldest repo (${OLDEST_REPO_DATE}) — complete! ==="
    echo "complete" > "$BACKFILL_STATE_FILE"
else
    if [ "$REVIEWED" -lt "$MAX_REVIEWS_PER_RUN" ]; then
        review_prs_created_on_date "$BACKFILL_DATE" "Phase 2"

        # Move to previous day for next run
        NEXT_DATE=$(date -d "${BACKFILL_DATE} - 1 day" +%Y-%m-%d 2>/dev/null || date -j -v-1d -f "%Y-%m-%d" "$BACKFILL_DATE" +%Y-%m-%d 2>/dev/null)
        echo "$NEXT_DATE" > "$BACKFILL_STATE_FILE"
        echo "Next backfill date: ${NEXT_DATE}"
    else
        echo "=== Phase 2: Skipped (hit review limit in Phase 1) ==="
    fi
fi

echo ""
echo "=== Poll Complete ==="
echo "Reviewed: ${REVIEWED}"
echo "Skipped (already reviewed): ${SKIPPED}"
echo "Errors: ${ERRORS}"

exit 0
