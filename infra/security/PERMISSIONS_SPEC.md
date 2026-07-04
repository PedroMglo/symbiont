# Permissions Ownership Spec

Date: 2026-06-22.

## Ownership

All declarative permission policy is owned by `infra/security/`.

Permission policy includes authentication defaults, authorization scopes,
command allowlists, action risk matrices, OPA/Rego bundles, sandbox permission
rules, security governance YAML/TOML, and public permission contracts.

## Runtime Boundaries

Runtime code may enforce permissions in its local owner when that enforcement is
part of the domain contract:

- `orchestrator` owns policy input assembly, decision recording and request
  gates.
- `features/workspace_execution` owns sandbox execution behavior.
- `agents/execution_policy_operator` owns read-only command risk evidence.
- service-local `security.py` modules may validate tokens, paths, uploads or
  request headers for that service.

Those modules must not become the source of truth for shared permission policy.
If a rule can affect more than one owner, add it under `infra/security/` and
make the runtime owner consume it.

## Canonical Files

- `api-security.yaml`: API authentication, token, CORS, redaction and audit
  defaults.
- `orchestrator.toml`: orchestrator security toggles and command allowlists.
- `policy-actions.toml`: policy action risk matrix consumed by
  `orchestrator.agentic.policy_registry`.
- `opa/`: OPA/Rego policy bundles.

## Guardrail

`scripts/validate_security_ownership.py` fails when new declarative permission
files are introduced outside `infra/security/`. Local implementation helpers
named `security.py` are allowed only as enforcement adapters, not as shared
policy stores.
