# Runtime Tool Envelope Spec

Owner: `orchestrator/agentic`.

The runtime tool envelope is the orchestrator-owned contract that normalizes
owner-published capability manifests for policy, resource leasing, dispatch,
ledger evidence, and future capability search.

It is metadata only. It must not execute service behavior, import owner
packages, infer domain semantics, parse storage/RAG/feature/agent payloads, or
replace an owner API. The orchestrator may validate the envelope, apply policy,
request leases, call dispatch/API transport, and record evidence.

## Inputs

The envelope is built from declarative manifests already owned by the current
capability system:

- `ActionCapabilityManifest`
- `ServiceCapabilityManifest`

The builder may import orchestrator manifest/catalog modules. It must not import
packages owned by `agents`, `features`, `storage_guardian`, or `obsidian-rag`.

## Required Fields

- `capability_id`
- `owner`
- `transport`
- `input_schema`
- `output_schema`
- `schema_refs`
- `policy_action`
- `risk_level`
- `is_read_only`
- `is_concurrency_safe`
- `resource_profile`
- `idempotency_policy`
- `evidence_types`
- `events_published`
- `result_persistence`

## Defaults

- `is_read_only` is derived from `writes_allowed`.
- `schema_refs.input` and `schema_refs.output` are derived from
  `input_schema.schema_ref` and `output_schema.schema_ref`.
- `is_concurrency_safe` is `false` unless the owner manifest explicitly
  declares `concurrency_safe` or `is_concurrency_safe` in `resource_profile` or
  `transport`.
- writable capabilities require owner evidence and previews before durable
  application.
- read-only capabilities produce ledger evidence, but do not imply durable
  owner output.

## Legacy Extinction Rule

Runtime code must consume these envelopes instead of adding parallel Python
registries for risk, schemas, evidence, timeout, or resource metadata. If an
existing Python fallback duplicates a manifest field, it must be removed once
the manifest-backed envelope covers the caller.
