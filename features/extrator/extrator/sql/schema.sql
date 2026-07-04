CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    completed_at TEXT,
    error        TEXT,
    outputs_json TEXT NOT NULL,
    summary_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id            TEXT PRIMARY KEY,
    source_path       TEXT NOT NULL,
    source_type       TEXT NOT NULL,
    file_hash         TEXT NOT NULL,
    config_hash       TEXT NOT NULL,
    status            TEXT NOT NULL,
    output_paths_json TEXT NOT NULL,
    metadata_json     TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id          TEXT PRIMARY KEY,
    doc_id            TEXT NOT NULL,
    chunk_hash        TEXT NOT NULL,
    text_ref          TEXT NOT NULL,
    token_count       INTEGER NOT NULL,
    source_type       TEXT NOT NULL,
    page_start        INTEGER,
    page_end          INTEGER,
    heading_path_json TEXT NOT NULL,
    embedding_policy  TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tables (
    table_id    TEXT PRIMARY KEY,
    doc_id      TEXT NOT NULL,
    name        TEXT NOT NULL,
    rows        INTEGER NOT NULL,
    columns     INTEGER NOT NULL,
    output_path TEXT NOT NULL,
    summary     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversions (
    conversion_id TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    input_path    TEXT NOT NULL,
    output_format TEXT NOT NULL,
    output_path   TEXT NOT NULL,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(file_hash, config_hash);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_tables_doc ON tables(doc_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
