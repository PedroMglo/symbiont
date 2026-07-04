# Documentation Template Index

Status: implemented
Owner: `templates/`
Last verified: 2026-06-29
Applies to: `templates/owners/*`, `templates/flows/*`
Audience: developer, maintainer

## Page Index

- [Purpose](#purpose)
- [Template Map](#template-map)
- [Selection Rules](#selection-rules)
- [Verification](#verification)
- [Open Questions](#open-questions)

## Purpose

This index is the entrypoint referenced by `templates/README.md`. Use it to
choose the nearest official template before creating or rewriting hand-written
project documentation.

## Template Map

| Documentation need | Use this template |
| --- | --- |
| Repo component, package, module or general owner page | `templates/owners/component-doc-template.md` |
| Runtime service, API, worker, daemon, Docker profile or operator surface | `templates/owners/service-doc-template.md` |
| Agent under `agents/*` | `templates/owners/agent-doc-template.md` |
| Feature under `features/*` | `templates/owners/feature-doc-template.md` |
| Central config, resolver, schemas, generated envs or runtime knobs | `templates/owners/config-doc-template.md` |
| Storage Guardian, custody, archive/restore or managed writes | `templates/owners/storage-guardian-doc-template.md` |
| Cross-component request/material/repair/final-answer flow | `templates/flows/end-to-end-loop-template.md` |
| Prewarming, service-intent prediction or lifecycle latency docs | `templates/flows/prewarming-doc-template.md` |

## Selection Rules

- Pick the closest owner or flow template.
- Keep the `Page Index` embedded in the Markdown page.
- Delete template sections only when they truly do not apply.
- When a document spans owners, prefer a flow template and keep the ownership
  map explicit.
- Generated reports may use generator-owned structure, but hand-written docs
  under `docs/` should declare the template they follow.

## Verification

| Check | Command or source | Expected result | Last run |
| --- | --- | --- | --- |
| Static docs check | `git diff --check -- templates/INDEX.md` | no whitespace errors | 2026-06-29 |

## Open Questions

- Should the repo add an automated template-section compliance checker?
