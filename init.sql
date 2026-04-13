CREATE TABLE IF NOT EXISTS reviews (
    repo        TEXT NOT NULL,
    pr          INTEGER NOT NULL,
    engineer    TEXT NOT NULL,
    pr_title    TEXT NOT NULL DEFAULT '',
    pr_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    head_sha    TEXT NOT NULL DEFAULT '',
    high        INTEGER NOT NULL DEFAULT 0,
    medium      INTEGER NOT NULL DEFAULT 0,
    low         INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    lines_changed INTEGER NOT NULL DEFAULT 0,
    review_body TEXT NOT NULL DEFAULT '',
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (repo, pr)
);

-- Index for dashboard queries
CREATE INDEX IF NOT EXISTS idx_reviews_engineer ON reviews (engineer);
CREATE INDEX IF NOT EXISTS idx_reviews_pr_created_at ON reviews (pr_created_at);
