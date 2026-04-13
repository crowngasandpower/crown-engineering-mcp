#!/bin/bash
# Shared GitHub API utilities — rate limit handling
#
# Source this file in other scripts:
#   source "$(dirname "$0")/github-utils.sh"

RATE_CHECK_COUNTER=0
RATE_LIMIT_REMAINING=100
GITHUB_API="${GITHUB_API:-https://api.github.com}"
_GA_TMPFILE=$(mktemp)

# Make a GitHub API request with rate limit handling.
# Writes response to _GA_TMPFILE, echoes the content to stdout.
# Usage: github_api "GET" "/repos/org/repo/pulls"
github_api() {
    local method="$1"
    local endpoint="$2"
    shift 2
    local url="${GITHUB_API}${endpoint}"

    check_rate_limit

    local http_code
    http_code=$(curl -s -o "$_GA_TMPFILE" -w "%{http_code}" \
        -X "$method" \
        -H "Accept: application/vnd.github+json" \
        -H "Authorization: Bearer ${GITHUB_TOKEN}" \
        "$url" "$@")

    if [ "$http_code" = "403" ] && grep -q "rate limit" "$_GA_TMPFILE" 2>/dev/null; then
        echo "Rate limit hit — waiting for reset..." >&2
        wait_for_rate_limit_reset
        http_code=$(curl -s -o "$_GA_TMPFILE" -w "%{http_code}" \
            -X "$method" \
            -H "Accept: application/vnd.github+json" \
            -H "Authorization: Bearer ${GITHUB_TOKEN}" \
            "$url" "$@")
    fi

    cat "$_GA_TMPFILE"
}

check_rate_limit() {
    RATE_CHECK_COUNTER=$((RATE_CHECK_COUNTER + 1))

    if [ "$RATE_CHECK_COUNTER" -lt 50 ] && [ "$RATE_LIMIT_REMAINING" -gt 50 ]; then
        RATE_LIMIT_REMAINING=$((RATE_LIMIT_REMAINING - 1))
        return
    fi

    RATE_CHECK_COUNTER=0

    local rate_file=$(mktemp)
    curl -s -o "$rate_file" \
        -H "Authorization: Bearer ${GITHUB_TOKEN}" \
        "https://api.github.com/rate_limit" 2>/dev/null

    RATE_LIMIT_REMAINING=$(grep '"remaining"' "$rate_file" | head -1 | sed 's/[^0-9]//g')
    local reset_epoch=$(grep '"reset"' "$rate_file" | head -1 | sed 's/[^0-9]//g')
    rm -f "$rate_file"

    RATE_LIMIT_REMAINING=${RATE_LIMIT_REMAINING:-0}

    if [ "$RATE_LIMIT_REMAINING" -le 10 ]; then
        echo "Rate limit nearly exhausted (${RATE_LIMIT_REMAINING} remaining). Waiting for reset..." >&2
        wait_until_epoch "$reset_epoch"
        RATE_LIMIT_REMAINING=5000
    fi
}

wait_until_epoch() {
    local target_epoch="$1"
    local now=$(date +%s)
    local wait_seconds=$((target_epoch - now + 5))

    if [ "$wait_seconds" -gt 0 ]; then
        local wait_minutes=$((wait_seconds / 60))
        echo "Waiting ${wait_seconds}s (~${wait_minutes}m) for rate limit reset..." >&2
        sleep "$wait_seconds"
    fi
}

wait_for_rate_limit_reset() {
    local rate_file=$(mktemp)
    curl -s -o "$rate_file" \
        -H "Authorization: Bearer ${GITHUB_TOKEN}" \
        "https://api.github.com/rate_limit" 2>/dev/null
    local reset_epoch=$(grep '"reset"' "$rate_file" | head -1 | sed 's/[^0-9]//g')
    rm -f "$rate_file"
    wait_until_epoch "$reset_epoch"
}
