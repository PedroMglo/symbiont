# Security

`infra/security/` is the canonical owner for permission and security
governance in this repository.

This directory owns declarative policy material such as:

- API authentication, authorization and rate-limit defaults;
- orchestrator security toggles and command allowlists;
- action risk classification used by the agentic policy engine;
- OPA/Rego policy bundles and parity artifacts;
- repo-level permission ownership rules.

Runtime owners still implement and enforce their local contracts. For example,
`orchestrator/` evaluates agentic policy, `features/workspace_execution/` owns
sandbox execution, and service-local `security.py` helpers may keep request
validation close to the service. Those owners must consume permission policy
from `infra/security/` instead of introducing new declarative security sources
elsewhere.

Real local secrets stay in `infra/docker/secrets/` and are ignored by Git.
Non-secret examples and required filenames are documented in
`infra/docker/secrets.example/`.

Run `python scripts/validate_security_ownership.py` after adding policy files.
