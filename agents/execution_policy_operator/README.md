# Execution Policy Operator

`execution_policy_operator` is the consolidated owner for deterministic command
and shell safety evidence.

It owns the `bash_safety` provider and exposes the stable `/v1/bash/*` API for
callers that need static shell review or single-command risk classification.

This agent does not execute commands, mutate workspaces, approve actions, or
publish artifacts. It classifies risk and returns structured evidence for the
orchestrator policy layer and sandbox runtime.

Execution remains owned by `workspace_execution`. Approval and completion
remain owned by `orchestrator/agentic`.
