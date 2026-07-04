# Resolver contracts

`config.resolver` is the source of truth for generated runtime configuration.
It owns shape, inference, validation and explanation only. Services remain the
owners of their behavior.

## Resolver JSON

| Surface | Contract | Consumer | Notes |
| --- | --- | --- | --- |
| `python -m config.resolver --print` | `ai-local.config-resolver.v1` | automation, UI, ledger and diagnostics consumers | Top-level typed config output. |
| `python -m config.resolver --health-report` | `ai-local.config-health.v1` | `orchestrator /health`, UI and ledger diagnostics | Short status object; consumers must not reinterpret service behavior here. |
| `python -m config.resolver --self-model` | `ai-local.operational-self-model.v1` | Resource Governor, prewarming, routing, UI and diagnostics surfaces | Derived capacity/status model; consumers must not treat it as behavior ownership. |

Every decision in `decisions` carries:

- `decision_id`
- `field`
- `value`
- `status`
- `confidence`
- `origin`
- `inputs`
- `probes`
- `reason`
- `formula`
- `override`
- `warning`
- `downstream_impact`

Valid high-level config health states are `ready`, `degraded`, `blocked`,
`external_missing`, `local_fallback` and `stale`.

## Generated Env Schemas

Generated env files are transition artifacts. They are not source-of-truth
config islands.

| Artifact | Contract env | Version env | Contract | Consumer | Sunset |
| --- | --- | --- | --- | --- | --- |
| `.env.storage.generated` | `AI_LOCAL_STORAGE_ENV_CONTRACT` | `AI_LOCAL_STORAGE_ENV_CONTRACT_VERSION` | `ai-local.storage-env.v1` | Docker Compose storage binds and `storage_guardian` | Replace after storage consumers read typed resolver output. |
| `.env.llm.generated` | `AI_LOCAL_LLM_ENV_CONTRACT` | `AI_LOCAL_LLM_ENV_CONTRACT_VERSION` | `ai-local.llm-env.v1` | LLM serving Compose services, orchestrator and RAG model loaders | Replace after LLM consumers read typed resolver output. |
| `.env.services.generated` | `AI_LOCAL_SERVICES_ENV_CONTRACT` | `AI_LOCAL_SERVICES_ENV_CONTRACT_VERSION` | `ai-local.services-env.v1` | Docker Compose service wiring, orchestrator, agents, features and RAG loaders | Replace after service consumers read typed resolver output. |
| `.env.docker.resources.generated` | `AI_LOCAL_DOCKER_RESOURCES_ENV_CONTRACT` | `AI_LOCAL_DOCKER_RESOURCES_ENV_CONTRACT_VERSION` | `ai-local.docker-resources-env.v1` | Docker Compose resource limits, lifecycle parallelism and cache caps | Replace after Compose resource policy and infra lifecycle read typed resolver output. |

Contract v2 must include an explicit consumer migration. A v1 key can disappear
only when the live consumer path no longer reads it or a documented transition
adapter owns the sunset.

Docker lifecycle keys currently emitted through the v1 Docker resources env
include `DOCKER_BUILDKIT`, `COMPOSE_PARALLEL_LIMIT`,
`AI_LOCAL_COMPOSE_PARALLEL_LIMIT`, `AI_LOCAL_DOCKER_BUILD_CACHE_MAX`,
`AI_LOCAL_DOCKER_UP_NO_BUILD`, `AI_LOCAL_DOCKER_UP_WAIT`,
`AI_LOCAL_DOCKER_UP_WAIT_TIMEOUT` and `AI_LOCAL_DOCKER_REMOVE_ORPHANS`.

## Runtime Output Contracts

| Surface | Contract | Consumer |
| --- | --- | --- |
| `resolved.rag_runtime` | `ai-local.rag-runtime.v1` | `obsidian-rag` runtime and Compose env generation. |
| `resolved.symbiont_runtime` | `ai-local.symbiont-runtime.v1` | orchestrator runtime, dispatch and agentic service wiring. |
| `resolved.command_runtime` | `ai-local.command-runtime.v1` | agentic command sandbox/runtime tooling. |
| `resolved.docker_resources` | `ai-local.docker-resources.v1` | Docker Compose resource env generation and governance checks. |
| `.local/generated/resource_governor_policy.json` | `resource-governor.v1` | orchestrator Resource Governor and pressure gates. |
| `.local/generated/autotuning.effective.json` | `ai-local.autotuning-effective.v1` | `config.resolver` Resource Governor overlay loader. |
| `resolved.operational_self_model` | `ai-local.operational-self-model.v1` | Resource Governor, prewarming, routing, UI and diagnostics surfaces. |

## Controlled Autotuning Contracts

Autotuning is supervised and generated-state only. It does not mutate
`config/main.yaml`, profiles or service configs.

| Surface | Contract | Purpose |
| --- | --- | --- |
| `.local/generated/autotuning.proposals.json` | `ai-local.autotuning-proposals.v1` | Advisory proposals derived from calibration report and trends. |
| `.local/generated/autotuning.simulation.json` | `ai-local.autotuning-simulation.v1` | Reviewable before/after diff, evidence, approval gate and rollback value. |
| `.local/generated/autotuning.approvals.json` | `ai-local.autotuning-approval.v1` | Manual approval record for selected applyable proposals. |
| `.local/generated/autotuning.effective.json` | `ai-local.autotuning-effective.v1` | Approved generated overlay consumed by `config.resolver`. |
| `.local/state/autotuning-decision-history.json` | `ai-local.autotuning-decision-history.v1` | Append-only apply/rollback decision history. |

Only approved `resource_governor.*` overlay targets are applied by the resolver.
Unsupported proposals remain visible in simulation, but their approval is
blocked and no effective overlay is written.

## Operational Self Model Contract

`resolved.operational_self_model` carries:

- `resources`: host, Docker and storage facts derived from resolver probes and
  central config.
- `limits`: resolved workers, batch size, Docker resource env values and
  Resource Governor limits.
- `active_owners`: owner/boundary summary for orchestrator, `storage_guardian`,
  RAG and service endpoints.
- `degradations`: config health errors/warnings, stale generated outputs,
  calibration recommendations and trend hints.
- `execution_capacity`: foreground interaction, background storage, heavy GPU,
  Docker lifecycle and routing availability.
- `feeds`: derived Resource Governor, routing and prewarming inputs.

This is a derived status/capacity surface. It must not contain service
semantics, prompt policy, storage lifecycle behavior or routing implementation.

## Compatibility Surfaces

The resolver reports current transition surfaces under
`contracts.compatibility_surfaces`.

| Surface | Status | Sunset |
| --- | --- | --- |
| `config/orc/*.toml` | transition input | Reduce as orchestrator loaders consume resolved contracts directly. |
| `config/rag/*.toml` | transition input | Reduce as RAG loaders consume resolved contracts directly. |
| `config/models/*.json` | transition input | Keep model intent here; remove runtime wiring once consumers read typed resolver output. |
