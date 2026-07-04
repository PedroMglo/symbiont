# Predictive Prewarming Spec

`prewarming` is orchestrator runtime infrastructure. It predicts which existing
agent, feature, or core service may be needed for a user request and asks the
container lifecycle manager to start it while normal request planning continues.
It is not the startup model warmup system in `orchestrator.core.warmup`, and it
is not a service, feature, agent, or direct-answer implementation.

Its project role is latency reduction at orchestration time: make likely
owners available earlier while preserving the same typed dispatch/API boundary
that the request would use without prewarming.

## Ownership

- Owns request-signal extraction, service-intent routing, score aggregation,
  resource-aware prewarm policy, per-request prewarm state, and hit/miss metrics.
- Does not own feature business logic, agent prompt behavior, storage lifecycle,
  or central service endpoint inference.
- Service names and container names must refer to live services from the runtime
  registry/config. Catalog entries without a live service are ghost entries.
- May call the orchestrator-owned lifecycle manager to request starts/stops, but
  must not import or execute another owner's private runtime behavior.
- May use neutral shared infrastructure such as `sharedai` payload/transport
  helpers. Shared helpers must not carry prewarming policy or feature semantics.

## Runtime Flow

The gateway schedules prewarming as a best-effort background task as soon as a
request enters the request path, before LLM planning and dispatch. The normal
request path continues independently.

The engine pipeline is:

1. `SignalExtractor` derives cheap request signals from text and attachment
   names.
2. `DirectAnswerGuard` skips the whole pipeline only for requests that are very
   likely answerable without tools, and only when no service/attachment signal is
   present.
3. `RuleRouter` produces deterministic catalog matches from keywords, patterns,
   file extensions, and negative gates.
4. A high-confidence rule match may request a lifecycle start immediately.
5. Level 1 semantic routing uses either `FastEmbedRouter` or `LightweightRouter`;
   optional Ollama embeddings are controlled by `PrewarmConfig.level1_enabled`.
6. `SemanticRouterAdapter` runs only when Level 1 is ambiguous.
7. `MicroClassifier` runs only for ambiguous or weak cases and is timeout-capped.
8. `Aggregator` merges router scores with running-container, recency, startup
   cost, and GPU-pressure signals.
9. `PolicyEngine` applies per-feature thresholds and resource limits before
   lifecycle starts.
10. `PrewarmState`, `PrewarmMetrics`, and `LearningLoop` track hit/miss outcomes
    after dispatch calls `mark_used`.

All errors are non-fatal to the user request. If prewarming fails, routing and
dispatch must still behave as if prewarming did not exist.

## Service Intent Catalog

`catalog.toml` describes durable service intent:

- `container_name`: lifecycle-facing service identity that must match the live
  runtime registry/lifecycle map.
- `description`: stable one-sentence role of the service.
- `capabilities`: domain-neutral capabilities that the service exposes.
- `inputs`: input types the service can process.
- `operations`: verbs/tasks the service performs.
- `keywords`, `patterns`, and `file_extensions`: cheap deterministic signals.
- `example_queries`: optional documentation/eval hints only.
- `prewarm_policy`: `standard`, `aggressive`, `conservative`, or `never`.
- `prewarm_threshold`, `startup_cost`, `uses_gpu`, `ttl_idle`, and `priority`:
  resource-policy inputs, not service behavior.

Semantic routers must build embeddings/vectors from service intent documents,
not from `example_queries`. Prompt examples are allowed only as non-authoritative
documentation or external eval data, because they bias predictions toward a
small set of phrasings.

Catalog entries must be scenario-neutral. They can describe generic service
capabilities, inputs, operations, identifiers, file types, and durable negative
gates. They must not encode benchmark answers, one-off demo phrasings, hidden
routing shortcuts, or feature-specific parsers.

Direct-answer behavior is owned by `reasoning_and_response`. The prewarming
catalog must not reintroduce a separate direct-answer service or no-service
runtime implementation.

## Runtime Contract

- Prewarming is best-effort and must never block the main request path.
- The engine must be conservative under GPU/resource pressure.
- Direct-answer guard blocks simple requests only when there is no service or
  attachment signal.
- Features marked `prewarm_policy = "never"` are intentionally excluded and must
  have a reason in the catalog or surrounding docs.
- Cleanup and hit/miss tracking must preserve request/session correlation.
- The lifecycle-facing service name is derived from the catalog feature id when
  starting/stopping containers. Any mismatch with the lifecycle service map must
  be fixed through the catalog/registry contract, not by adding service-specific
  branches in the engine.
- Ports, URLs, health endpoints, storage paths, and machine-derived runtime
  values belong to `config/`/runtime registries, not to `catalog.toml`.
- Configuration knobs belong in root `config/` and `orchestrator.config`.
  Prewarming modules may read `PrewarmConfig`, but must not create private env or
  machine-config islands.
- Storage-related predictions may only start or mark the `storage_guardian`
  owner. Archive, restore, custody, manifests, and storage policy remain owned by
  `storage_guardian`.
- RAG/research predictions may only start or mark the RAG/research owner.
  Retrieval, ingestion, graph, and enrichment behavior remain owned by
  `obsidian-rag`.

## Verification

- For catalog/router changes, run targeted prewarming tests and `ruff check` on
  `orchestrator/prewarming`.
- For gateway or lifecycle integration changes, add targeted orchestrator tests
  around `/prewarm/status` or lifecycle start behavior.
- For config changes, cover `PrewarmConfig` parsing/defaults and any profile or
  resolver surface that exposes the knob.
- For resource-policy changes, test GPU limits, already-running behavior,
  threshold decisions, and false-positive cleanup where relevant.
