# Permission And Output Ledger Spec

Owner: `orchestrator/agentic`

This spec defines how the agentic runtime records governed command execution
without turning the orchestrator into a storage owner or a hidden command
implementation.

## Boundaries

- `agents/execution_policy_operator` owns command risk classification.
- `features/workspace_execution` owns writable workspace execution when a task
  needs material generation.
- `storage_guardian` owns durable managed-storage writes and artifact lifecycle.
- `orchestrator/agentic` owns policy decisions, approvals, sessions, task
  ledger events, and references to outputs produced by owner services.

The orchestrator must not persist full command output inside tool-call payloads.
Tool calls may contain references, hashes, byte sizes, truncation status,
redaction status, exit status, and artifact references.

## Output Contract

Each executed command run records:

- `stdout_ref` and `stderr_ref`.
- `stdout_sha256` and `stderr_sha256` over the redacted ledger stream.
- `stdout_size_bytes` and `stderr_size_bytes` over the redacted ledger stream.
- `output_truncated`.
- `redaction_status`.

When `workspace_execution` provides owner refs, those refs are preserved. When
the local process sandbox is used, refs are ledger-local logical refs in the
form `agentic-command-run:<run_id>:stdout` and
`agentic-command-run:<run_id>:stderr`.

The command run table may keep bounded previews for operator inspection. Those
previews are not a durable artifact API and must not be copied into tool-call
output payloads.

## Metadata Injection Defense

User or model supplied session metadata is public metadata only. The command
service must discard metadata keys that attempt to inject internal state,
including:

- keys starting with `_`;
- keys starting with `workspace_execution_`;
- keys starting with `internal_`;
- runtime keys such as `backend`, `mounts`, `classification`, `approval_id`,
  `policy_decision`, `command_run_id`, `session_id`, `run_id`, `task_id`, and
  `trace_id`.

System metadata is added after public metadata, so user/model input cannot
override owner-provided runtime fields.

## Extinction Rule

After this contract exists, raw `stdout`/`stderr` fields in
`agentic_tool_calls.output_preview` are forbidden. Consumers must read command
run previews or referenced owner artifacts instead.
