CREATE TABLE IF NOT EXISTS mails (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    thread_key    TEXT,
    issue_id      INTEGER,
    from_addr     TEXT,
    from_name     TEXT,
    subject       TEXT,
    received_at   TEXT,
    body          TEXT,
    translation   TEXT,
    lang          TEXT,
    masked_count  INTEGER DEFAULT 0,
    truncated     INTEGER DEFAULT 0,
    attachments   TEXT,
    auth          TEXT,
    in_spam       INTEGER DEFAULT 0,
    held_reason   TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (source, source_id)
);

CREATE TABLE IF NOT EXISTS issues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_code   TEXT NOT NULL,
    contact_addr    TEXT,
    contact_name    TEXT,
    thread_key      TEXT,
    subject         TEXT,
    title           TEXT,
    summary         TEXT,
    detail_where    TEXT,
    detail_when     TEXT,
    detail_impact   TEXT,
    translation     TEXT,
    lang            TEXT,
    category        TEXT,
    urgency         TEXT NOT NULL DEFAULT 'normal',
    urgency_rule    TEXT,
    urgency_model   TEXT,
    urgency_reason  TEXT,
    urgency_by      TEXT DEFAULT 'rule',
    rule_hits       TEXT,
    model_status    TEXT NOT NULL DEFAULT 'pending',
    model_name      TEXT,
    prompt_ver      TEXT,
    injection       INTEGER DEFAULT 0,
    not_new         INTEGER DEFAULT 0,
    issue_count     INTEGER DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'open',
    ball            TEXT NOT NULL DEFAULT 'us',
    ball_since      TEXT,
    assignee        TEXT,
    after_hours     INTEGER DEFAULT 0,
    late            INTEGER DEFAULT 0,
    alerted_at      TEXT,
    acked_at        TEXT,
    acked_by        TEXT,
    reack_count     INTEGER DEFAULT 0,
    last_alert_at   TEXT,
    opened_at       TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    closed_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_issues_status ON issues (status, urgency);
CREATE INDEX IF NOT EXISTS idx_issues_thread ON issues (thread_key);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id  INTEGER NOT NULL,
    at        TEXT NOT NULL,
    actor     TEXT,
    kind      TEXT NOT NULL,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_issue ON events (issue_id);

CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    mail_id    INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'queued',
    attempts   INTEGER DEFAULT 0,
    run_after  TEXT,
    error      TEXT,
    created_at TEXT NOT NULL,
    done_at    TEXT
);

CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id    INTEGER,
    kind        TEXT NOT NULL,
    recipient   TEXT,
    chat_id     TEXT,
    text        TEXT NOT NULL,
    silent      INTEGER DEFAULT 0,
    status      TEXT NOT NULL,
    tg_message_id TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_code TEXT NOT NULL,
    name          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deadlines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    item_code  TEXT,
    stage      TEXT,
    title      TEXT NOT NULL,
    due_date   TEXT,
    done_at    TEXT,
    owner      TEXT,
    memo       TEXT,
    notified   TEXT DEFAULT '',
    sort       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_deadlines_due ON deadlines (due_date);

CREATE TABLE IF NOT EXISTS news (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    title        TEXT NOT NULL,
    link         TEXT NOT NULL UNIQUE,
    published_at TEXT,
    fetched_at   TEXT NOT NULL,
    matched      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_news_pub ON news (published_at);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
