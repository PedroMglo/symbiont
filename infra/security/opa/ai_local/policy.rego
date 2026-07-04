package ai_local.agentic

default decision := {
  "decision": "allow",
  "reason": "Low-risk read-only or generation action allowed",
  "requires_approval": false,
  "dry_run_required": false,
  "evidence_required": false,
  "lease_required": false,
  "max_risk": "high",
}

decision := {
  "decision": "deny",
  "reason": "OPA policy denies the action without explicit override",
  "requires_approval": false,
  "dry_run_required": true,
  "evidence_required": true,
  "lease_required": true,
  "max_risk": "high",
} if {
  input.action.risk_level == "deny"
}

decision := {
  "decision": approval_decision,
  "reason": "OPA policy requires approval and dry-run evidence for high-risk action",
  "requires_approval": true,
  "dry_run_required": true,
  "evidence_required": true,
  "lease_required": true,
  "max_risk": "high",
} if {
  input.action.risk_level == "high"
  approval_decision := "would_require_approval"
  input.context.policy_mode != "enforce"
}

decision := {
  "decision": "require_approval",
  "reason": "OPA policy requires approval and dry-run evidence for high-risk action",
  "requires_approval": true,
  "dry_run_required": true,
  "evidence_required": true,
  "lease_required": true,
  "max_risk": "high",
} if {
  input.action.risk_level == "high"
  input.context.policy_mode == "enforce"
}

decision := {
  "decision": "allow",
  "reason": "OPA policy allows medium-risk action with audit evidence",
  "requires_approval": false,
  "dry_run_required": false,
  "evidence_required": true,
  "lease_required": false,
  "max_risk": "high",
} if {
  input.action.risk_level == "medium"
}
