# AI Code Review

Automated code review for Crown Gas and Power GitHub repositories using Claude (Anthropic API). Runs in Jenkins on a polling schedule, reviews new PRs across all repos in the org.

## How it works

1. Jenkins polls every 5 minutes
2. Scans all repos in the `crowngasandpower` org for open PRs
3. Skips PRs that have already been reviewed (checks for bot marker in existing reviews)
4. Skips PRs that were last updated before March 1st 2026 (ignores old backlog)
5. For each new/updated PR: fetches the diff, sends to Claude, posts review comments
6. If blocking issues found, PR is marked "Changes Requested"
7. If a PR is updated after a review, it gets re-reviewed automatically

No webhooks needed — works behind firewalls.

## What it reviews

Based on Crown's [Engineering Commandments](https://crowngasandpower-team-delivery.atlassian.net/wiki/spaces/CAT/pages/102334485):

**Blocking (must fix):**
- Security vulnerabilities (SQL injection, XSS, command injection)
- Hardcoded secrets or credentials
- Data loss risks (destructive migrations)
- Missing authentication
- `env()` calls in app/ code (breaks config cache)

**Warnings (should fix):**
- Hardcoded filesystem paths (`/mnt/genus/` etc.)
- `shell_exec()` without error checking
- N+1 query patterns
- Missing error handling on external calls

**Suggestions:**
- Code style, naming, Laravel best practices
- Commented-out code that should be removed

## Setup

### 1. Jenkins credentials

Add two secret text credentials in Jenkins:

| Credential ID | Type | Value |
|---|---|---|
| `anthropic-api-key` | Secret text | Your Anthropic API key (`sk-ant-api03-...`) |
| `github-token` | Secret text | GitHub PAT with `repo` scope |

### 2. Jenkins job

Create a Pipeline job:
- **Pipeline script from SCM**: Git
- **Repository URL**: `https://github.com/crowngasandpower/crown-engineering-mcp.git`
- **Branch**: `*/main`
- **Script Path**: `review/Jenkinsfile`

That's it. The cron trigger polls every 5 minutes automatically. No webhooks, no per-repo configuration.

## Configuration

| Variable | Where | Default | Description |
|---|---|---|---|
| `REVIEW_SINCE` | Jenkinsfile | `2026-03-01T00:00:00Z` | Only review PRs updated after this date |
| `MAX_REVIEWS_PER_RUN` | poll-and-review.sh | `10` | Cap reviews per poll cycle to control API costs |
| `CLAUDE_MODEL` | Jenkinsfile | `claude-sonnet-4-6` | Claude model to use |
| `BOT_MARKER` | Jenkinsfile | `AI Code Review (Claude)` | String used to detect our own reviews |

## Cost

- **Claude Sonnet**: ~$0.01-0.03 per typical PR review
- **Max 10 reviews per 5-minute poll** = max ~$0.30 per cycle in burst
- Steady state (a few new PRs per day): ~$0.10-0.50/day
- **Monthly estimate**: $3-15/month depending on PR volume

## How it handles the backlog

On first run, the bot scans all open PRs but only reviews those updated after March 1st 2026. It processes max 10 per run, so it works through the backlog over several poll cycles without hammering the APIs. Once caught up, it only reviews genuinely new PRs.

## Files

```
review/
├── Jenkinsfile                 # Pipeline: cron trigger, credentials
├── api/
│   ├── app.py                  # FastAPI: POST /review for engineer pre-push reviews
│   ├── Dockerfile
│   └── requirements.txt
├── scripts/
│   ├── poll-and-review.sh      # Scans org for unreviewed PRs (Jenkins entry)
│   ├── review-pr.sh            # Reviews a single PR (diff → Claude → GitHub)
│   ├── review-local.sh         # Engineer-side pre-push helper
│   ├── github-utils.sh         # Shared GitHub API rate-limit helpers
│   ├── backfill.sh             # One-shot backfill of historical reviews
│   └── backfill-lines.sh       # Backfill lines_changed for old reviews
└── README.md                   # this file
```

## Customisation

To adjust the review rules, edit the `SYSTEM_PROMPT` in `scripts/review-pr.sh`. To change which repos are scanned, the poll script currently scans all repos in the org — to limit it, add a repo allowlist/blocklist in `poll-and-review.sh`.

**Important:** the system prompt is duplicated in `scripts/review-pr.sh`, `scripts/review-local.sh`, and `api/app.py`. Update all three together.
