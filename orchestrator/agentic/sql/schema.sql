CREATE TABLE IF NOT EXISTS agentic_tasks (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    priority TEXT NOT NULL,
    session_id TEXT,
    user_id_hash TEXT,
    trace_id TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    budget_json TEXT NOT NULL,
    result_json TEXT,
    error_json TEXT,
    metadata_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agentic_tasks_status ON agentic_tasks(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_agentic_tasks_session ON agentic_tasks(session_id, updated_at);

CREATE TABLE IF NOT EXISTS agentic_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    graph_run_id TEXT,
    entrypoint TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    metadata_json TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES agentic_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_runs_task ON agentic_runs(task_id, started_at);

CREATE TABLE IF NOT EXISTS agentic_steps (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id TEXT,
    step_name TEXT NOT NULL,
    step_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    duration_ms REAL,
    input_preview TEXT,
    output_preview TEXT,
    error_json TEXT,
    metadata_json TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES agentic_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_steps_task ON agentic_steps(task_id, started_at);

CREATE TABLE IF NOT EXISTS agentic_events (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    event_type TEXT NOT NULL,
    timestamp REAL NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    trace_id TEXT,
    FOREIGN KEY(task_id) REFERENCES agentic_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_events_task ON agentic_events(task_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_agentic_events_type ON agentic_events(event_type, timestamp);

CREATE TABLE IF NOT EXISTS agentic_state_snapshots (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    previous_state_hash TEXT,
    state_json TEXT NOT NULL,
    source_event_id TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY(task_id) REFERENCES agentic_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_state_snapshots_task ON agentic_state_snapshots(task_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agentic_state_snapshots_hash ON agentic_state_snapshots(task_id, state_hash);

CREATE TABLE IF NOT EXISTS agentic_decisions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    input_state_hash TEXT NOT NULL,
    decision_status TEXT NOT NULL,
    confidence REAL NOT NULL,
    decision_json TEXT NOT NULL,
    raw_output_ref_json TEXT,
    valid INTEGER NOT NULL DEFAULT 1,
    error_json TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY(task_id) REFERENCES agentic_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_decisions_task ON agentic_decisions(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agentic_decisions_input_hash ON agentic_decisions(task_id, input_state_hash);

CREATE TABLE IF NOT EXISTS agentic_raw_outputs (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    trace_id TEXT,
    agent TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    preview TEXT NOT NULL,
    redacted INTEGER NOT NULL DEFAULT 1,
    artifact_ref TEXT,
    size_bytes INTEGER NOT NULL,
    created_at REAL NOT NULL,
    metadata_json TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES agentic_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_raw_outputs_task ON agentic_raw_outputs(task_id, created_at);

CREATE TABLE IF NOT EXISTS agentic_tool_calls (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    step_id TEXT,
    tool_name TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    status TEXT NOT NULL,
    input_preview TEXT,
    output_preview TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    requires_approval INTEGER NOT NULL DEFAULT 0,
    approval_id TEXT,
    error TEXT,
    metadata_json TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES agentic_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_tool_calls_task ON agentic_tool_calls(task_id, started_at);

CREATE TABLE IF NOT EXISTS agentic_approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    action TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    payload_preview TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    dry_run_result TEXT,
    status TEXT NOT NULL,
    requested_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    approved_by TEXT,
    approved_at REAL,
    rejected_reason TEXT,
    metadata_json TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES agentic_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_approvals_status ON agentic_approvals(status, requested_at);

CREATE TABLE IF NOT EXISTS agentic_preapproval_windows (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    action TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    max_uses INTEGER NOT NULL,
    used_count INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL,
    revoked_at REAL,
    revoked_reason TEXT,
    FOREIGN KEY(task_id) REFERENCES agentic_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_preapproval_windows_status ON agentic_preapproval_windows(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_agentic_preapproval_windows_action ON agentic_preapproval_windows(action, status, expires_at);

CREATE TABLE IF NOT EXISTS agentic_resource_leases (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    lease_id TEXT,
    capability TEXT NOT NULL,
    decision TEXT NOT NULL,
    status TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    renewed_at REAL,
    released_at REAL,
    expires_at REAL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES agentic_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_resource_leases_task ON agentic_resource_leases(task_id, acquired_at);

CREATE TABLE IF NOT EXISTS agentic_runtime_flags (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL
);
CREATE INDEX IF NOT EXISTS idx_agentic_runtime_flags_expiry ON agentic_runtime_flags(expires_at);

CREATE TABLE IF NOT EXISTS agentic_improvement_proposals (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    confidence REAL NOT NULL,
    score REAL NOT NULL,
    fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL,
    approval_id TEXT,
    applied_at REAL,
    rejected_reason TEXT,
    FOREIGN KEY(task_id) REFERENCES agentic_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_improvements_status ON agentic_improvement_proposals(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_agentic_improvements_fingerprint ON agentic_improvement_proposals(fingerprint, status);

CREATE TABLE IF NOT EXISTS agentic_actuations (
    id TEXT PRIMARY KEY,
    proposal_id TEXT,
    task_id TEXT,
    action TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    before_json TEXT NOT NULL,
    operation_json TEXT NOT NULL,
    after_json TEXT,
    impact_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    error_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL,
    rolled_back_at REAL,
    rollback_reason TEXT,
    FOREIGN KEY(proposal_id) REFERENCES agentic_improvement_proposals(id),
    FOREIGN KEY(task_id) REFERENCES agentic_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_actuations_status ON agentic_actuations(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_agentic_actuations_proposal ON agentic_actuations(proposal_id, created_at);

CREATE TABLE IF NOT EXISTS agentic_parallel_rounds (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    status TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    round_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(task_id) REFERENCES agentic_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_parallel_rounds_task ON agentic_parallel_rounds(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agentic_parallel_rounds_plan ON agentic_parallel_rounds(plan_id, updated_at);

CREATE TABLE IF NOT EXISTS agentic_messages (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    round_id TEXT,
    message_type TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY(task_id) REFERENCES agentic_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_messages_task ON agentic_messages(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agentic_messages_round ON agentic_messages(round_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agentic_messages_type ON agentic_messages(message_type, created_at);

CREATE TABLE IF NOT EXISTS agentic_ai_events (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    trace_id TEXT,
    producer TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    event_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY(task_id) REFERENCES agentic_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_ai_events_task ON agentic_ai_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agentic_ai_events_type ON agentic_ai_events(event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_agentic_ai_events_producer ON agentic_ai_events(producer, created_at);

CREATE TABLE IF NOT EXISTS agentic_memories (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    trace_id TEXT,
    memory_type TEXT NOT NULL,
    source TEXT NOT NULL,
    content_preview TEXT NOT NULL,
    memory_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL,
    FOREIGN KEY(task_id) REFERENCES agentic_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_memories_task ON agentic_memories(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agentic_memories_type ON agentic_memories(memory_type, created_at);

CREATE TABLE IF NOT EXISTS agentic_command_sessions (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    trace_id TEXT,
    context_profile TEXT NOT NULL,
    cwd TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL,
    closed_at REAL,
    metadata_json TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES agentic_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_command_sessions_status ON agentic_command_sessions(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_agentic_command_sessions_task ON agentic_command_sessions(task_id, created_at);

CREATE TABLE IF NOT EXISTS agentic_command_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT,
    trace_id TEXT,
    command TEXT NOT NULL,
    cwd TEXT NOT NULL,
    context_profile TEXT NOT NULL,
    action TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    policy_decision TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    stdout_preview TEXT,
    stderr_preview TEXT,
    output_truncated INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL,
    finished_at REAL,
    duration_ms REAL,
    approval_id TEXT,
    error TEXT,
    metadata_json TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES agentic_command_sessions(id),
    FOREIGN KEY(task_id) REFERENCES agentic_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_agentic_command_runs_session ON agentic_command_runs(session_id, started_at);
CREATE INDEX IF NOT EXISTS idx_agentic_command_runs_task ON agentic_command_runs(task_id, started_at);
