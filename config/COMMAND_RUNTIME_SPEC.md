# Command Runtime Config Spec

Data: 2026-06-15.

## Ownership.

`config/` owns user-facing command runtime knobs and host capability inference.
`features/workspace_execution` owns sandbox behavior and Docker runner
implementation. The orchestrator owns only tool dispatch and policy gating.

## Canonical Config

Sandbox runtime selection is exposed to `workspace_execution` as generated env
surface from config:

```toml
[command_runtime]
sandbox_runtime = "docker" # docker | runsc
require_runtime = false
```

Runtime envs:

- `WORKSPACE_EXECUTION_SANDBOX_RUNTIME=docker|runsc`
- `WORKSPACE_EXECUTION_REQUIRE_RUNTIME=true|false`
- `ORC_AGENTIC_RUNTIME_COMMAND_TOOL_TIMEOUT_SECONDS`
- `ORC_AGENTIC_RUNTIME_COMMAND_TOOL_MAX_OUTPUT_BYTES`
- `ORC_AGENTIC_RUNTIME_COMMAND_TOOL_SESSION_TTL_SECONDS`
- `ORC_AGENTIC_RUNTIME_COMMAND_TOOL_MAX_COMMANDS_PER_SESSION`
- `ORC_AGENTIC_RUNTIME_COMMAND_TOOL_DOCKER_MEMORY_LIMIT_MB`
- `ORC_AGENTIC_RUNTIME_COMMAND_TOOL_DOCKER_PIDS_LIMIT`

`docker` remains the default. `runsc`/gVisor is opt-in and must degrade
explicitly when not installed unless `require_runtime = true`.

Command-tool budgets are derived from the global LLM request timeout,
quality/latency profile and worker count. They are watchdogs for autonomous
validation/recovery loops, not short fixed probes; long commands may run until
their configured timeout, while stuck sessions are still bounded by TTL and max
command count.

## Evidence

Command execution evidence must include:

- selected runtime;
- whether runtime support was inferred or required;
- sandbox image;
- network mode;
- user;
- resource limits.

## Legacy Removal Rule

`workspace_execution` owns the configured runtime path. The orchestrator command
tool must reject non-`workspace_execution` backends instead of keeping local
process or Docker runners as compatibility paths. Runner semantics, sandbox
runtime selection, copies, diffs, artefacts, and VM gating live in
`features/workspace_execution`.
