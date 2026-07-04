# Auditable Reflection Spec

`orchestrator/agentic` owns the read-only reflection projection for autonomous
runtime operation. The projection explains operational quality and owner
selection from the event ledger; it must not execute feature behavior, import
owner internals, or infer learning data from raw LLM text.

## Inputs

Only persisted audit records may feed the reflection projection:

- `agentic_events`
- `agentic_tool_calls`
- `agentic_resource_leases`
- `ai_local_events`
- task traces and state snapshots

## Metrics

The cockpit reflection view exposes these continuous evaluation buckets:

- route quality: route/owner selection events with a concrete owner or
  capability reference.
- answer quality: explicit quality/critic/validation events only. Missing
  events must be reported as `insufficient_evidence`.
- tool success: completed vs failed/blocked/denied tool calls.
- policy blocks: policy events that deny, block, or wait for approval.
- resource pressure: denied/deferred/expired resource leases and explicit
  resource pressure events.
- RAG misses: `rag.miss` ledger events and normalized
  `ai_local.rag.miss` events.

## Learning Loop

The learning loop is audit-only in this phase. It may surface candidate signals
and evidence refs for later review, but it must not write memory, mutate routing
policy, or promote behavior automatically.

## Owner Explanation

Owner explanations are derived from boundary-checked action events and route
selection events. Every explanation must include the evidence event id and must
prefer owner/capability metadata from manifests or runtime envelopes over hidden
Python vocabulary.
