# Agentic Completion Spec

Data: 2026-06-15.

## Ownership

`orchestrator/agentic` owns runtime completion semantics, task state,
policy-gated execution, and ledger evidence. Agents may propose facts and
actions, but the runner decides whether a task has enough evidence to move to
`completed`.

## Completion Contract

A task may be marked `completed` only when the ledger contains evidence that
matches the requested kind of outcome.

- Read-only or conversational tasks may complete from graph output, accepted
  deliberation, or read-only observations.
- Material-output tasks, such as requests to create a project, files, code,
  reports, datasets, containers, or runnable artifacts, require at least one
  successful effectful action or artifact/diff evidence from an owned execution
  service.
- When `expected_artifact_root` is declared for a generated project or
  deliverable root, completion requires a workspace artifact whose path matches
  that root or its archive name. Intermediate diffs remain timeline evidence,
  but they do not by themselves prove that the final deliverable root was
  validated and packaged.
- Deliberation consensus is evidence for confidence, not evidence that material
  work happened.
- Empty successful agent responses are failed observations. They must not count
  toward consensus or task completion.

## Material Output Detection

Callers should prefer explicit metadata:

```json
{
  "material_output_required": true,
  "expected_artifact_root": "relative/project-name",
  "expected_artifacts": ["README.md", "docker-compose.yml"]
}
```

When metadata is absent, the runtime may infer a material-output task from
generic delivery language in the goal. Inference is only a completion guard and
delegation signal; it must not select service internals, special-case benchmark
scenarios, or bypass policy.

## Execution Boundary

Material-output work must run through `features/material_execution_kernel`.
The kernel coordinates material sessions through typed API calls to
`agents/material_builder` and the active VM-backed sandbox owner.

The orchestrator must not write generated project files, execute generated
commands, call Docker for generated projects, or import material owner internals.
Publishing or applying artifacts to durable storage remains owned by
`storage_guardian` or a future explicit apply contract.

## Resource Leases

User-requested material-output tasks are foreground work for Resource Governor
admission unless explicitly marked as `background` or `heavy`. They should
request an `interactive` model-runtime/material-generation lease with quality
preserved. Explicit background/heavy tasks remain preemptible and may be
deferred under battery, thermal, swap, or interaction pressure.

## Material Execution Kernel

Material-output delegation should prefer a kernel-owned material session with a
`material_manifest.v3.2` projection. Agents should not return project-building
`AgentDecision` payloads. A robust flow is:

- create or resume a material session by idempotency key;
- request a compact plan from `agents/material_builder`;
- request file or patch proposals through structured contracts;
- materialize files through sandbox batch-write or patch APIs;
- run validation profiles inside the VM-backed sandbox owner;
- record command, validation, artifact and VM cleanup evidence;
- emit replayable material events and a `material_manifest.v3.2` manifest;
- complete only when artifact, validation, VM cleanup and no-host-execution
  evidence are present.

If a material owner cannot produce structured project content or validation
evidence, it must fail closed with typed issues. It must not fabricate static
project content, use benchmark-specific templates, or bypass the LLM/material
proposal contract.

Sandbox sessions should preserve their scratch/artifact state until TTL expiry
or explicit cleanup so validation can inspect generated diffs and artifact
descriptors. This is temporary service-owned storage, not a host write. Durable
publishing remains a separate `storage_guardian` action.
