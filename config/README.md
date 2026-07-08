# ai-local configuration center

The user-facing config is `config/main.yaml`.

It intentionally contains only high-level choices:

- mode: `dev`, `prod`, `local`, `debug`
- hardware profile: `auto`, `cpu_only`, `gpu_8gb`, `low_ram`
- external storage root and fallback policy
- preferred LLM backend and quality/latency preference
- maximum user limits
- bind host and base port
- privacy policy
- Docker operator limits for parallel builds, cache caps and startup waiting
- mandatory Docker image build policy for all prebuilt runtime capabilities
- material dependency policy defaults for generated-project sessions

Everything else is resolved by:

```bash
python -m config.resolver --print
python -m config.resolver --explain
python -m config.resolver --validate
python -m config.resolver --write-storage-env
python -m config.resolver --write-llm-env
python -m config.resolver --write-services-env
python -m config.resolver --write-docker-resources-env
python -m config.resolver --write-ollama-host-config
python -m config.resolver --health-report
python -m config.resolver --self-model
python -m config.resolver --write-operational-self-model
python -m config.autotuning --json
python -m config.autotuning --simulate
```

## Generated Env Contracts

Generated env files are transition artifacts, not source-of-truth config
islands. Each file declares its contract id and version in comments and in
machine-readable env values so Docker/service consumers can detect the schema
they are reading.

| Artifact | Contract | Expected consumer | Sunset condition |
| --- | --- | --- | --- |
| `.env.storage.generated` | `ai-local.storage-env.v1` | Docker Compose storage binds and `storage_guardian` | Replace after storage consumers read typed resolver output instead of env files. |
| `.env.llm.generated` | `ai-local.llm-env.v1` | LLM serving Compose services, orchestrator and RAG model loaders | Replace after LLM consumers read typed resolver output instead of env files. |
| `.env.services.generated` | `ai-local.services-env.v1` | Docker Compose service wiring, orchestrator, agents, features and RAG service loaders | Replace after service consumers read typed resolver output instead of env files. |
| `.env.docker.resources.generated` | `ai-local.docker-resources-env.v1` | Docker Compose resource limits and operator knobs generated from central config | Replace after Compose resource policy and infra lifecycle read typed resolver output instead of env files. |

The resolver also exposes these contracts under `contracts.generated_env` in
`python -m config.resolver --print`. The complete resolver, generated env,
runtime output and transition surface contracts live in
[`config/RESOLVER_CONTRACTS.md`](RESOLVER_CONTRACTS.md). Contract v2 must be
paired with a consumer migration and must keep or explicitly deprecate v1 keys
until that migration lands.

`python -m config.resolver --health-report` prints a short typed object for
status surfaces such as `orchestrator /health`. Its states are `ready`,
`degraded`, `blocked`, `external_missing`, `local_fallback` and `stale`.

## Controlled Autotuning

`config.autotuning` turns calibration reports and trends into supervised
proposals. The apply path is intentionally gated:

```bash
python -m config.autotuning --write
python -m config.autotuning --simulate
python -m config.autotuning --approve all --approver "$USER" --approval-reason "reviewed"
python -m config.autotuning --apply-approved
python -m config.autotuning --rollback
```

Autotuning never edits `config/main.yaml` and never applies automatically. A
proposal must pass through simulation and approval before an effective overlay
is written to `.local/generated/autotuning.effective.json`. The resolver then
loads that generated overlay into `resolved.resource_governor_policy.autotuning`
and applies only approved `resource_governor.*` targets.

Each simulation includes the target, before/after diff, reason, evidence,
approval requirement and rollback value. Apply and rollback actions are recorded
under `.local/state/autotuning-decision-history.json`.

## Operational Self Model

`resolved.operational_self_model` is the AGI-grade status/capacity surface for
config-owned inference. It derives, but does not own, runtime behavior:

- resources: host, Docker and storage facts
- limits: resolved workers, batch size, Docker resources and Resource Governor
  limits
- active owners: orchestrator, `storage_guardian`, RAG and service endpoints
- degradations: config health, stale outputs, calibration recommendations and
  trend hints
- execution capacity: foreground interaction, background storage, heavy GPU,
  Docker lifecycle and routing availability
- feeds: Resource Governor, routing budgets and prewarming capacity

Use `python -m config.resolver --self-model` for inline inspection or
`python -m config.resolver --write-operational-self-model` to write
`.local/generated/operational-self-model.json`.

## Resource Pressure Policy

`config/resource_governor.yaml` owns the global pressure policy emitted in
`resolved.resource_governor_policy.pressure_policy`. Swap usage is treated as
active pressure only when it is growing, RAM headroom is below policy, PSI
reports memory/IO stalls, or the operator explicitly disables active-pressure
gating. Residual swap with healthy RAM is observed by default and should not
reduce or pause background work unless `swap_static_action: reduce` is set.

Linux PSI `avg10` values are percentages. For example, `avg10=0.48` means less
than one percent stalled time, not forty-eight percent. Hard PSI thresholds
should therefore be expressed as sustained double-digit stall percentages.

Job-end cleanup is owner-safe: config declares that owners should release their
own process-local caches and malloc arenas after heavy jobs, while global actions
such as `swapoff`, `drop_caches`, killing unknown processes or Docker prune stay
forbidden outside explicit owner contracts.

## Worker Budgets

The resolver publishes separate worker decisions for different resource
classes:

- `runtime.workers.final` is the conservative mixed-runtime budget. It is
  limited by CPU, RAM, user limits and GPU/VRAM capacity because it can affect
  interactive LLM/model work.
- `runtime.workers.background_cpu_io` is the preemptible CPU/IO background
  budget. It is limited by CPU, RAM and user limits, but not by free VRAM. It
  is intended for read-only enrichment such as local evidence inspection,
  document extraction dispatch, archive listing and similar work where the
  owner service still enforces its own safety and storage boundaries.

`resolved.resource_governor_policy.limits.background_workers` consumes the
CPU/IO budget, while `heavy_gpu_concurrency` still bounds GPU-heavy lanes such
as audio transcription. This lets stronger Linux machines use available CPU/RAM
without hardcoding any user-specific machine profile.

## Precedence

The resolver applies:

```text
safe internal defaults
+ config/main.yaml
+ selected profile from config/profiles.yaml
+ runtime probes
+ AI_* environment overrides
+ final validation
```

Secrets are never read into the resolved user config. Runtime secrets stay in
Docker secrets or `*_FILE` environment variables.

## Docker

The `docker:` section in `config/main.yaml` keeps local Docker behavior
portable across users and machines. The resolver validates those inputs and
emits transition env values such as `COMPOSE_PARALLEL_LIMIT`,
`DOCKER_BUILDKIT`, `AI_LOCAL_DOCKER_BUILD_CACHE_MAX`,
`AI_LOCAL_DOCKER_UP_NO_BUILD`, `AI_LOCAL_DOCKER_UP_WAIT` and
`AI_LOCAL_DOCKER_UP_WAIT_TIMEOUT`.

Defaults use `auto` where machine capacity matters: low-capacity hosts get
lower Compose parallelism and cache caps, while larger workstations get more
parallelism without requiring a user-specific Makefile edit.

The mandatory image build catalog lives in `config/docker/image-build-catalog.toml`.
`make infra` builds every mandatory direct target and the full Compose image
catalog. `AI_COMPOSE_PROFILES` controls runtime activation for `make up`; it
does not remove images from the build inventory.

## Storage

External storage is discovered automatically by default with
`storage.external_root: auto`. The resolver checks explicit candidates from
`AI_STORAGE_AUTO_CANDIDATES` and common mount layouts such as `/mnt/*/ai-local`,
`/media/$USER/*/ai-local`, `/run/media/$USER/*/ai-local` and
`/Volumes/*/ai-local`.

If external storage is required but no writable root is discovered, the
resolver switches to `local_fallback` by default: it leaves
`AI_STORAGE_EXTERNAL_ROOT` empty, writes a warning, and points Docker bind paths
at `.local` with data under `.local/data` and logs under `.local/logs` so the
core stack can start without an SSD mounted.

Set `AI_STORAGE_EXTERNAL_ROOT=/absolute/path` or
`storage.external_root: /absolute/path` to force a specific external root. Use
`AI_STORAGE_AUTO_DISABLE_DISCOVERY=true` for deterministic tests that should
not scan mounted devices.

The fallback can be disabled with `AI_STORAGE_ALLOW_LOCAL_HEAVY_FALLBACK=false`
or `storage.allow_local_heavy_fallback: false`. In that mode the resolver uses
`external_missing`, points data paths at `.local/data/external-missing`, and
leaves heavy/background work blocked until the external root is restored.

When local fallback is active, the resolver writes a warning to
`.local/logs/storage/storage-warning.txt` and emits `AI_LOCAL_UID`/
`AI_LOCAL_GID` so Docker services that write bind-mounted data can run as the
local owner.

`make infra` generates `.env.storage.generated` through the central resolver
and runs a one-shot reconciliation. When the SSD is detected again, files
created under an explicitly enabled local fallback tree are moved to the
matching SSD paths after hash verification; identical duplicates are removed
locally, conflicts are left in local storage and reported. This is not a
background daemon, so it does not consume resources continuously.

The generated env file remains the Docker Compose transition layer for
`LLM_MODELS_DIR`, `HF_CACHE_DIR`, `OLLAMA_MODELS`,
`AUDIO_TRANSCRIBE_DATA_DIR`, `GRAPHIFY_OUT_DIR`, `QDRANT_DATA_DIR` and related
storage paths.

Compose fragments that can be statically inspected before generated env files
are loaded must use resolver-compatible defaults for these storage bind paths.
The generated env file still has precedence, but direct `docker compose config`
must not warn merely because `LLM_MODELS_DIR` or `HF_CACHE_DIR` is absent from
the caller shell.

## LLM Serving

`make infra` generates `.env.llm.generated` through the central resolver. It
contains non-secret transition values for LLM serving and agent Compose
files:

- backend URLs such as `VLLM_URL`, `LLAMA_CPP_AUX_URL` and `LLAMA_CPP_FAST_URL`
- native Ollama URLs such as `OLLAMA_BASE_URL`, `ORC_OLLAMA_BASE_URL` and
  `RAG_OLLAMA_BASE_URL`
- backend acceleration hints such as `ORC_LLM_BACKEND_OLLAMA_ACCELERATOR`, derived
  from central runtime inference rather than service-local probes
- agent model assignments such as `REASONING_AND_RESPONSE_MODEL`
- native agent loader variables such as `REASONING_AND_RESPONSE_LLM_BASE_URL`,
  `REASONING_AND_RESPONSE_LLM_TEMPERATURE`,
  `REASONING_AND_RESPONSE_LLM_MAX_TOKENS` and
  `REASONING_AND_RESPONSE_LLM_TIMEOUT_SECONDS`
- vLLM startup knobs such as `VLLM_MAX_MODEL_LEN` and `VLLM_GPU_MEM_UTIL`
- llama.cpp model files, context windows, threads and batch sizes

Agent TOMLs no longer expose manual `[llm]` blocks. LLM backend, model,
temperature, token cap and timeout defaults are generated by the resolver and
can still be overridden per command through the corresponding environment
variable.

Long-running agentic material generation is also configured here. The resolver
derives `ORC_AGENTIC_RUNTIME_TASK_DEFAULT_TIMEOUT_SECONDS`,
`ORC_AGENTIC_RUNTIME_MATERIAL_DECISION_TIMEOUT_SECONDS`,
`MATERIAL_EXECUTION_KERNEL_SESSION_BUDGET_SECONDS`,
`MATERIAL_EXECUTION_KERNEL_NO_PROGRESS_WATCHDOG_SECONDS`,
`MATERIAL_EXECUTION_KERNEL_MODEL_LANES`,
`MATERIAL_EXECUTION_KERNEL_PREWARM_MATERIAL_LANES`,
`MATERIAL_EXECUTION_KERNEL_BUILDER_TIMEOUT_SECONDS`,
`MATERIAL_BUILDER_MAX_FILES`, `MATERIAL_BUILDER_FILE_TARGET_SECONDS` and
`MATERIAL_BUILDER_LLM_CALL_MAX_TIMEOUT_SECONDS` from the selected quality/latency
profile and LLM request budget. These are watchdogs and progress budgets, not
fixed project templates or canned content.

Material builder model routing is lane-specific and generated centrally:
`MATERIAL_BUILDER_PLAN_LLM_*`, `MATERIAL_BUILDER_FILE_LLM_*`,
`MATERIAL_BUILDER_PATCH_LLM_*`, `MATERIAL_BUILDER_REPAIR_LLM_*` and
`MATERIAL_BUILDER_CRITIC_LLM_*`. The material builder reports the selected lane
route in its structured responses, and the material execution kernel projects
that route into session events for latency and backend diagnostics. These lanes
are backend/model budgets only; they must not contain scenario-specific output
or static generation shortcuts.

Material dependency policy defaults are also central configuration. By default,
generated-project package installation and external network access are disabled,
lockfiles are not required for dependency-free sessions, and native builds are
denied. The Material Execution Kernel receives the effective policy through its
typed session constraints and freezes it into the material contract before
validation or repair.

`config/models/orc.config.json` keeps model/agent intent, prompts and backend
service names. It should not contain runtime URLs for `vllm`, `llama.cpp` or
Ollama; the orchestrator service registry resolves those from generated envs such as
`ORC_SERVICES_VLLM_URL`, `ORC_SERVICES_LLAMA_CPP_FAST_URL` and
`ORC_OLLAMA_BASE_URL`. `backend.url` is still accepted as an explicit
override, but it is no longer the normal path.

`config/models/rag.config.json` follows the same rule: RAG roles keep model
names and prompts, while `RAG_OLLAMA_BASE_URL` supplies the Ollama endpoint.

RAG runtime performance values are also generated centrally. `config/rag.py`
derives `RAG_PERFORMANCE_*`, `RAG_ROUTER_TIMEOUT`,
`RAG_GRAPHIFY_MAX_CONCURRENCY` and `RAG_GRAPHIFY_COMMUNITY_MAX_WORKERS` from
hardware/runtime probes, global worker/batch decisions and the quality/latency
policy. The final values are visible in `python -m config.resolver --explain`
under `rag_runtime`.

Symbiont runtime values follow the same rule. `config/symbiont_runtime.py`
derives dispatch budgets, context/feature/agent timeouts, dynamic routing
limits and collaboration TTLs from the resolved worker count, LLM timeout and
quality/latency policy. The resolver emits these as `ORC_DISPATCH_*`,
`ORC_DYNAMIC_ROUTING_*` and `ORC_AGENTS_COLLABORATION_*` in
`.env.services.generated`; `--explain` shows them under
`symbiont_runtime`.

Production lifecycle values are also emitted there. Long-running service
overrides must satisfy the live verification floors in
`config.symbiont_runtime.PRODUCTION_LIFECYCLE_IDLE_TIMEOUT_FLOORS` so
autonomous material, extraction, research and workspace tasks are not reaped
while still doing useful work.

Agentic command runtime values are also generated centrally.
`config/command_runtime.py` derives command timeout, output truncation budget,
session TTL, max commands per session, Docker memory limit and Docker PIDs
limit from the quality/latency profile, resolved LLM request timeout and
resolved worker count. These values are sized for autonomous validation and
repair sessions in the `workspace_execution` sandbox, while still keeping
bounded watchdogs for stuck commands. The resolver emits these as
`ORC_AGENTIC_RUNTIME_COMMAND_TOOL_*` in `.env.services.generated`; `--explain`
shows them under `command_runtime`.

`config/orc/llm.toml` keeps backend policy such as priority, enabled flag,
models and capabilities. Known backend URLs are inferred by name from generated
envs, so `base_url` is only needed for a new custom backend that is explicitly
enabled.

## Service Registry

`make infra` generates `.env.services.generated` from the central service
registry. It contains non-secret Docker-internal URLs, hosts, ports and derived
worker counts for services such as `reasoning_and_response`, `research`,
`audio_transcribe`, `translation`, `clickhouse` and `otel`.

The `[services]` section in `config/orc/providers.toml` is retired as a
manual URL list. The runtime now accepts generated `ORC_SERVICES_*`
environment variables and falls back to the internal registry defaults.

Per-service `[server]` blocks are also no longer user-edited in agent/feature
TOMLs. The generated services env emits each service's native loader variables,
for example `REASONING_AND_RESPONSE_SERVER_PORT`, `RESEARCH_SERVER_WORKERS` and
`AUDIO_TRANSCRIBE_SERVER_HOST`.

The registry also emits healthcheck policy such as `SYMBIONT_HEALTHCHECK_PATH`
and `SYMBIONT_HEALTHCHECK_TIMEOUT`. The runtime uses the lightweight
`/live` endpoint for Docker liveness, while `/health` remains the richer runtime
diagnostic endpoint.

RAG runtime endpoints are generated here as well. `RAG_API_HOST`,
`RAG_API_PORT`, `RAG_STORE_QDRANT_URL`, `RAG_OBSERVABILITY_CLICKHOUSE_URL`,
`RESEARCH_RAG_URL` and `LOCAL_EVIDENCE_GRAPH_RAG_URL` are runtime wiring, not
manual user TOML.

Resource Governor telemetry authority settings are declared in
`config/resource_governor.yaml` under `telemetry_authority`. The services env
emits `AI_TELEMETRY_AUTHORITY_URL`,
`AI_TELEMETRY_AUTHORITY_CA_BUNDLE_PATH`,
`AI_TELEMETRY_AUTHORITY_CACHE_TTL_SECONDS` and
`AI_TELEMETRY_AUTHORITY_TIMEOUT_SECONDS`; run `make telemetry-authority` on the
host before GPU-heavy live validation. That target generates a
`telemetry-authority` certificate under `.local/tls` and exposes the authority
over HTTPS so `/resources/snapshot` can include host `nvidia-smi` GPU
utilization, VRAM, temperature and process data.

`make infra` generates storage, LLM and service env files.

The services env also emits host mount anchors such as `AI_LOCAL_HOST_HOME`,
`AI_LOCAL_HOST_PROJECTS_DIR`, `PROJECTS_DIR` and `AI_LOCAL_TLS_DIR_HOST`.
Docker Compose consumes these when lifecycle-managed containers start other
services, so host read-only workspace and TLS mounts do not depend on the
invoking container's own `${HOME}` value or relative working directory.

## Host Ollama

Native host services that are outside Docker still receive generated project
configuration. `make infra` writes host helper artifacts under
`.local/generated/`, including the Ollama systemd drop-in and an idempotent
apply script.

For a native Linux/systemd Ollama service:

```bash
make infra
.local/generated/ollama-host/apply-ollama-systemd.sh
```

The generated Ollama drop-in is derived from central config and runtime probes.
When a GPU is available it enables GPU offload with `CUDA_VISIBLE_DEVICES=0`
and `OLLAMA_NUM_GPU=-1`; CPU-only profiles avoid the GPU path. It does not
install or pull models.

Manual storage reconciliation must run through the storage owner:

```bash
PYTHONPATH=. python -m storage_guardian.cli --config config/storage_guardian.yaml reconcile-structure
PYTHONPATH=. python -m storage_guardian.cli --config config/storage_guardian.yaml reconcile-structure --apply
PYTHONPATH=. python -m storage_guardian.cli --config config/storage_guardian.yaml sync-pending
```

## Runtime Transition Env

Runtime services read active TOML/JSON transition files from `config/orc`,
`config/rag` and `config/models`. These files are no longer hidden project
state; they are part of the root configuration surface and should be reduced
over time as more consumers read typed resolver output directly.

## Docker Policy

Shared Docker catalogs live under `config/docker`:

- `config/docker/service-catalog.toml`
- `config/docker/compose-projects.toml`
- `config/docker/volumes-catalog.toml`
- `config/docker/governance/*.toml`

Compose files and vendor runtime assets remain under `infra/`, but policy,
ownership and backup/restore classification are central config.
