PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS signals (
    id               INTEGER PRIMARY KEY,
    company_id       INTEGER NOT NULL REFERENCES companies(id),
    signal_type      TEXT    NOT NULL,
    source           TEXT    NOT NULL,
    board_token      TEXT    NOT NULL,
    external_id      TEXT    NOT NULL,
    source_url       TEXT    NOT NULL,
    title            TEXT    NOT NULL,
    department       TEXT,
    matched_term     TEXT,
    seniority        TEXT,
    amount_usd       INTEGER,            -- money_raised signals only
    first_published  TEXT,
    first_seen_at    TEXT    NOT NULL,
    repost_of        INTEGER REFERENCES signals(id),
    delivery_status  TEXT    NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    next_retry_at    TEXT,
    UNIQUE (source, board_token, external_id, signal_type)
);

CREATE INDEX IF NOT EXISTS idx_signals_company ON signals(company_id);
CREATE INDEX IF NOT EXISTS idx_signals_status  ON signals(delivery_status, next_retry_at);

CREATE TABLE IF NOT EXISTS signal_deliveries (
    id            INTEGER PRIMARY KEY,
    signal_id     INTEGER NOT NULL REFERENCES signals(id),
    attempt_no    INTEGER NOT NULL,
    attempted_at  TEXT    NOT NULL,
    ok            INTEGER NOT NULL,
    status_code   INTEGER,
    error         TEXT,
    next_retry_at TEXT
);
