# Owner Documentation Templates

Use these templates for documentation owned by a single component, package,
service, feature, agent, or runtime owner.

## Choose A Template

| Owner type | Template |
| --- | --- |
| Generic component or owner area | [component-doc-template.md](component-doc-template.md) |
| Agent under `agents/*` | [agent-doc-template.md](agent-doc-template.md) |
| Feature under `features/*` | [feature-doc-template.md](feature-doc-template.md) |
| Runtime service/API/worker | [service-doc-template.md](service-doc-template.md) |
| Central config owner | [config-doc-template.md](config-doc-template.md) |
| Durable storage authority | [storage-guardian-doc-template.md](storage-guardian-doc-template.md) |

## Owner Page Rules

- Name the owner path in the header.
- Say what the owner does and what it must not do.
- Link to source, config, manifests, tests, and runtime evidence.
- Include API/CLI usage when the owner exposes one.
- Include failure modes and recovery ownership.
- End with verification and open questions.

## Common Owner Boundaries

| Owner | Owns | Does not own |
| --- | --- | --- |
| `agents/*` | prompt/task reasoning and typed responses | durable writes, shell, Docker, storage lifecycle |
| `features/*` | domain API/pipeline behavior | gateway policy, hidden storage writes |
| `config/` | source-of-truth settings and generated compatibility outputs | runtime behavior or side effects |
| `storage_guardian/` | managed writes, archive/restore, custody | feature logic, central config inference |
| `orchestrator/` | routing, policy, ledger, dispatch | feature internals or direct command execution |
| `obsidian-rag/` | retrieval, ingestion, graph/CAG | agentic policy or storage lifecycle |
