# Tool Execution Boundary Spec

Owner: `orchestrator/agentic`.

The tool execution boundary is the deterministic gate between typed
`AgentDecision` proposals and any runtime adapter that can call an agent,
feature, RAG, workspace sandbox, storage owner, or command tool.

## Rules

- Runtime adapters may execute only actions currently present in
  `AgentState.pending_actions`.
- Pending actions are produced only by a valid `AgentDecision` whose
  `input_state_hash` matched the current state when recorded.
- The runner must record an `agent.action.boundary_checked` event before it
  calls a tool adapter.
- Boundary check events include the current `state_hash`, `action_id`,
  `action_type`, `capability_id`, `policy_action`, owner, and whether sandbox
  proof is required.
- Shell command actions must cross the governed command service, and that
  service must use the `workspace_execution` backend before a command can run.
- API actions must resolve to registered capability/service metadata, not raw
  URLs.
- Policy checks, approval handling, resource lease checks, and sandbox command
  proof must happen before owner transport or command execution.

## Boundary

This gate validates orchestration preconditions only. It must not implement
storage lifecycle, feature behavior, RAG behavior, command risk classification,
or sandbox execution. Those remain owned by `storage_guardian`, feature/RAG
services, `agents/execution_policy_operator`, and `workspace_execution`.
