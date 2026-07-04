PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = {busy_timeout_ms};

CREATE TABLE IF NOT EXISTS llm_call_log (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id              TEXT NOT NULL UNIQUE,
    session_id              TEXT,
    timestamp               REAL NOT NULL,
    date                    TEXT NOT NULL,
    hour                    INTEGER NOT NULL,
    weekday                 INTEGER NOT NULL,
    entrypoint              TEXT NOT NULL DEFAULT 'api',
    model                   TEXT NOT NULL,
    backend                 TEXT NOT NULL,
    intent                  TEXT,
    complexity              TEXT,
    requested_model         TEXT,
    fallback_used           BOOLEAN NOT NULL DEFAULT 0,
    fallback_reason         TEXT,
    blocked_backends        TEXT,
    privacy_mode            BOOLEAN NOT NULL DEFAULT 0,
    decision_reason         TEXT,
    prompt_tokens           INTEGER,
    completion_tokens       INTEGER,
    total_tokens            INTEGER,
    usage_source            TEXT NOT NULL DEFAULT 'missing',
    stream                  BOOLEAN NOT NULL DEFAULT 0,
    chunks_count            INTEGER DEFAULT 0,
    latency_ms              REAL NOT NULL,
    first_token_latency_ms  REAL,
    context_latency_ms      REAL,
    llm_latency_ms          REAL,
    router_latency_ms       REAL,
    context_build_latency_ms REAL,
    model_load_latency_ms   REAL,
    prompt_eval_latency_ms  REAL,
    generation_latency_ms   REAL,
    total_latency_ms        REAL,
    cold_start              BOOLEAN NOT NULL DEFAULT 0,
    prompt_tokens_per_second REAL,
    generation_tokens_per_second REAL,
    total_tokens_per_second REAL,
    ollama_total_duration   INTEGER,
    ollama_load_duration    INTEGER,
    ollama_prompt_eval_count INTEGER,
    ollama_prompt_eval_duration INTEGER,
    ollama_eval_count       INTEGER,
    ollama_eval_duration    INTEGER,
    profile_key             TEXT,
    query_length            INTEGER,
    response_length         INTEGER,
    query_hash              TEXT,
    prompt_preview          TEXT,
    response_preview        TEXT,
    rag_used                BOOLEAN NOT NULL DEFAULT 0,
    graph_used              BOOLEAN NOT NULL DEFAULT 0,
    tools_used              TEXT,
    agentic                 BOOLEAN NOT NULL DEFAULT 0,
    iterations              INTEGER DEFAULT 0,
    success                 BOOLEAN NOT NULL DEFAULT 1,
    error_type              TEXT,
    error_message           TEXT
);

CREATE INDEX IF NOT EXISTS idx_llm_timestamp ON llm_call_log (timestamp);
CREATE INDEX IF NOT EXISTS idx_llm_date ON llm_call_log (date);
CREATE INDEX IF NOT EXISTS idx_llm_model ON llm_call_log (model);
CREATE INDEX IF NOT EXISTS idx_llm_backend ON llm_call_log (backend);
CREATE INDEX IF NOT EXISTS idx_llm_session ON llm_call_log (session_id);
CREATE INDEX IF NOT EXISTS idx_llm_intent ON llm_call_log (intent);
CREATE INDEX IF NOT EXISTS idx_llm_weekday_hour ON llm_call_log (weekday, hour);

CREATE TABLE IF NOT EXISTS backend_health_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        REAL NOT NULL,
    backend          TEXT NOT NULL,
    status           TEXT NOT NULL,
    latency_ms       REAL,
    models_detected  INTEGER DEFAULT 0,
    error            TEXT
);

CREATE INDEX IF NOT EXISTS idx_bh_timestamp ON backend_health_log (timestamp);
CREATE INDEX IF NOT EXISTS idx_bh_backend ON backend_health_log (backend);

CREATE TABLE IF NOT EXISTS system_resources (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp            REAL NOT NULL,
    gpu_name             TEXT,
    gpu_vram_total_mb    INTEGER,
    gpu_vram_used_mb     INTEGER,
    gpu_vram_free_mb     INTEGER,
    gpu_utilization_pct  REAL,
    gpu_temperature_c    REAL,
    gpu_power_w          REAL,
    ram_total_mb         INTEGER,
    ram_used_mb          INTEGER,
    ram_available_mb     INTEGER,
    ram_percent          REAL,
    swap_total_mb        INTEGER,
    swap_used_mb         INTEGER,
    cpu_count            INTEGER,
    cpu_percent          REAL,
    ollama_models_loaded INTEGER DEFAULT 0,
    ollama_vram_used_mb  INTEGER DEFAULT 0,
    models_loaded_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_sr_timestamp ON system_resources (timestamp);
