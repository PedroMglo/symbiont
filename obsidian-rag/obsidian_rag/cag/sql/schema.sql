CREATE TABLE IF NOT EXISTS packs (
    pack_type       TEXT NOT NULL,
    scope           TEXT NOT NULL DEFAULT 'global',
    content         TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    source_hash     TEXT NOT NULL DEFAULT '',
    config_version  TEXT NOT NULL DEFAULT '',
    model_version   TEXT NOT NULL DEFAULT '',
    ttl_seconds     INTEGER NOT NULL DEFAULT 3600,
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (pack_type, scope)
);

CREATE TABLE IF NOT EXISTS response_cache (
    query_hash      TEXT PRIMARY KEY,
    response        TEXT NOT NULL,
    context_hash    TEXT NOT NULL,
    model           TEXT NOT NULL,
    config_version  TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    ttl_seconds     INTEGER NOT NULL DEFAULT 600
);
