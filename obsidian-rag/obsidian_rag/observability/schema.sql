-- ClickHouse schema for obsidian-rag observability
-- Database: obsidian_rag
-- Auto-applied on first dispatcher connection

CREATE DATABASE IF NOT EXISTS obsidian_rag;

CREATE TABLE IF NOT EXISTS obsidian_rag.rag_requests (
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    event LowCardinality(String),
    request_id String,
    symbiont_request_id String DEFAULT '',
    endpoint LowCardinality(String) DEFAULT '',
    method LowCardinality(String) DEFAULT '',
    status_code UInt16 DEFAULT 0,
    latency_ms Float32 DEFAULT 0,
    caller_ip String DEFAULT '',
    success UInt8 DEFAULT 1,
    error_type LowCardinality(String) DEFAULT '',
    error_message String DEFAULT '',
    query_hash String DEFAULT '',
    query_length UInt32 DEFAULT 0
) ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, endpoint, request_id)
TTL date + INTERVAL 90 DAY;

CREATE TABLE IF NOT EXISTS obsidian_rag.rag_retrieval (
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    event LowCardinality(String),
    request_id String DEFAULT '',
    symbiont_request_id String DEFAULT '',
    route_mode LowCardinality(String) DEFAULT '',
    route_method LowCardinality(String) DEFAULT '',
    route_confidence Float32 DEFAULT 0,
    route_latency_ms Float32 DEFAULT 0,
    query_hash String DEFAULT '',
    query_length UInt32 DEFAULT 0,
    query_complexity LowCardinality(String) DEFAULT '',
    effective_top_k UInt16 DEFAULT 0,
    collection LowCardinality(String) DEFAULT '',
    results_count UInt16 DEFAULT 0,
    results_after_filter UInt16 DEFAULT 0,
    best_score Float32 DEFAULT 0,
    threshold_used Float32 DEFAULT 0,
    search_latency_ms Float32 DEFAULT 0,
    exact_removed UInt16 DEFAULT 0,
    semantic_removed UInt16 DEFAULT 0,
    reranker_used UInt8 DEFAULT 0,
    reranker_backend LowCardinality(String) DEFAULT '',
    candidates_examined UInt16 DEFAULT 0,
    candidates_retained UInt16 DEFAULT 0,
    reranker_best_score Float32 DEFAULT 0,
    reranker_mean_score Float32 DEFAULT 0,
    llm_calls_made UInt16 DEFAULT 0,
    reranker_model LowCardinality(String) DEFAULT '',
    reranker_latency_ms Float32 DEFAULT 0,
    hyde_used UInt8 DEFAULT 0,
    hyde_chars UInt32 DEFAULT 0,
    hyde_latency_ms Float32 DEFAULT 0,
    hyde_skipped_reason LowCardinality(String) DEFAULT '',
    gate_passed UInt8 DEFAULT 0,
    gate_reason LowCardinality(String) DEFAULT '',
    budget_notes_tokens UInt32 DEFAULT 0,
    budget_code_tokens UInt32 DEFAULT 0,
    budget_graph_tokens UInt32 DEFAULT 0,
    total_context_tokens UInt32 DEFAULT 0,
    sources_used String DEFAULT '',
    latency_ms Float32 DEFAULT 0,
    success UInt8 DEFAULT 1,
    error_type LowCardinality(String) DEFAULT ''
) ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, route_mode, request_id)
TTL date + INTERVAL 90 DAY;

CREATE TABLE IF NOT EXISTS obsidian_rag.rag_ingest_runs (
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    event LowCardinality(String),
    run_id String,
    files_scanned UInt32 DEFAULT 0,
    files_parsed UInt32 DEFAULT 0,
    files_skipped UInt32 DEFAULT 0,
    chunks_produced UInt32 DEFAULT 0,
    chunks_embedded UInt32 DEFAULT 0,
    chunks_stored UInt32 DEFAULT 0,
    stale_deleted UInt32 DEFAULT 0,
    latency_ms Float32 DEFAULT 0,
    success UInt8 DEFAULT 1,
    error_count UInt16 DEFAULT 0,
    error_type LowCardinality(String) DEFAULT '',
    error_message String DEFAULT ''
) ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, run_id)
TTL date + INTERVAL 90 DAY;

CREATE TABLE IF NOT EXISTS obsidian_rag.rag_ingest_stages (
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    event LowCardinality(String),
    run_id String DEFAULT '',
    stage_name LowCardinality(String) DEFAULT '',
    latency_ms Float32 DEFAULT 0,
    items_in UInt32 DEFAULT 0,
    items_out UInt32 DEFAULT 0,
    success UInt8 DEFAULT 1,
    error_count UInt16 DEFAULT 0,
    governor_action LowCardinality(String) DEFAULT '',
    governor_reason String DEFAULT '',
    ram_percent Float32 DEFAULT 0,
    cpu_percent Float32 DEFAULT 0,
    swap_percent Float32 DEFAULT 0,
    vocab_size UInt32 DEFAULT 0,
    documents_count UInt32 DEFAULT 0
) ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, run_id, stage_name)
TTL date + INTERVAL 90 DAY;

CREATE TABLE IF NOT EXISTS obsidian_rag.rag_embedding_batches (
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    event LowCardinality(String),
    request_id String DEFAULT '',
    run_id String DEFAULT '',
    batch_size UInt16 DEFAULT 0,
    batch_chars UInt32 DEFAULT 0,
    latency_ms Float32 DEFAULT 0,
    model_used LowCardinality(String) DEFAULT '',
    cache_hits UInt16 DEFAULT 0,
    cache_misses UInt16 DEFAULT 0,
    success UInt8 DEFAULT 1,
    retry_count UInt8 DEFAULT 0,
    error_type LowCardinality(String) DEFAULT ''
) ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, model_used)
TTL date + INTERVAL 90 DAY;

CREATE TABLE IF NOT EXISTS obsidian_rag.rag_cag_operations (
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    event LowCardinality(String),
    request_id String DEFAULT '',
    operation LowCardinality(String) DEFAULT '',
    pack_type LowCardinality(String) DEFAULT '',
    pack_scope String DEFAULT '',
    cache_hit UInt8 DEFAULT 0,
    ttl_remaining Float32 DEFAULT 0,
    nodes_matched UInt16 DEFAULT 0,
    communities_used UInt16 DEFAULT 0,
    traversal_depth UInt8 DEFAULT 0,
    graph_context_hit UInt8 DEFAULT 0,
    latency_ms Float32 DEFAULT 0,
    success UInt8 DEFAULT 1
) ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, event, operation)
TTL date + INTERVAL 90 DAY;

CREATE TABLE IF NOT EXISTS obsidian_rag.rag_store_operations (
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    event LowCardinality(String),
    request_id String DEFAULT '',
    run_id String DEFAULT '',
    operation LowCardinality(String) DEFAULT '',
    collection LowCardinality(String) DEFAULT '',
    latency_ms Float32 DEFAULT 0,
    batch_count UInt16 DEFAULT 0,
    results_count UInt16 DEFAULT 0,
    success UInt8 DEFAULT 1,
    retry_count UInt8 DEFAULT 0,
    error_type LowCardinality(String) DEFAULT ''
) ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, collection, operation)
TTL date + INTERVAL 90 DAY;

CREATE TABLE IF NOT EXISTS obsidian_rag.rag_resource_samples (
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    event LowCardinality(String),
    cpu_percent Float32 DEFAULT 0,
    ram_percent Float32 DEFAULT 0,
    ram_available_gb Float32 DEFAULT 0,
    swap_percent Float32 DEFAULT 0,
    disk_free_gb Float32 DEFAULT 0,
    vram_used_gb Float32 DEFAULT 0,
    vram_percent Float32 DEFAULT 0,
    psi_memory_full_avg10 Float32 DEFAULT 0,
    psi_io_full_avg10 Float32 DEFAULT 0,
    active_ingest UInt8 DEFAULT 0,
    governor_action LowCardinality(String) DEFAULT ''
) ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp)
TTL date + INTERVAL 30 DAY;

-- Materialized views for pre-aggregated dashboard queries

CREATE TABLE IF NOT EXISTS obsidian_rag.mv_requests_hourly_target (
    hour DateTime,
    endpoint LowCardinality(String),
    total_count UInt64,
    error_count UInt64,
    avg_latency_ms Float64,
    p95_latency_ms Float64,
    max_latency_ms Float64
) ENGINE = SummingMergeTree
ORDER BY (hour, endpoint);

CREATE MATERIALIZED VIEW IF NOT EXISTS obsidian_rag.mv_requests_hourly
TO obsidian_rag.mv_requests_hourly_target AS
SELECT
    toStartOfHour(timestamp) AS hour,
    endpoint,
    count() AS total_count,
    countIf(success = 0) AS error_count,
    avg(latency_ms) AS avg_latency_ms,
    quantile(0.95)(latency_ms) AS p95_latency_ms,
    max(latency_ms) AS max_latency_ms
FROM obsidian_rag.rag_requests
GROUP BY hour, endpoint;

CREATE TABLE IF NOT EXISTS obsidian_rag.mv_retrieval_hourly_target (
    hour DateTime,
    route_mode LowCardinality(String),
    total_queries UInt64,
    accepted UInt64,
    avg_best_score Float64,
    avg_latency_ms Float64,
    p95_latency_ms Float64
) ENGINE = SummingMergeTree
ORDER BY (hour, route_mode);

CREATE MATERIALIZED VIEW IF NOT EXISTS obsidian_rag.mv_retrieval_hourly
TO obsidian_rag.mv_retrieval_hourly_target AS
SELECT
    toStartOfHour(timestamp) AS hour,
    route_mode,
    count() AS total_queries,
    countIf(gate_passed = 1) AS accepted,
    avg(best_score) AS avg_best_score,
    avg(latency_ms) AS avg_latency_ms,
    quantile(0.95)(latency_ms) AS p95_latency_ms
FROM obsidian_rag.rag_retrieval
GROUP BY hour, route_mode
