# ai-local Terminal UX

`symbiont-tui` is the current Rust/Ratatui terminal UX for `ai-local`.

The V3.2 direction is to evolve this owner into a Codex-like user CLI instead
of keeping the `symbiont` command as the long-term user experience center.

## Ownership

- Owner type: user CLI/TUI client.
- Owning repo: `ai-local`.
- Owning directory: `crates/symbiont-tui`.
- Runtime package: Rust crate `symbiont-tui`.
- External callers: user terminal and install aliases such as `@`.
- Data owned: terminal rendering, input handling, local command-line flags,
  prompt history UI, slash-command UI, live event views and approval UI.
- Data read but not owned: orchestrator sessions, agentic task events, material
  session events, command output refs, file change refs, model/profile status
  and policy decisions.
- Data not owned: orchestrator routing, task lifecycle, policy decisions,
  command execution, filesystem writes, storage, model routing and service
  semantics.

## Codex-Like UX Direction

Borrow UX patterns:

- full-screen stream of turns, items, command outputs and file changes;
- slash commands for status, resume, fork, diff, approvals, permissions, model,
  plan, goal, usage, raw events and diagnostics;
- queued follow-up prompts;
- interrupt and steer for long turns;
- JSONL non-interactive event mode;
- session resume and fork;
- clear distinction between UI, app/session API and execution owners.

Do not borrow:

- host execution permissions;
- direct Docker access;
- hidden Codex CLI subprocess;
- runtime decisions hardcoded in the client.

## Migration Rule

The alias `@` remains the user entry contract during migration. The old
`symbiont` branding or command should be deleted after a Codex-like CLI reaches
parity and docs/install aliases are migrated. Do not keep permanent wrappers
without a live caller and deletion gate.
