CREATE TABLE IF NOT EXISTS login_limits (
    client_key TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL,
    started_at INTEGER NOT NULL
);
