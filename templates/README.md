# Documentation Templates

Use this folder as the single source for new project documentation templates.
The goal is uniform documentation across owners, components, services, runtime
flows, prewarming, and end-to-end repair loops without forcing every page into
the same shape when the owner semantics are different.

Start with [INDEX.md](INDEX.md) when choosing a template.

## Folder Structure

```text
templates/
  README.md
  INDEX.md
  owners/
    README.md
    component-doc-template.md
    agent-doc-template.md
    feature-doc-template.md
    service-doc-template.md
    config-doc-template.md
    storage-guardian-doc-template.md
  flows/
    README.md
    end-to-end-loop-template.md
    prewarming-doc-template.md
```

## Which Template To Use

| Need | Template | Why this template exists |
| --- | --- | --- |
| Any component, package, module, service, feature, or agent | [owners/component-doc-template.md](owners/component-doc-template.md) | Shared baseline for ownership, behavior, usage, architecture, contracts, operations, verification, and status. |
| Central config, resolver, profiles, generated envs, schemas, runtime knobs, or config-owned policy catalogs | [owners/config-doc-template.md](owners/config-doc-template.md) | Config owns source-of-truth settings, precedence, validation, generated compatibility artifacts, secrets boundaries, and drift control. |
| Agent under `agents/*` | [owners/agent-doc-template.md](owners/agent-doc-template.md) | Agents own reasoning/prompt/task behavior and must not own durable side effects. |
| Feature under `features/*` | [owners/feature-doc-template.md](owners/feature-doc-template.md) | Features expose domain APIs/pipelines and often call storage, RAG, workspace execution, or agents by contract. |
| Runtime service, infra service, HTTP API, worker, Docker profile, or daemon | [owners/service-doc-template.md](owners/service-doc-template.md) | Services need lifecycle, API, ports, health, storage, observability, and operator docs. |
| Storage Guardian, managed storage, archive/restore, object upload, materialization, custody, or storage schema docs | [owners/storage-guardian-doc-template.md](owners/storage-guardian-doc-template.md) | Storage Guardian is the durable-write authority and needs stronger custody, manifest, hash, restore, and safety documentation than a normal service. |
| Predictive prewarming catalog/router/policy docs | [flows/prewarming-doc-template.md](flows/prewarming-doc-template.md) | Prewarming is runtime latency infrastructure, not a feature or direct-answer service. |
| Cross-component request flow, agentic loop, material generation, validation, repair, final answer | [flows/end-to-end-loop-template.md](flows/end-to-end-loop-template.md) | Full flows need sequence diagrams, failure/repair loops, evidence, and ownership boundaries across many components. |

## Documentation Rules

- Start from the nearest template and delete sections that truly do not apply.
- Keep the owner explicit. A page must say who owns behavior and who is only
  called by contract.
- Separate `implemented`, `enabled by default`, `opt-in`, `blocked`, and
  `not implemented`.
- Include at least one Mermaid diagram when more than one component participates.
- Describe the user-facing flow and the operator-facing flow separately.
- Document safe usage, dangerous usage, and expected failure modes.
- Cite local source files, manifests, specs, tests, and smoke commands that can
  prove the current status.
- Do not copy another owner's internals into a page as if they belonged to the
  current owner.
- Do not encode scenario-specific prompts, benchmark answers, or one-off
  examples as runtime truth.
- Every generated Markdown page should keep a `Page Index` near the top. Update
  it whenever headings are added, removed, or renamed.

## Recommended Page Header

```markdown
# <Component Or Flow Name>

Status: <implemented | enabled-by-default | opt-in | draft | blocked>
Owner: `<path-or-owner>`
Last verified: <YYYY-MM-DD>
Applies to: `<paths>`, `<services>`, `<profiles>`
Audience: <developer | operator | user | maintainer>
```

## Status Vocabulary

| Status | Meaning |
| --- | --- |
| `implemented` | Code or configuration exists, but may not be active by default. |
| `enabled-by-default` | Active in the normal local runtime path. |
| `opt-in` | Requires an explicit profile, config flag, command, or endpoint. |
| `proven-live` | Verified against a running stack or smoke test, with evidence. |
| `blocked` | Known blocker prevents normal use. Include the blocker and owner. |
| `retired` | Still present only during a dated removal window. Include removal path. |

## Diagram Standard

Use Mermaid diagrams directly in Markdown. Prefer:

- `flowchart` for ownership, dependencies, lifecycle, and policy gates.
- `sequenceDiagram` for request, validation, repair, and user-facing flows.
- `stateDiagram-v2` for runtime states, retries, and repair arbitration.

Every diagram should name real owners, not vague boxes like "system" unless the
page is intentionally abstract.

## Validation Footer

Every finished document should end with this section:

```markdown
## Verification

| Check | Command or source | Expected result | Last run |
| --- | --- | --- | --- |
| Static docs check | `git diff --check -- <doc-path>` | no whitespace errors | <date or not-run> |
| Owner tests | `<command>` | `<expected>` | <date or not-run> |
| Runtime smoke | `<command>` | `<expected>` | <date or not-run> |

## Open Questions

- <question, owner, or decision still pending>
```
