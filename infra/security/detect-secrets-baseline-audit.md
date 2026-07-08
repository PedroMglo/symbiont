# detect-secrets Baseline Audit

Generated from tracked repository files with `detect-secrets scan` and audited on 2026-07-06.
All current entries in `.secrets.baseline` are marked `is_secret: false` after redacted line review.
This file intentionally records categories and paths only; it does not contain secret values.

## Summary

- Files with findings: 47
- Findings audited as false positives: 105
- Findings audited as real secrets: 0

## Finding Types

- AWS Access Key: 1
- Base64 High Entropy String: 17
- Basic Auth Credentials: 1
- GitHub Token: 2
- Hex High Entropy String: 2
- JSON Web Token: 3
- Secret Keyword: 79

## Audit Buckets

- configuration keys and secret-file references: 36
  - .github/public-export/policies/private-denylist.yml
  - agents/audio_transcribe/config.toml
  - config/docker/compose-projects.toml
  - config/docker/service-catalog.toml
  - config/models/orc.config.json
  - config/orc/observability.toml
  - config/rag/internal.toml
  - config/resolver.py
- documentation describing secret handling: 2
  - docs/owners/obsidian-rag.md
  - obsidian-rag/docs/observability.md
- operator scripts using placeholder values: 5
  - scripts/docker_policy.py
  - scripts/workspace_vm_smoke.py
- runtime code naming env vars or redaction markers: 12
  - crates/symbiont-tui/live.rs
  - features/personal_context/personal_context/email.py
  - obsidian-rag/rag_config.py
  - orchestrator/config.py
  - orchestrator/observability/events.py
  - orchestrator/registry.py
- test fixtures and redaction assertions: 50
  - features/translation/tests/test_api_auth.py
  - features/translation/tests/test_security_redaction.py
  - features/voice_runtime/tests/test_gateway_contract.py
  - features/workspace_execution/tests/test_contracts.py
  - tests/config/test_resolver.py
  - tests/config/test_verify_install.py
  - tests/orchestrator/features/extrator/test_config.py
  - tests/orchestrator/features/personal_context/test_config_private.py

## Ongoing Gate

- `scripts/security/run_security_audit.sh` must run `detect-secrets scan --baseline .secrets.baseline`.
- `scripts/security/summarize_security_reports.py` must fail if the baseline contains unknown or real-secret entries.
- New findings must be removed or explicitly audited before the security gate passes.
