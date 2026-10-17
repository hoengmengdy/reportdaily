CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'editor', 'viewer')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 200),
    url TEXT NOT NULL,
    profile_id TEXT NOT NULL UNIQUE,
    target INTEGER NOT NULL CHECK(target BETWEEN 0 AND 1000000),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS seed_registry (
    seed_key TEXT PRIMARY KEY,
    page_id INTEGER NOT NULL REFERENCES pages(id)
);
CREATE TABLE IF NOT EXISTS reports (
    date TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_by INTEGER REFERENCES users(id),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS report_rows (
    report_date TEXT NOT NULL REFERENCES reports(date),
    page_id INTEGER NOT NULL REFERENCES pages(id),
    page_name TEXT NOT NULL,
    profile_url TEXT NOT NULL,
    target INTEGER NOT NULL CHECK(target BETWEEN 0 AND 1000000),
    actual INTEGER NOT NULL DEFAULT 0 CHECK(actual BETWEEN 0 AND 1000000),
    PRIMARY KEY(report_date, page_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    csrf_token TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
