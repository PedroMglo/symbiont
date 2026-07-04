-- ClickHouse schema for AI Symbiont observability
-- Database: ai_symbiont
-- Run: clickhouse-client < schema.sql

CREATE DATABASE IF NOT EXISTS ai_symbiont;

-- ═══════════════════════════════════════
-- Main events table
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS ai_symbiont.llm_events
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),

    event LowCardinality(String),
    level LowCardinality(String),

    request_id String,
    session_id String DEFAULT '',
    trace_id String DEFAULT '',
    span_id String DEFAULT '',

    entrypoint LowCardinality(String) DEFAULT '',

    model LowCardinality(String) DEFAULT '',
    backend LowCardinality(String) DEFAULT '',
    backend_type LowCardinality(String) DEFAULT '',
    profile LowCardinality(String) DEFAULT '',
    intent LowCardinality(String) DEFAULT '',
    complexity LowCardinality(String) DEFAULT '',

    requested_model String DEFAULT '',
    selected_model String DEFAULT '',
    requested_profile String DEFAULT '',
    selected_backend String DEFAULT '',

    fallback_used UInt8 DEFAULT 0,
    fallback_reason LowCardinality(String) DEFAULT '',

    prompt_tokens UInt32 DEFAULT 0,
    completion_tokens UInt32 DEFAULT 0,
    total_tokens UInt32 DEFAULT 0,
    context_tokens UInt32 DEFAULT 0,
    usage_source LowCardinality(String) DEFAULT 'missing',

    total_latency_ms Float32 DEFAULT 0,
    first_token_latency_ms Float32 DEFAULT 0,
    model_load_latency_ms Float32 DEFAULT 0,
    prompt_eval_latency_ms Float32 DEFAULT 0,
    generation_latency_ms Float32 DEFAULT 0,
    context_build_latency_ms Float32 DEFAULT 0,
    rag_latency_ms Float32 DEFAULT 0,
    graph_latency_ms Float32 DEFAULT 0,
    llm_latency_ms Float32 DEFAULT 0,
    router_latency_ms Float32 DEFAULT 0,

    tokens_per_second Float32 DEFAULT 0,
    prompt_tokens_per_second Float32 DEFAULT 0,
    generation_tokens_per_second Float32 DEFAULT 0,

    rag_used UInt8 DEFAULT 0,
    graph_used UInt8 DEFAULT 0,
    tools_used UInt8 DEFAULT 0,
    agentic UInt8 DEFAULT 0,
    iterations UInt16 DEFAULT 0,

    cold_start UInt8 DEFAULT 0,
    stream UInt8 DEFAULT 0,
    chunks_count UInt16 DEFAULT 0,

    cpu_percent Float32 DEFAULT 0,
    ram_used_mb UInt32 DEFAULT 0,
    gpu_util_percent Float32 DEFAULT 0,
    vram_used_mb UInt32 DEFAULT 0,
    vram_peak_mb UInt32 DEFAULT 0,

    success UInt8 DEFAULT 1,
    error_type LowCardinality(String) DEFAULT '',
    error_message_safe String DEFAULT '',

    query_length UInt32 DEFAULT 0,
    response_length UInt32 DEFAULT 0,
    query_hash String DEFAULT '',

    metadata_json String DEFAULT '',

    ollama_total_duration UInt64 DEFAULT 0,
    ollama_load_duration UInt64 DEFAULT 0,
    ollama_prompt_eval_count UInt32 DEFAULT 0,
    ollama_prompt_eval_duration UInt64 DEFAULT 0,
    ollama_eval_count UInt32 DEFAULT 0,
    ollama_eval_duration UInt64 DEFAULT 0
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, event, model, backend, session_id)
TTL date + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;


-- ═══════════════════════════════════════
-- Sessions aggregate table
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS ai_symbiont.llm_sessions
(
    session_id String,
    first_seen DateTime64(3),
    last_seen DateTime64(3),
    request_count UInt32,
    total_tokens UInt64,
    total_latency_ms Float64,
    models_used Array(String),
    intents Array(String),
    error_count UInt32,
    entrypoint LowCardinality(String)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(first_seen)
ORDER BY (session_id, first_seen);


-- ═══════════════════════════════════════
-- Router decisions detail
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS ai_symbiont.router_decisions
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    request_id String,
    intent LowCardinality(String),
    complexity LowCardinality(String),
    requested_model String,
    selected_model LowCardinality(String),
    selected_backend LowCardinality(String),
    profile LowCardinality(String),
    fallback_used UInt8 DEFAULT 0,
    fallback_reason LowCardinality(String) DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, intent, selected_model)
TTL date + INTERVAL 180 DAY;


-- ═══════════════════════════════════════
-- Backend health events
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS ai_symbiont.backend_health_events
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    backend LowCardinality(String),
    backend_type LowCardinality(String),
    status LowCardinality(String),
    latency_ms Float32 DEFAULT 0,
    models_available Array(String),
    error_message String DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, backend)
TTL date + INTERVAL 90 DAY;


-- ═══════════════════════════════════════
-- Resource samples
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS ai_symbiont.resource_samples
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    cpu_percent Float32 DEFAULT 0,
    ram_used_mb UInt32 DEFAULT 0,
    ram_total_mb UInt32 DEFAULT 0,
    ram_percent Float32 DEFAULT 0,
    gpu_util_percent Float32 DEFAULT 0,
    vram_used_mb UInt32 DEFAULT 0,
    vram_total_mb UInt32 DEFAULT 0,
    vram_free_mb UInt32 DEFAULT 0,
    gpu_name LowCardinality(String) DEFAULT '',
    gpu_temperature_c Float32 DEFAULT 0,
    gpu_power_w Float32 DEFAULT 0,
    ollama_models_loaded UInt8 DEFAULT 0,
    ollama_vram_used_mb UInt32 DEFAULT 0
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY timestamp
TTL date + INTERVAL 30 DAY;


-- ═══════════════════════════════════════
-- RAG events
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS ai_symbiont.rag_events
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    request_id String,
    latency_ms Float32 DEFAULT 0,
    documents_retrieved UInt16 DEFAULT 0,
    context_tokens UInt32 DEFAULT 0,
    success UInt8 DEFAULT 1,
    error_type LowCardinality(String) DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, request_id)
TTL date + INTERVAL 180 DAY;


-- ═══════════════════════════════════════
-- Tool events
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS ai_symbiont.tool_events
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    request_id String,
    session_id String DEFAULT '',
    tool_name LowCardinality(String),
    latency_ms Float32 DEFAULT 0,
    success UInt8 DEFAULT 1,
    error_type LowCardinality(String) DEFAULT '',
    iterations UInt16 DEFAULT 0
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, tool_name)
TTL date + INTERVAL 180 DAY;


-- ═══════════════════════════════════════
-- Adaptation events (degradation mode changes, cache, evictions)
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS ai_symbiont.adaptation_events
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    event_type LowCardinality(String),  -- 'degradation_change', 'model_eviction', 'cache_shrink', 'cache_hit', 'cache_miss'
    prev_mode String DEFAULT '',
    new_mode String DEFAULT '',
    trigger_metric String DEFAULT '',   -- 'vram_pressure', 'ram_pressure', etc.
    trigger_value Float32 DEFAULT 0,
    detail String DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, event_type)
TTL date + INTERVAL 30 DAY;


-- ═══════════════════════════════════════
-- Materialized views for fast queries
-- ═══════════════════════════════════════

-- Hourly model stats
CREATE MATERIALIZED VIEW IF NOT EXISTS ai_symbiont.mv_model_hourly
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(hour)
ORDER BY (hour, model, backend)
AS SELECT
    toStartOfHour(timestamp) AS hour,
    model,
    backend,
    count() AS requests,
    sum(total_tokens) AS tokens,
    sum(total_latency_ms) AS latency_sum,
    sum(cold_start) AS cold_starts,
    sum(fallback_used) AS fallbacks,
    sum(1 - success) AS errors
FROM ai_symbiont.llm_events
WHERE event IN ('request_completed', 'llm_call_completed')
GROUP BY hour, model, backend;


-- ═══════════════════════════════════════
-- Graph node execution events (LangGraph tracing)
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS ai_symbiont.graph_node_events
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),

    -- Correlation
    graph_run_id String,
    request_id String DEFAULT '',
    session_id String DEFAULT '',
    trace_id String DEFAULT '',
    span_id String DEFAULT '',
    parent_span_id String DEFAULT '',

    -- Node identity
    node_name LowCardinality(String),
    node_type LowCardinality(String) DEFAULT '',  -- classify|route|context|agent|critic|synthesize|learn|direct|llm_fallback|collect

    -- Timing
    duration_ms Float32 DEFAULT 0,

    -- Result
    success UInt8 DEFAULT 1,
    error_type LowCardinality(String) DEFAULT '',
    error_message String DEFAULT '',

    -- Context
    tokens_used UInt32 DEFAULT 0,
    iteration UInt16 DEFAULT 0,
    parallel_group String DEFAULT '',

    -- State info
    input_keys Array(String) DEFAULT [],
    output_keys Array(String) DEFAULT [],

    -- Extensible
    metadata_json String DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, graph_run_id, node_name)
TTL date + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;


-- ═══════════════════════════════════════
-- Graph run summary (one row per graph.invoke())
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS ai_symbiont.graph_runs
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),

    graph_run_id String,
    request_id String DEFAULT '',
    session_id String DEFAULT '',
    trace_id String DEFAULT '',

    -- Metrics
    total_duration_ms Float32 DEFAULT 0,
    node_count UInt16 DEFAULT 0,
    success UInt8 DEFAULT 1,
    error_type LowCardinality(String) DEFAULT '',

    -- Path taken
    path Array(String) DEFAULT [],
    agents_invoked Array(String) DEFAULT [],
    context_sources Array(String) DEFAULT [],

    -- Classification
    intent LowCardinality(String) DEFAULT '',
    complexity LowCardinality(String) DEFAULT '',
    confidence Float32 DEFAULT 0,

    -- Features
    fallback_used UInt8 DEFAULT 0,
    critic_invoked UInt8 DEFAULT 0,
    critic_loops UInt16 DEFAULT 0,
    iterations UInt16 DEFAULT 0,

    -- Tokens
    total_tokens UInt32 DEFAULT 0,
    model_used LowCardinality(String) DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, graph_run_id)
TTL date + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;


-- Hourly graph node stats (aggregated)
CREATE MATERIALIZED VIEW IF NOT EXISTS ai_symbiont.mv_graph_node_hourly
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(hour)
ORDER BY (hour, node_name, node_type)
AS SELECT
    toStartOfHour(timestamp) AS hour,
    node_name,
    node_type,
    count() AS executions,
    sum(duration_ms) AS duration_sum,
    sum(1 - success) AS errors,
    sum(tokens_used) AS tokens
FROM ai_symbiont.graph_node_events
GROUP BY hour, node_name, node_type;


-- ═══════════════════════════════════════════════════════════════════════════════
-- GEMILYNI — Gemini Execution Layer Observability (v2.1)
-- ═══════════════════════════════════════════════════════════════════════════════

-- Execution runs (top-level summary per run)
CREATE TABLE IF NOT EXISTS ai_symbiont.gemilyni_runs
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    run_id String,
    trace_id String DEFAULT '',
    query_id String DEFAULT '',
    complexity LowCardinality(String) DEFAULT '',
    intent LowCardinality(String) DEFAULT '',
    selected_path LowCardinality(String) DEFAULT '',
    external_used UInt8 DEFAULT 0,
    workers_total UInt16 DEFAULT 0,
    workers_succeeded UInt16 DEFAULT 0,
    workers_failed UInt16 DEFAULT 0,
    containers_total UInt16 DEFAULT 0,
    containers_failed UInt16 DEFAULT 0,
    total_duration_ms Float32 DEFAULT 0,
    planning_duration_ms Float32 DEFAULT 0,
    bundle_duration_ms Float32 DEFAULT 0,
    container_start_duration_ms Float32 DEFAULT 0,
    gemini_duration_ms Float32 DEFAULT 0,
    synthesis_duration_ms Float32 DEFAULT 0,
    fallback_used UInt8 DEFAULT 0,
    final_status LowCardinality(String) DEFAULT '',
    reason String DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, run_id)
TTL date + INTERVAL 180 DAY;


-- Workers (one row per worker)
CREATE TABLE IF NOT EXISTS ai_symbiont.gemilyni_workers
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    run_id String,
    trace_id String DEFAULT '',
    worker_id String,
    bundle_id String DEFAULT '',
    task_type LowCardinality(String) DEFAULT '',
    status LowCardinality(String) DEFAULT '',
    started_at DateTime64(3) DEFAULT 0,
    finished_at DateTime64(3) DEFAULT 0,
    duration_ms Float32 DEFAULT 0,
    container_id String DEFAULT '',
    image LowCardinality(String) DEFAULT '',
    auth_mode LowCardinality(String) DEFAULT '',
    exit_code Int16 DEFAULT -1,
    error_type LowCardinality(String) DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, run_id, worker_id)
TTL date + INTERVAL 180 DAY;


-- Bundles (one row per bundle)
CREATE TABLE IF NOT EXISTS ai_symbiont.gemilyni_bundles
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    run_id String,
    trace_id String DEFAULT '',
    bundle_id String,
    worker_id String,
    allowed_files_count UInt16 DEFAULT 0,
    blocked_files_count UInt16 DEFAULT 0,
    allowed_context_blocks UInt16 DEFAULT 0,
    blocked_context_blocks UInt16 DEFAULT 0,
    workspace_mode LowCardinality(String) DEFAULT '',
    repo_mounted_directly UInt8 DEFAULT 0,
    workspace_readonly UInt8 DEFAULT 1,
    manifest_hash String DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, run_id, bundle_id)
TTL date + INTERVAL 180 DAY;


-- Bundle files (one row per file considered)
CREATE TABLE IF NOT EXISTS ai_symbiont.gemilyni_bundle_files
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    run_id String,
    trace_id String DEFAULT '',
    bundle_id String,
    worker_id String,
    relative_path String,
    file_hash String DEFAULT '',
    file_size_bytes UInt64 DEFAULT 0,
    included UInt8 DEFAULT 0,
    blocked UInt8 DEFAULT 0,
    block_reason LowCardinality(String) DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, run_id, bundle_id)
TTL date + INTERVAL 180 DAY;


-- Context blocks (one row per context block considered)
CREATE TABLE IF NOT EXISTS ai_symbiont.gemilyni_context_blocks
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    run_id String,
    trace_id String DEFAULT '',
    bundle_id String DEFAULT '',
    worker_id String DEFAULT '',
    source LowCardinality(String),
    source_type LowCardinality(String) DEFAULT '',
    block_hash String DEFAULT '',
    token_estimate UInt32 DEFAULT 0,
    size_bytes UInt32 DEFAULT 0,
    included UInt8 DEFAULT 0,
    blocked UInt8 DEFAULT 0,
    block_reason LowCardinality(String) DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, run_id, source)
TTL date + INTERVAL 180 DAY;


-- Containers (lifecycle events)
CREATE TABLE IF NOT EXISTS ai_symbiont.gemilyni_containers
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    run_id String,
    trace_id String DEFAULT '',
    worker_id String,
    container_id String,
    image LowCardinality(String) DEFAULT '',
    auth_mode LowCardinality(String) DEFAULT '',
    created_at DateTime64(3) DEFAULT 0,
    started_at DateTime64(3) DEFAULT 0,
    finished_at DateTime64(3) DEFAULT 0,
    status LowCardinality(String) DEFAULT '',
    exit_code Int16 DEFAULT -1,
    duration_ms Float32 DEFAULT 0
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, run_id, container_id)
TTL date + INTERVAL 180 DAY;


-- Container stats (time-series samples during execution)
CREATE TABLE IF NOT EXISTS ai_symbiont.gemilyni_container_stats
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    run_id String,
    trace_id String DEFAULT '',
    worker_id String,
    container_id String,
    cpu_percent Float32 DEFAULT 0,
    memory_usage_bytes UInt64 DEFAULT 0,
    memory_limit_bytes UInt64 DEFAULT 0,
    memory_percent Float32 DEFAULT 0,
    network_rx_bytes UInt64 DEFAULT 0,
    network_tx_bytes UInt64 DEFAULT 0,
    block_read_bytes UInt64 DEFAULT 0,
    block_write_bytes UInt64 DEFAULT 0
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, container_id)
TTL date + INTERVAL 30 DAY;


-- Gemini invocations (one row per CLI call)
CREATE TABLE IF NOT EXISTS ai_symbiont.gemilyni_gemini_invocations
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    run_id String,
    trace_id String DEFAULT '',
    worker_id String,
    container_id String DEFAULT '',
    started_at DateTime64(3) DEFAULT 0,
    finished_at DateTime64(3) DEFAULT 0,
    duration_ms Float32 DEFAULT 0,
    status LowCardinality(String) DEFAULT '',
    exit_code Int16 DEFAULT -1,
    auth_mode LowCardinality(String) DEFAULT '',
    model LowCardinality(String) DEFAULT '',
    input_tokens_estimate UInt32 DEFAULT 0,
    output_tokens_estimate UInt32 DEFAULT 0,
    stdout_size_bytes UInt32 DEFAULT 0,
    stderr_size_bytes UInt32 DEFAULT 0,
    error_type LowCardinality(String) DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, run_id, worker_id)
TTL date + INTERVAL 180 DAY;


-- Policy events (violations and blocks)
CREATE TABLE IF NOT EXISTS ai_symbiont.gemilyni_policy_events
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    run_id String,
    trace_id String DEFAULT '',
    worker_id String DEFAULT '',
    policy_name LowCardinality(String),
    event_type LowCardinality(String) DEFAULT '',
    blocked_item_type LowCardinality(String) DEFAULT '',
    blocked_item_ref String DEFAULT '',
    reason String DEFAULT '',
    severity LowCardinality(String) DEFAULT 'warning'
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, run_id, policy_name)
TTL date + INTERVAL 180 DAY;


-- Worker outputs (metadata only, never raw content)
CREATE TABLE IF NOT EXISTS ai_symbiont.gemilyni_worker_outputs
(
    timestamp DateTime64(3),
    date Date MATERIALIZED toDate(timestamp),
    run_id String,
    trace_id String DEFAULT '',
    worker_id String,
    container_id String DEFAULT '',
    result_json_exists UInt8 DEFAULT 0,
    patch_diff_exists UInt8 DEFAULT 0,
    patch_size_bytes UInt32 DEFAULT 0,
    result_size_bytes UInt32 DEFAULT 0,
    logs_size_bytes UInt32 DEFAULT 0,
    output_files_count UInt16 DEFAULT 0
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (timestamp, run_id, worker_id)
TTL date + INTERVAL 180 DAY;
