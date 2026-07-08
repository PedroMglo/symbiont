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

## Local Security Gates

`make verify-security` is the repo-level local security gate. It installs no
project behavior and requires scanners from the operator PATH, including
`detect-secrets`, `gitleaks`, `semgrep`, `cargo-audit`, and `cargo-deny`.

Secret scanning uses the audited root `.secrets.baseline`. Current baseline
classification is documented in `infra/security/detect-secrets-baseline-audit.md`.
New `detect-secrets` findings must be removed or audited before the gate passes.

Container image CVEs are tracked separately when the check needs already-built
runtime images. Accepted image risks live in
`infra/security/risk-acceptance.json` with short review windows and must name
the affected image, scanner, vulnerability IDs, compensating controls, and the
Trivy command/report that will detect the issue again.
