# Docker infrastructure

This directory contains the Docker runtime assets for `ai-local`.

- `compose/`: root-owned infrastructure compose fragments included by the root
  `compose.yml` (storage, observability, LLM serving and overlays).
- `images/`: Dockerfiles for storage guardian, command sandbox, runtime
  services and RAG services.
- `grafana/`: dashboards and provisioning mounted by the observability compose.
- `otel/`: OpenTelemetry collector config.
- `OBSERVABILITY_RUNBOOK.md`: operator runbook for OTEL, Grafana,
  Langfuse/ClickHouse and cross-owner correlation ids.
- `scripts/`: Docker validation and model download helpers.
- `secrets/`: real local Docker secrets, ignored by Git and Docker build context.
- `secrets.example/`: non-secret documentation for expected secret files.

Validate from the repo root:

```bash
make infra
make up
make rollback
```

`make infra` and `make up` intentionally stay separate. `make infra` generates
config, builds the mandatory image catalog, and validates the Docker contract.
That build is intentionally complete: profiles do not decide which images exist.
Profiles only decide which already-built containers are started later. `make up`
starts the selected stack without rebuilding images, waits for services to
become running/healthy, then runs runtime smoke checks.

Docker operator defaults are inferred by the central resolver and emitted into
`.env.docker.resources.generated`:

- `DOCKER_BUILDKIT`
- `COMPOSE_PARALLEL_LIMIT`
- `AI_LOCAL_DOCKER_BUILD_CACHE_MAX`
- `AI_LOCAL_DOCKER_UP_NO_BUILD`
- `AI_LOCAL_DOCKER_UP_WAIT`
- `AI_LOCAL_DOCKER_UP_WAIT_TIMEOUT`
- `AI_LOCAL_DOCKER_REMOVE_ORPHANS`

Users can tune portable-machine behavior in `config/main.yaml` under `docker:`
or override for one command, for example:

```bash
COMPOSE_PARALLEL_LIMIT=2 make infra
AI_LOCAL_DOCKER_UP_WAIT_TIMEOUT=240 make up
```

The root `compose.yml` is the mono-repo control-plane wrapper. Runtime
definitions live in central fragments:

- `infra/docker/compose/symbiont.yml`
- `infra/docker/compose/rag.yml`

The root wrapper includes those fragments so `make infra` can validate the
platform and build all mandatory project Docker images, while `make up` starts and
smoke-tests the selected stack from the repo root.

## Mandatory Image Build Contract

The mandatory build catalog lives in `config/docker/image-build-catalog.toml`.
It is policy, not runtime behavior:

- all cataloged direct targets are built by `make infra`;
- the root Compose graph is built with all project profiles enabled;
- `AI_COMPOSE_PROFILES` controls `make up`, logs and runtime smoke scope, not
  the image inventory produced by `make infra`;
- `make up` uses `--no-build` by default and assumes images already exist.

This keeps the local AI system responsive: heavy services can remain stopped,
but their images are present for on-demand startup or prewarming.

Large domains should use shared runtime bases and thin leaves. Current mandatory
direct targets include the shared service base, a shared audio runtime, and
specialized Extrator capability images (`core`, `office`, `ocr`, `docling`,
`unstructured`). The compatibility `ai-local-extrator` image remains full
capability until runtime dispatch can choose the specialized images directly.

## Governance

Docker policy is now split into observed and intended truth:

- Observed truth: root `docker compose config --format json`
- Intended policy: `config/docker/service-catalog.toml`
- Profile contract: `infra/docker/compose/profile-contract.toml`

`config/docker/compose-projects.toml` catalogs the root mono-repo wrapper.
`docs/generated/docker-inventory.json` records that check under
`compose_projects`; governance fails if the cataloged compose graph becomes
invalid.
`infra/docker/scripts/validate_compose_profiles.py` checks that each cataloged
profile is documented, each Compose service has a catalog owner and expected
profile set, health requirements are honored, and declared generated env inputs
are present in the Compose project catalog.
`infra/docker/scripts/validate_observability_stack.py` checks that the
observability profile wires OTEL, Grafana, ClickHouse and Langfuse, that OTEL
dashboards query canonical `ai.local.*` attributes, and that service capability
manifests publish degraded-state events.

Useful commands:

```bash
make infra
make up
make rollback
```

## Disk Usage

Docker disk pressure is part of the infra operator contract. In this mono-repo,
the first place to check is BuildKit cache because `make infra` intentionally
builds every mandatory project image, including images that are not started by
the default `make up`.

Use the read-only report first:

```bash
make docker-disk-report
```

The safe cleanup command preserves Docker volumes. It only prunes stopped
containers, dangling images, and BuildKit cache above the configured cap:

```bash
make docker-safe-prune
DOCKER_CACHE_MAX=50gb make docker-safe-prune
```

Do not use `docker volume prune` or `docker system prune --volumes` as routine
maintenance for this project. Volumes can contain Qdrant, model caches,
Temporal state, generated runtime data, and other state that must survive image
rebuilds.

## Workspace Execution

`workspace-execution` is an internal feature service under the `features`
profile. It must not publish a host port; smoke tests enter the running
container and call its local API with the mounted internal token.

Useful commands:

```bash
AI_COMPOSE_PROFILES=core,storage,features make up
python scripts/workspace_execution_smoke.py
```

`workspace-execution` uses `ai-local-command-sandbox:latest` for ephemeral
runner containers. If the feature profile is enabled, `make up` prepares the
runner image before starting compose.

Docker builds consume Git package dependencies such as `sharedai` through the
declared package metadata. `sharedai` is public, so the default dependency path
uses HTTPS and does not require a GitHub SSH key. Private SSH Git dependencies
can still be built without baking credentials into images by passing a BuildKit
secret:

```bash
GITHUB_TOKEN="$(gh auth token)" make infra
GITHUB_TOKEN="$(gh auth token)" DOCKER_BUILD_SECRET_FLAGS="--secret id=github_token,env=GITHUB_TOKEN" make infra
```

Generated reports are written to `docs/generated/`. Strict mode is expected to
become tighter over time; baseline mode protects against accidental ports,
tracked secrets, debug drift and missing healthchecks.

Low-level lifecycle targets such as Compose validation, Docker policy,
inventory, runtime smoke and degraded-state reports are implemented by
`infra/docker/scripts/infra_ops.py`, not exposed as root Makefile commands.
