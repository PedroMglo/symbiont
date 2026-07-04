CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (session_id, created_at)
);
CREATE INDEX IF NOT EXISTS idx_sessions_access ON sessions (created_at);
