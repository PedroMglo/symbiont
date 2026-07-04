CREATE TABLE IF NOT EXISTS files (
    source_id       TEXT NOT NULL DEFAULT '',
    source_type     TEXT NOT NULL DEFAULT '',
    path            TEXT NOT NULL,
    repo            TEXT NOT NULL,
    mtime           REAL NOT NULL,
    size            INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'indexed',
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    config_version  TEXT NOT NULL DEFAULT '',
    last_indexed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source_id, path)
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id      TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL DEFAULT '',
    source_type   TEXT NOT NULL DEFAULT '',
    file_path     TEXT NOT NULL,
    repo          TEXT NOT NULL,
    chunk_hash    TEXT NOT NULL,
    vector_status TEXT NOT NULL DEFAULT 'pending',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (source_id, file_path) REFERENCES files(source_id, path) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_path);
CREATE INDEX IF NOT EXISTS idx_chunks_source_file ON chunks(source_id, file_path);
CREATE INDEX IF NOT EXISTS idx_chunks_repo ON chunks(repo);
CREATE INDEX IF NOT EXISTS idx_chunks_status ON chunks(vector_status);
CREATE INDEX IF NOT EXISTS idx_files_repo ON files(repo);
CREATE INDEX IF NOT EXISTS idx_files_source ON files(source_id);
