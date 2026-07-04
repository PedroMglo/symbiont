CREATE TABLE IF NOT EXISTS embedding_cache (
    text_sha256 TEXT NOT NULL,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (text_sha256, model)
);
