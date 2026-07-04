# Capability Search And Command Registry Spec

Owner: `orchestrator/agentic`.

This spec defines two read-only orchestration surfaces:

- `CapabilitySearch`, which searches public `RuntimeToolEnvelope` metadata.
- `CommandRegistry`, which maps terminal alias slash commands to declared
  owner/API/capability targets.

Both surfaces are metadata and routing contracts only. They must not execute
service behavior, import owner packages, parse owner-domain payloads, infer
storage/RAG/feature semantics, or replace owner APIs. Execution still crosses
the existing policy, dispatch/API, command sandbox, and owner boundaries.

## CapabilitySearch

Inputs:

- public `RuntimeToolEnvelope` records built from owner-published manifests;
- user query text or explicit `select:<capability_id>`.

Search fields:

- `capability_id`;
- `owner`;
- `kind`;
- `service_name`;
- `description`;
- `capabilities`;
- `policy_action`;
- `risk_level`;
- `evidence_types`;
- `schema_refs`;
- `supported_action_types`;
- `events_published`;
- safe transport metadata such as service, method and path.

Rules:

- Results return public envelopes only.
- Results must not expose execution handles, callables, import paths, local
  filesystem paths, tokens, passwords, API keys, auth headers or secrets.
- Ranking is deterministic and explainable through matched fields.
- `select:<capability_id>` bypasses fuzzy ranking and returns the exact
  capability when it exists.
- Missing matches return an empty result set, not a fallback capability.

## CommandRegistry

Inputs:

- `orchestrator/capabilities/command_registry.toml`.

Registry entries:

- slash command name, for example `/doctor`;
- optional aliases;
- owner;
- description;
- target type and target metadata;
- optional `capability_id`;
- policy action;
- read-only flag;
- evidence types.

Allowed target types:

- `api`: a read-only orchestrator/owner API route;
- `make`: a project Make target;
- `capability`: a declared capability selected by `capability_id`.

Rules:

- The registry is read-only metadata. Selecting a command does not execute it.
- Target metadata must stay declarative and must not contain host-local paths
  or secrets.
- The gateway/local alias may use the registry to identify slash commands, but
  execution remains a later policy-gated flow.
- Hidden Python command lists must not be added when a registry entry can
  describe the command.

## Legacy Extinction Rule

When a slash command or capability route is covered by this registry/search
surface, future runtime code must consume the registry/envelope instead of
adding hardcoded gateway, CLI or routing vocabularies. Any new compatibility
entry must document a live caller and a removal phase.
