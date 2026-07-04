# Reasoning And Response Agent

Super-agent for read-only cognitive provider modes that share the same
reasoning loop, model lanes and side-effect boundary.

Phase 3 starts with the `synthesize` provider only. Other thin providers remain
on their current services until their parity gates pass.

## API Contract

Base internal URL: `https://reasoning-and-response:8000`

Protected endpoints require `Authorization: Bearer <service-token>`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Healthcheck |
| GET | `/v1/reasoning/capabilities` | Advertised provider modes |
| POST | `/v1/reasoning/synthesize` | Combine structured source outputs |

Provider requests accept optional `language_context` either as a top-level
field or inside `metadata`. The agent uses it only for language policy in
user-facing wording: original user text remains the source of truth, internal
contracts stay in English, and this owner does not choose routing from language
signals.

## Ownership

This owner may provide direct response, decomposition, synthesis, critique and
classification modes over time. It does not own orchestrator routing policy,
feature behavior, storage writes, RAG internals, sandbox execution or shell
safety.

Current provider modes:

- `agent.reasoning_and_response.respond`
- `agent.reasoning_and_response.decompose`
- `agent.reasoning_and_response.synthesize`
- `agent.reasoning_and_response.critique`
- `agent.reasoning_and_response.classify`
