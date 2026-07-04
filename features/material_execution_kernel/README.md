# Material Execution Kernel

`material_execution_kernel` is the feature owner for material work sessions:
turning an approved material intent into a validated artifact through governed
VM-backed sandbox execution.

It is not an executor and not an LLM agent. It coordinates typed proposals,
workspace materialization, validation, repair, diagnostics and completion
evidence.

## Ownership

- Owner type: feature service.
- Owning repo: `ai-local`.
- Owning directory: `features/material_execution_kernel`.
- Runtime name: `material-execution-kernel`.
- Runtime package: `material_execution_kernel`.
- External callers: `orchestrator/agentic` through HTTPS/API or dispatch.
- Data owned: material sessions, material manifests, material events, repair
  rounds, issue state, validation summaries and diagnostic descriptors.
- Data read but not owned: user prompt, translation result, policy context,
  material builder proposals, sandbox command evidence and transient artifact
  descriptors.
- Data not owned: LLM prompt behavior, filesystem writes, command execution,
  Docker/Compose runtime, durable storage lifecycle, task completion policy and
  final user synthesis.

## Boundaries

`orchestrator/agentic` owns task lifecycle, routing, policy, leases, ledger,
timeline projection and completion decision.

`agents/material_builder` owns material plan, file, chunk, issue and patch
proposals. It does not execute or write.

The active sandbox owner, initially `features/workspace_execution` unless it is
replaced by `features/workspace_runtime`, owns VM-backed workspaces, batch
writes, patch apply, command runs, background services, diffs, logs and
transient artifacts.

`storage_guardian` owns durable artifact publication. This feature never writes
managed storage paths directly.

## V3.2 Security Invariants

- Generated content is untrusted data until proven safe.
- Generated content is never policy for the current session.
- Generated-code commands must never execute on the host.
- VM-backed sandbox execution is required for generated files, tests,
  Dockerfiles, Compose stacks, scripts, hooks and CLIs.
- Fallback from VM to host is forbidden.
- The generated project must never receive the host Docker socket.
- Completion requires artifact evidence, validation evidence and cleanup
  evidence with `host_execution_used=false`.

## Public API Draft

Base internal URL: `https://material-execution-kernel:8000`

| Method | Path | Use |
| --- | --- | --- |
| GET | `/health` | Healthcheck. |
| GET | `/v1/material-execution/capabilities` | Capabilities, limits and schemas. |
| POST | `/v1/material-execution/sessions` | Start or resume a material session. |
| GET | `/v1/material-execution/sessions/{id}` | Session and manifest summary. |
| POST | `/v1/material-execution/sessions/{id}/step` | Advance one bounded phase. |
| GET | `/v1/material-execution/sessions/{id}/events` | JSONL-compatible event stream. |
| GET | `/v1/material-execution/sessions/{id}/diagnostics` | Diagnostic refs. |
| POST | `/v1/material-execution/sessions/{id}/cancel` | Stop without publishing. |

## Language Context

`MaterialSessionRequest.language_context` carries the original language,
English working language, translation availability/safety, quality metadata and
final response language. During planning the kernel forwards that context to
`agents/material_builder` through the typed HTTP client, together with
`original_query` and the English `working_query`.

The kernel does not translate, lint, route or synthesize final user text. It
only preserves the language audit trail while enforcing the English internal
contract for material proposals.

## Runtime Scope

The current implementation provides the bounded kernel runtime:

- material session create/read/cancel contracts;
- JSONL-compatible event stream;
- incremental manifest projection;
- HTTP client boundary to `agents/material_builder` proposals;
- HTTP client boundary to the active sandbox owner;
- bounded phase advancement for policy preflight, VM request, planning, file
  generation, batch write, validation, evidence-driven repair, revalidation,
  packaging and completion evidence.

It still does not execute shell, call Docker, write generated files itself,
publish durable storage, or import owner internals. Real side effects arrive
through configured material-builder and sandbox owner APIs. If the active
sandbox owner cannot prove a ready VM-backed sandbox, the session blocks with
typed VM evidence instead of falling back to host execution.

Current V3.2 runtime state:

- `material-builder` proposals are available through the configured LLM lane;
- material session create/read/step/events are live;
- JSONL material events are reducible and include latency source fields;
- capabilities expose the configured material model lane policy and runtime
  watchdogs; session creation emits `material.model_lanes.prewarm.requested`
  as a side-effect-free intent for lifecycle/prewarming owners;
- VM allocation can receive ready `microvm` isolation proof from
  `workspace_execution` when the local QEMU/KVM backend is available;
- workspace materialization, validation and artifact packaging are exercised
  through the active sandbox owner, not by direct filesystem or shell access;
- material file plans that already include the project root are normalized
  before batch write: the sandbox receives paths relative to `project_root`,
  while the manifest keeps full project-relative paths;
- before file generation, the kernel runs a scenario-neutral plan coverage
  gate derived from requested capabilities, validation profiles, file kinds,
  architecture notes and proposed validation commands;
- after plan coverage passes and before any file generation request, the kernel
  freezes `MaterialContract v0.1`, a versioned intended contract containing
  language audit fields, requirements, planned files, intended interfaces,
  validation profiles, artifact expectations and completion criteria;
- `MaterialContract v0.1` requires every planned file, validation, artifact and
  completion criterion to trace to a requirement or the frozen contract ID, and
  rejects duplicate IDs, orphan references and scenario-specific runtime rule
  terms;
- contract creation emits `material.contract.created` and
  `material.contract.frozen`; invalid contracts block with
  `material_contract_invalid` before `agents/material_builder` is asked to
  generate file bodies;
- after the sandbox owner proves batch materialization, the kernel builds
  `ObservedContract v0.1` from generated file evidence using deterministic,
  bounded extractors; the initial Python adapter records imports, exports,
  dependency metadata, test expectations, generic runtime surfaces and
  observed issues without executing generated code on the host;
- observed contract extraction emits `material.observed_contract.extracted`
  and projects the result into `material_manifest.v3.2`;
- after observed contract extraction, the kernel runs `ContractComparison v0.1`
  to build a requirement-driven trace between requirements, acceptance
  criteria, intended interfaces, validation profiles, concrete checks and
  evidence refs; interface/dependency/symbol drift blocks before runtime
  validation when there is no valid evidence path;
- `DependencyPolicy` is resolved outside the kernel and carried into each
  material session as explicit policy. `MaterialContract v0.1` freezes that
  policy together with the builder's dependency strategy, and contract
  comparison fails closed when generated imports, package install intent,
  network use, lockfile requirements or native-build needs violate policy;
- contract comparison emits `material.contract_comparison.completed` or
  `material.contract_comparison.failed`; observed extra runtime surfaces emit
  `material.contract.change.required` as evidence for a future amendment
  instead of silently changing the frozen contract;
- incomplete plans emit `material.plan.coverage.failed` and are sent back to
  `agents/material_builder` for a full plan-repair proposal; if coverage
  remains unresolved, the session blocks with `material_plan_coverage_unresolved`
  before any generated file is written or command is run;
- validation profiles can use declarative `validation_commands` proposed by
  `agents/material_builder`; the kernel validates the profile match, sends the
  command to the sandbox owner with `requires_vm_backed_sandbox=true`, and
  records stdout/stderr refs plus bounded previews on failed validations;
- failed validation issues with a concrete repair target enter a bounded
  patch-first repair loop: the kernel requests a patch proposal from
  `agents/material_builder`, asks the sandbox owner to apply it, records
  before/after hash evidence and re-runs validation before packaging;
- patch and patch-set repairs emit `material.patch.critic.completed` after
  deterministic patch-contract verification and before the sandbox owner is
  asked to apply changes;
- `/v1/material-execution/sessions/{id}/diagnostics` returns a
  `material_diagnostics.v3.2` projection derived from the session manifest and
  material events, including requirements trace, contract comparison, issue
  targeting, patch rejection history, dependency policy, validation
  applicability, model lane metrics, command runs, security decisions and
  artifact summary;
- runtime profiles without a built-in command and without a declared validation
  command block with `validation_command_unavailable`, rather than inventing a
  static probe;
- missing tools, VM/runtime failures, security issues and validation failures
  without a patchable target fail closed rather than becoming speculative
  regeneration;
- completion requires artifact evidence, command evidence and VM cleanup
  evidence with `host_execution_used=false`;
- when the VM backend or an isolated container runtime is unavailable, the
  kernel/sandbox path blocks with typed evidence rather than falling back to
  host execution.

Docker Compose runtime validation (`build`, `up`, `logs`, `down`) depends on a
configured VM-backed compose runtime proxy exposed by the sandbox owner. Static
Compose validation (`docker compose config`) is covered by the VM command path.
When the proxy is not configured or fails isolation invariants, the sandbox
returns typed `docker_runtime_unavailable` or `compose_runtime_isolation_failed`
evidence; the kernel must not reinterpret that as success or fallback to host
execution.

Phase 7 live evidence:

- `make material-runtime-profiles-smoke` completed a material session with
  `python-basic`, `python-api` and `cli` validations inside the VM-backed
  sandbox;
- the API profile used VM-local loopback and did not enable external network;
- completion included artifact, command-run evidence, cleanup evidence and
  `host_execution_used=false`.

## Verification

```bash
find features/material_execution_kernel -maxdepth 5 -type f -print
python -m compileall features/material_execution_kernel
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q features/material_execution_kernel/tests
ruff check features/material_execution_kernel
make material-kernel-smoke
make material-runtime-profiles-smoke
```
