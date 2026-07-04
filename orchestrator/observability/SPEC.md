# Orchestrator Observability Spec

Data: 2026-06-15.

## Ownership

`orchestrator` owns runtime observability for routing, dispatch,
pipeline execution, policy gates, tool execution, model calls, agentic state,
ledger correlation and gateway-facing request traces.

It does not own RAG internals, feature business metrics, storage lifecycle
metrics, or central runtime config inference. Those owners may publish their
own OpenTelemetry spans and metrics with the shared `ai.local.*` semantic
attribute names, but they must not import orchestrator internals.

## Canonical Telemetry Contract

OpenTelemetry is the canonical transport for new observability behavior.
Langfuse is the canonical LLM/pipeline trace view. ClickHouse, JSONL and local
SQLite stores are transition sinks until their dashboards and audit readers
consume OpenTelemetry/collector outputs or the event ledger directly.

Every emitted span, metric or span event owned by the orchestrator should use
the constants in `semantic_attributes.py`. String literals for cross-cutting
attribute keys are forbidden and must be replaced when touched.

Required correlation attributes:

- `ai.local.owner`
- `ai.local.request_id`
- `ai.local.trace_kind`
- `ai.local.run_id` when tied to a graph/agentic run
- `ai.local.task_id` when tied to an agentic task
- `ai.local.capability_id` when tied to a capability dispatch
- `ai.local.policy_action` and `ai.local.risk_level` when tied to policy
- `ai.local.model.name`, `ai.local.model.backend`, and
  `ai.local.model.profile` when tied to model work

## Migration Rule

No dispatcher, sink, schema or store may be removed until the replacement has:

- targeted tests;
- equivalent trace/metric/dashboard visibility;
- a rollback path;
- no live caller depending on the removed surface.

After a surface is migrated, the old implementation must be removed in the
same phase. Transition wrappers are allowed only when a live caller and a
dated sunset are documented.

## Phase 1 Baseline

The first migration establishes semantic attributes and maps structured events
onto the active OpenTelemetry span. Removed code in this phase is the broken
eval baseline path under `docs/benchmarks`, not the dispatcher itself; removing
the dispatcher now would drop dashboard parity.
