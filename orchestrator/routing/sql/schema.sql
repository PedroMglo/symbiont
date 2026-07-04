PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 3000;

CREATE TABLE IF NOT EXISTS routing_decisions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id          TEXT NOT NULL UNIQUE,
    session_id          TEXT,
    timestamp           REAL NOT NULL,
    query               TEXT NOT NULL,
    query_hash          TEXT,
    intent              TEXT,
    complexity          TEXT,
    action              TEXT NOT NULL,
    reasoning           TEXT,
    agents_planned      TEXT,
    agents_executed     TEXT,
    routing_model       TEXT,
    routing_latency_ms  REAL,
    execution_latency_ms REAL,
    synthesis_latency_ms REAL,
    total_latency_ms    REAL,
    total_tokens        INTEGER DEFAULT 0,
    success             BOOLEAN NOT NULL DEFAULT 1,
    fallback_used       BOOLEAN NOT NULL DEFAULT 0,
    critic_score        REAL,
    critic_acceptable   BOOLEAN,
    user_rating         INTEGER,
    user_feedback       TEXT,
    response_preview    TEXT,
    created_at          REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rd_timestamp ON routing_decisions (timestamp);
CREATE INDEX IF NOT EXISTS idx_rd_intent ON routing_decisions (intent);
CREATE INDEX IF NOT EXISTS idx_rd_complexity ON routing_decisions (complexity);
CREATE INDEX IF NOT EXISTS idx_rd_action ON routing_decisions (action);
CREATE INDEX IF NOT EXISTS idx_rd_session ON routing_decisions (session_id);
CREATE INDEX IF NOT EXISTS idx_rd_rating ON routing_decisions (user_rating);
CREATE INDEX IF NOT EXISTS idx_rd_success ON routing_decisions (success);
