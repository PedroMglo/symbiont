# OPA Policy Input Spec

Data: 2026-06-15.

## Ownership

`orchestrator` owns policy enforcement. Owners publish capability
metadata and evidence. The orchestrator reduces that evidence into a policy
input and records the decision in the agentic ledger.

## Canonical Input

```json
{
  "actor": {
    "kind": "agent",
    "id": "reasoning_and_response"
  },
  "action": {
    "policy_action": "command.execute",
    "capability_id": "workspace.command",
    "owner": "workspace_execution",
    "risk_level": "high",
    "writes_allowed": false,
    "dry_run_supported": true,
    "rollback_supported": false
  },
  "evidence": {
    "execution_policy": {},
    "storage_scope": {},
    "resource_lease": {},
    "approval": {}
  },
  "context": {
    "profile": "supervised",
    "autonomy_mode": "supervised",
    "policy_mode": "enforce",
    "dry_run": false
  }
}
```

## Decision Contract

OPA and the current Python policy backend must reduce to the same public
decision shape before a migration can remove Python policy rules:

- `allow`
- `deny`
- `require_approval`
- `reason`
- `evidence_required`
- `lease_required`
- `max_risk`

## Legacy Removal Rule

Python policy tables may only be removed action group by action group, after
golden decisions prove OPA parity. Command risk classification belongs to
`agents/execution_policy_operator`; the orchestrator may keep only transport,
policy input assembly and decision recording.
