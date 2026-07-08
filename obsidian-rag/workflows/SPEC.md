# RAG Workflows Spec

`workflows` owns durable execution wrappers for long-running RAG
admin jobs. It does not own ingestion, graph building, CAG generation, storage
lifecycle, or orchestration policy.

## Public Contract

- The API entrypoint remains `POST /admin/reprocess`.
- Supported targets are `local`, `sources`, `graph`, `cag`, and `all`.
- The canonical target executor is `execute_reprocess_target()` in
  `workflows.reprocess`.
- Every admin request carries normalized `origin` metadata. Callers should
  identify at least the boundary that initiated the request: `user_machine`,
  `agent`, `feature`, `service`, or `scheduler`.
- Long-running parent jobs expose child progress in `result.children`.
  Repo/document sources run as child jobs under the parent; parent-level reset
  and stale cleanup are coordinated once per run.
- Resource-pressure states are first-class job states. Parent and child records
  may expose `paused_resource_pressure`, `retry_scheduled`,
  `failed_resource_pressure` or `cancelled`.
- Admin jobs can be cancelled with `POST /admin/jobs/{job_id}/cancel`.
- Backend selection is configured by `[workflows] backend`:
  - `direct`: execute the canonical target executor in-process.
  - `temporal`: submit `RagReprocessWorkflow` to Temporal.

## Boundaries

- `pipeline.sync` owns the concrete sync, Graphify, and CAG functions.
- `workflows.reprocess` owns target-to-owner-function dispatch and durable
  progress publication only.
- `pipeline.sync` owns child source discovery, source execution, global reset,
  and stale cleanup semantics.
- Temporal is optional infrastructure. If selected, the service must be
  installed with the `temporal` extra and a worker must be running on the
  configured task queue.
- The orchestrator must call the RAG API rather than importing this package.

## Legacy Removal Rule

Do not keep per-endpoint target switches once the canonical executor covers a
target. API, workers, and tests must call the executor or submit the workflow.

## Origin Contract

`POST /admin/reprocess` accepts:

```json
{
  "origin": {
    "kind": "agent",
    "name": "codex",
    "agent": "codex",
    "machine": "workstation",
    "trace_id": "request-123",
    "reason": "operator requested RAG rebuild",
    "metadata": {"surface": "cli"}
  }
}
```

When headers such as `X-AI-Agent`, `X-AI-Feature`, `X-AI-Service`,
`X-AI-Machine`, or `X-Request-ID` are present, the API folds them into the same
normalized `origin` object.

## Parent/Child Job Contract

The accepted API job is the parent. Child records are stored under
`result.children` and include `child_id`, `phase`, `source`, `status`,
timestamps, optional `error`, and phase-specific `result`.

For repo/document ingestion, source scan/parse child jobs may run in parallel up
to `performance.source_scan_parallel_jobs`. Embedding and vector write are
protected by global lanes/leases and do not scale with source child parallelism.
For `force=true`, the shared repo/document collection is reset once by the
parent before child execution; children must not reset the global collection
independently.

When the Resource Governor remains in `PAUSE` beyond the configured budget, the
child is deferred with `retry_at`, `attempt`, `pause_started_at`,
`pause_budget_seconds`, `last_governor_snapshot` and `resource_state`. Retries
are finite; after `performance.resource_retry_max_attempts`, the child becomes
`failed_resource_pressure`.
