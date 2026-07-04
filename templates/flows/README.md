# Flow Documentation Templates

Use these templates for behavior that crosses owner boundaries.

## Choose A Template

| Flow type | Template |
| --- | --- |
| Full user prompt, agentic task, material generation, repair, publication, final answer | [end-to-end-loop-template.md](end-to-end-loop-template.md) |
| Predictive prewarming, service intent routing, lifecycle start policy | [prewarming-doc-template.md](prewarming-doc-template.md) |

## Flow Page Rules

- Start with the user prompt shape or triggering event.
- List every participating owner and the contract between them.
- Use at least one sequence diagram.
- Describe success, partial success, fail-closed behavior, and approvals.
- Keep evidence explicit: traces, events, object refs, hashes, logs, tests.
- Do not move behavior into the coordinator just because the flow is documented
  end to end.
