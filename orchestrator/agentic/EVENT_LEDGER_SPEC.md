# Agentic Event Ledger Spec

Owner: `orchestrator/agentic`.

The agentic event ledger is the authoritative runtime history for autonomous
control flow. State snapshots are projections and must be reproducible by
replaying ledger events through the reducer.

## Required Context

Every event recorded through `AgenticStore.record_event` includes an
`event_context` payload object with normalized audit fields when available:

- `event_id`
- `event_type`
- `task_id`
- `trace_id`
- `request_id`
- `capability_id` and `capability_ids`
- `policy_action` and `policy_actions`
- `resource_lease_id`
- `resource_lease_decision`
- `action_id`
- `decision_id`
- `state_hash`
- `input_state_hash`
- `evidence_refs`

The context is derived from task metadata, action metadata, capability
metadata, lease decisions, decision payloads and action results. Existing event
payload fields remain stable for compatibility; `event_context` is the
canonical cross-event index for replay, trace explanation and audit readers.

## Replay Rule

Reducers may ignore fields that are not relevant to state projection, but they
must tolerate `event_context` on every payload. Replay tests must prove that
the latest state snapshot hash matches the hash produced by replaying the
ledger from the initial task state.

## Boundary Rule

The event ledger stores orchestration context and owner evidence references
only. It must not copy owner internals, durable storage payloads, full command
output, feature business data, RAG internals or service-specific fallback
state into orchestrator-owned events.
