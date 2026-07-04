# Task Timeline UX Spec

Owner: `orchestrator/agentic`

The agentic runtime must expose enough task state for a terminal UX to show
what is happening while a background task runs. The UX is not allowed to infer
progress from assistant prose.

## Contract

`GET /agentic/tasks/{task_id}/timeline` returns a compact operational view of
the existing ledger:

- task id, trace id, status, source and goal preview;
- elapsed seconds and terminal duration when available;
- ordered events with actor, timestamp and gap since previous event;
- runs, steps, tool calls and command runs with durations;
- command output refs, hashes, redaction/truncation status and artifact refs;
- file activity from workspace execution diff files and artifact descriptors,
  including bounded inline patch previews when the execution owner provides
  them;
- material activity projected from `ai_local.material.*` ledger events,
  including phase/status, latency source, validation profile, command refs,
  issue bundle IDs, target resolution, patch diagnostics, runtime metadata,
  cleanup evidence, model lane metrics and artifact hashes;
- completion state, active phase and counts.

The endpoint summarizes ledger data only. It must not execute work, call owner
services, read raw artifact contents, or own storage/diff semantics.

## Terminal UX

The `@` alias must treat `/tasks` as an operational command, not as model text:

- `@ /tasks` lists recent agentic tasks.
- `@ /tasks <task_id>` renders the timeline once.
- `@ /tasks <task_id> --watch` refreshes until the task is terminal.
- `@ /tasks <task_id> --json` prints the raw timeline JSON.

For normal interactive prompts, `@` must automatically follow the agentic task
timeline in the same terminal when the gateway emits task metadata. The user
must not need a second terminal to see operational progress. The follow mode
renders snapshots during pipeline milestones and then follows the task until a
terminal status. `--no-follow` may disable this only for explicit operator
needs such as compact output, demos or debugging.

The viewer may display refs, per-file diff summaries and owner-provided inline
patch previews, but it must not fetch or persist managed artifacts directly.
Durable artifact publication remains owned by `storage_guardian`.

## Extinction Rule

The previous behavior where `/tasks` entered the normal chat/query path is retired.
Once this spec is implemented, `/tasks` must be handled by the operational
viewer before `/query` is called.
