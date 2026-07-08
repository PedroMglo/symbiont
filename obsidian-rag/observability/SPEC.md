# RAG Observability Spec

Data: 2026-06-15.

## Ownership

`obsidian-rag` owns observability for RAG API behavior, ingestion,
retrieval, graph/enrichment, ranking, CAG, RAG budgets and RAG-specific
diagnostics.

The orchestrator may correlate with RAG through request IDs and trace IDs, but
must not import RAG internals or reimplement RAG observability.

## Canonical Telemetry Direction

New RAG observability should emit OpenTelemetry spans, metrics and span events
using the documented `ai.local.*` attribute names. RAG may keep local
ClickHouse/dashboard transition sinks only until dashboard parity is proven.

Required correlation attributes:

- `ai.local.owner = "obsidian-rag"`
- `ai.local.request_id`
- `ai.local.symbiont_request_id` when present
- `ai.local.trace_kind`
- `ai.local.rag.collection`
- `ai.local.rag.query_hash`
- `ai.local.rag.results_count`
- `ai.local.graph.used` and graph-specific counters for graph context

## Migration Rule

`events.py`, `_dispatcher.py` and `schema.sql` remain transition surfaces
until RAG dashboards and eval/audit tests can read equivalent data from the
OpenTelemetry collector or a documented ledger/export. Once a table/event path
is migrated, the old sink path must be removed in the same phase.
