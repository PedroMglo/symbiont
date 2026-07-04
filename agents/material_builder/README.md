# Material Builder Agent

`material_builder` is the agent owner for material proposals used by the
Material Execution Kernel.

It turns an English working query plus constraints into structured plans, file
specifications, chunks, issue contracts and patch proposals. It does not write
files, execute commands, call Docker, call workspace runtimes, publish artifacts
or mark tasks as complete.

## Ownership

- Owner type: agent.
- Owning repo: `ai-local`.
- Owning directory: `agents/material_builder`.
- Runtime package: `material_builder`.
- External callers: `features/material_execution_kernel` through typed
  API/dispatch.
- Data owned: material prompts, plan proposal contracts, file proposal
  contracts, chunk protocol, patch proposal contracts and material issue
  proposal contracts.
- Data read but not owned: original user query, translation result, context
  bundle, validation logs and issue evidence.
- Data not owned: material sessions, manifests, command runs, filesystem
  writes, VM execution, storage publication, orchestrator routing or final task
  completion.

## Language Policy

Internal material contracts use English. For Portuguese prompts, the translation
owner provides the English working query and the original query is preserved for
audit. This agent must not hardcode Portuguese typo corrections or scenario
shortcuts.

`MaterialPlanRequest` accepts `language_context` alongside `working_query`,
`original_query` and `original_language`. The field is audit/context only: the
agent still plans in English and must not turn language variants into routing,
policy or benchmark-specific generation rules.

## Prohibited Behavior

- No static fallback project.
- No benchmark-specific runtime behavior.
- No direct workspace writes.
- No command execution.
- No Docker or Compose execution.
- No durable storage writes.
- No final completion decisions.

## Runtime Scope

This directory exposes a proposal-only FastAPI service.

Supported proposal modes:

- `contract_blueprint`: explicit caller-supplied contract used by integration
  tests and controlled callers;
- `llm`: centrally configured `MATERIAL_BUILDER_LLM_*` lane for material plans
  per-file proposals, plan-repair proposals and focused patch proposals.

The LLM lane is schema-first:

- planning and file proposals return structured JSON contracts;
- plans may include `validation_commands` keyed by validation profile for
  runtime checks that require project-specific commands, such as API, CLI,
  worker or stateful smoke tests;
- plans may include explicit intended-contract sections: requirements, planned
  file requirement refs, intended interfaces, artifact expectations and
  completion criteria; these are proposal data only and are frozen/validated by
  `features/material_execution_kernel` before file generation;
- plans must include an explicit dependency strategy describing dependency
  metadata files, external packages, lockfiles, install profiles, network need
  and native-build need. The agent proposes this strategy only; policy
  enforcement belongs to `features/material_execution_kernel` and `config`;
- plan repair accepts typed coverage issues from the Material Execution Kernel
  and returns a full repaired plan manifest; it does not patch files, execute
  commands, invent static fallback content or encode benchmark-specific
  structures;
- file generation is requested one file at a time;
- patch generation returns one unified diff for one requested target path;
- invalid JSON receives one schema-repair attempt;
- invalid contracts return typed errors;
- unknown validation profiles are rejected;
- generated proposals include SHA-256 hashes.
- LLM-backed responses include `lane_metrics` with duration, estimated token
  counts, estimated tokens/second, schema retry count, timeout settings,
  no-progress watchdog settings and timeout reason when available.

Validation commands are proposals only. They are executed later by the active
VM-backed sandbox owner through the Material Execution Kernel, never by this
agent. Commands must be bounded, generic to the requested project capability,
free of host Docker/socket assumptions, and must not rely on hidden scenario
templates.

Runtime endpoints:

- `POST /v1/material-builder/plan`;
- `POST /v1/material-builder/plan/repair`;
- `POST /v1/material-builder/files`;
- `POST /v1/material-builder/patch`.

If no LLM lane is configured and no explicit blueprint is supplied, the service
returns `material_builder_backend_unavailable`. That blocked response is
intentional. This agent must not use static fallback projects or benchmark
templates to appear capable.

Runtime state validated in V3.2:

- capabilities report `llm_generation_backend=true` when the lane is active;
- capabilities expose `lane_routes` and `prewarm_lanes` so lifecycle/prewarming
  owners can prepare material lanes without importing this agent's internals;
- live LLM plan and file proposal smoke passed with
  `static_fallback_used=false`;
- plan contracts preserve profile-specific `validation_commands` for runtime
  profiles while rejecting undeclared or unknown profile commands;
- patch proposal contracts are implemented for explicit blueprints and the LLM
  lane, with target-path and expected-hash checks;
- this service still does not write files, execute commands, call Docker or
  complete tasks.

## Verification

```bash
find agents/material_builder -maxdepth 5 -type f -print
python -m compileall agents/material_builder
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q agents/material_builder/tests
ruff check agents/material_builder
```
