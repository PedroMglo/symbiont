# RAG Workflows Spec

`workflows` owns durable execution wrappers for long-running RAG
admin jobs. It does not own ingestion, graph building, CAG generation, storage
lifecycle, or orchestration policy.

## Public Contract

- The API entrypoint remains `POST /admin/reprocess`.
- Supported targets remain `local`, `graph`, `cag`, and `all`.
- The canonical target executor is `execute_reprocess_target()` in
  `workflows.reprocess`.
- Backend selection is configured by `[workflows] backend`:
  - `direct`: execute the canonical target executor in-process.
  - `temporal`: submit `RagReprocessWorkflow` to Temporal.

## Boundaries

- `pipeline.sync` owns the concrete sync, Graphify, and CAG functions.
- `workflows.reprocess` owns target-to-owner-function dispatch only.
- Temporal is optional infrastructure. If selected, the service must be
  installed with the `temporal` extra and a worker must be running on the
  configured task queue.
- The orchestrator must call the RAG API rather than importing this package.

## Legacy Removal Rule

Do not keep per-endpoint target switches once the canonical executor covers a
target. API, workers, and tests must call the executor or submit the workflow.
