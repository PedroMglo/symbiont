CREATE TABLE IF NOT EXISTS extraction_cache (
    file_hash         TEXT NOT NULL,
    model_name        TEXT NOT NULL,
    schema_version    TEXT NOT NULL,
    graphify_version  TEXT NOT NULL,
    graph_fragment    TEXT NOT NULL,
    extracted_at      REAL NOT NULL,
    PRIMARY KEY (file_hash, model_name, schema_version, graphify_version)
);

CREATE INDEX IF NOT EXISTS idx_cache_hash ON extraction_cache(file_hash);
