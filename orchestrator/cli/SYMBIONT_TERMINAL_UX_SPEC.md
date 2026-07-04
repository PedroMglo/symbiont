# Symbiont Terminal UX Spec

Owner: `orchestrator/cli`

`symbiont` is the primary interactive terminal entrypoint for local AI work.
It opens a persistent terminal session instead of requiring one shell command
per prompt.

## Contract

The terminal client may:

- send user prompts to the gateway `/query` API;
- consume streaming gateway events;
- consume `/agentic/tasks/{task_id}/timeline`;
- consume `/agentic/tasks/{task_id}/events`;
- consume `/agentic/tasks/{task_id}/events/stream`;
- render task, agentic, command, sandbox and diff evidence already recorded by
  the runtime ledger;
- install local launchers through `make aliases`.

The terminal client must not:

- execute feature, agent, storage or RAG behavior directly;
- infer file changes from assistant prose;
- read managed artifact bytes directly;
- duplicate feature-specific semantics, parsers or policy rules.

## Runtime UX

Running `symbiont` starts the private `~/.local/lib/ai-local/symbiont-tui chat`, a Rust
Ratatui/Crossterm/Tokio terminal client. Running `symbiont -l`, `symbiont
--live` or `symbiont --list` starts the private `symbiont-tui live`, a
separate read-only observability dashboard for background sessions and tasks.
The Python task watcher prototype is not a runtime path.

The terminal session provides:

- session header showing model, API URL and current directory;
- prompt input inside the session;
- live status for running tasks;
- mode selection for `smart`, `compact`, `verbose` and `raw`;
- task timeline snapshots in the same terminal;
- groups for task summary, agents/features/runtime activity, file changes,
  command runs, phases, events and prompt summaries;
- bounded colored diffs when the execution owner provides inline patch data;
- slash commands for `/help`, `/compact`, `/verbose`, `/raw`, `/smart`,
  `/open`, `/model`, `/diff`, `/clear` and `/exit`.

Groups are collapsed or expanded by the client state only. Collapsing a group
does not mutate the runtime ledger.

## Event Feed

`orchestrator/agentic` owns the event projection. It exposes normalized events
from the append-only ledger and command-run metadata:

- `GET /agentic/tasks/{task_id}/events?cursor=<seq>`
- `GET /agentic/tasks/{task_id}/events/stream?cursor=<seq>`

The stream provides `seq` cursors, task events, command cards, file diff
events, artifact events, approvals and agent/LLM summaries when present in the
ledger. The CLI may reconnect by cursor, but it must fall back to `/timeline`
if the stream is unavailable.

`symbiont-tui` reads `/query` SSE in the main path. When a task id is emitted,
it performs one bounded `/timeline` fetch and one bounded `/events` fetch by
cursor. Future live event streaming should extend this Rust event consumer
without adding agentic execution logic to the TUI.

## Live UX

`symbiont -l` is not a conversation surface. It answers: "what is happening in
the agentic runtime now?"

The live dashboard may:

- consume `GET /agentic/live/snapshot`;
- group tasks by `session_id`;
- show running, failed, recent and all filters;
- lazily consume `GET /agentic/tasks/{task_id}/timeline` only for the selected
  task;
- render selected-task file changes, command summaries and inline diffs already
  recorded by the runtime;
- open the selected task in the chat UX with `o` in the current terminal or
  `Enter` in a new terminal window when a supported terminal launcher is
  available.

The live dashboard must not:

- create tasks or execute agentic behavior;
- scan the workspace for diffs;
- fetch every task timeline in a loop;
- pass API keys through process argv;
- block the runtime or the chat UX.

Initial implementation uses one bounded snapshot refresh plus selected-task
timeline fetches. A future global SSE endpoint may replace polling, but it must
preserve the same read-only event projection contract.

## Performance Guard

The terminal UX is a renderer, not an executor. It must preserve the fast path
latency of the existing `@` alias:

- no default post-response watch loop;
- no waiting for a task to reach a terminal state unless the user explicitly
  runs `/watch` or passes a follow/live flag;
- no polling when a prompt produces no task id;
- live dashboard polling is global snapshot-only; no per-task timeline polling;
- at most one bounded quick timeline read after a streamed response by
  default;
- event reads must never block response token consumption;
- render is throttled to 10-20 FPS;
- frame rendering uses terminal size and dirty flags;
- empty activity groups such as `files (0)` and `commands (0)` must be hidden
  by default.

## Legacy Rule

The `@` alias may remain as a compatibility path, but the canonical user
entrypoint for interactive prompting is `symbiont`. Future UX work must extend
`crates/symbiont-tui` first. Do not reintroduce a second Python terminal
renderer as a compatibility layer.
