# Workspace Execution Feature

`workspace_execution` is the owned feature for disposable execution sessions over
workspace snapshots, uploaded inputs, and generated artifacts. It exists so
agents and features can run commands, tests, conversions, and destructive
experiments inside an isolated copy before any real user-machine change is
authorized elsewhere.

This feature is the canonical owner for disposable sandbox execution. The
orchestrator command tool may keep compatibility backends, but the default
agentic command backend is expected to call this API rather than own a runner.

## Ownership

- Owner type: feature service.
- Owning repo: `ai-local`.
- Owning directory: `features/workspace_execution`.
- Runtime name: `workspace-execution`.
- Runtime package: `workspace_execution`.
- External callers: `orchestrator` through HTTPS/API dispatch, and
  other agents/features only through this feature API.
- Data owned: disposable sessions, copied workspaces, session-local command
  records, diffs, transient artifacts, redacted execution logs, TTL cleanup
  metadata.
- Data read but not owned: source workspace files, uploaded documents, feature
  outputs, storage object descriptors, central generated config.
- Data not owned: managed storage paths, archive/restore policy, object
  promotion/deletion, document extraction semantics, shell risk classification,
  code analysis semantics, agent prompts, routing, policy decisions, and final
  host mutation.

## Boundaries

`workspace_execution` owns execution in a disposable copy only.

`storage_guardian` owns all managed storage writes, folder lifecycle,
archive/restore, object upload sessions, promotion, deletion, manifests, hashes,
and chain-of-custody. This feature may write only to explicitly unmanaged
session scratch paths and must publish durable artifacts through
`storage_guardian` APIs.

`orchestrator` owns runtime control flow, policy gates, reducers,
ledger events, and tool routing. The orchestrator must not import
`workspace_execution` internals; it calls this feature through HTTPS/API or a
generic dispatch boundary.

`execution_policy_operator` owns shell-command risk classification. This feature may require
classification evidence before execution, but it must not duplicate
execution-policy parsers or risk rules.

Domain features and agents such as `extrator`, `local_evidence_operator`,
`translation`, and `research` keep their own pipelines and contracts. They can
use `workspace_execution` only as an execution substrate through API requests.

## Config

User-facing and machine-specific runtime values belong in the root `config/`
center, not in service-local config files.

Config integration covers:

- service port and generated service URL;
- worker/concurrency limits;
- default CPU, memory, PID, timeout, and session TTL limits;
- allowed runner image names or profiles;
- validation profiles advertised by the feature, such as `python-basic`,
  `python-pytest`, `python-api`, `docker-compose-static`,
  `docker-compose-runtime`, `stateful-postgres`, `stateful-redis`,
  `worker-queue`, `cli`, `artifact` and `node-basic`;
- default network policy;
- scratch root used by the service container;
- feature manifest and policy action wiring.

Service-local config may contain only static implementation constants that do
not need machine inference or user control.

## Storage

Session scratch paths are unmanaged temporary paths. They are valid only while a
session is active and must be cleaned by TTL or explicit close.

Durable outputs must become storage objects through `storage_guardian`:

1. The sandbox produces artifact bytes and metadata inside the session.
2. The feature computes content hashes and returns artifact descriptors.
3. A publish request calls `storage_guardian` upload/object APIs.
4. The response records storage object ids and chain-of-custody metadata.

This feature must never delete, move, archive, restore, or promote real
user-machine storage directly.

## API Contract Draft

Base internal URL: `https://workspace-execution:8000`

Authentication: service-to-service auth through the same servicekit/dispatch
pattern used by other features.

| Method | Path | Use |
| --- | --- | --- |
| GET | `/health` | Healthcheck |
| GET | `/v1/workspace-execution/capabilities` | Advertised execution profiles and limits |
| POST | `/v1/workspace-execution/sessions` | Create a disposable execution session |
| POST | `/v1/workspace-execution/vm-sessions` | Request a VM-backed material sandbox session |
| GET | `/v1/workspace-execution/vm-sessions/{vm_session_id}` | Read VM session isolation proof |
| GET | `/v1/workspace-execution/vm-sessions/{vm_session_id}/events` | Read VM lifecycle events |
| POST | `/v1/workspace-execution/vm-sessions/{vm_session_id}/close` | Cleanup/close VM session |
| POST | `/v1/workspace-execution/sessions/{session_id}/inputs` | Attach uploaded inputs or storage object materializations |
| POST | `/v1/workspace-execution/sessions/{session_id}/files/batch` | Materialize generated files by content hash inside the session |
| POST | `/v1/workspace-execution/sessions/{session_id}/patches` | Apply unified-diff repair patches inside the session |
| POST | `/v1/workspace-execution/sessions/{session_id}/commands` | Run a policy-approved command inside the session copy |
| GET | `/v1/workspace-execution/sessions/{session_id}/diff` | Return structured diff against the session baseline |
| GET | `/v1/workspace-execution/sessions/{session_id}/artifacts` | List generated artifacts and hashes |
| POST | `/v1/workspace-execution/sessions/{session_id}/artifacts/publish` | Publish selected artifacts through `storage_guardian` |
| POST | `/v1/workspace-execution/sessions/{session_id}/close` | Close and schedule cleanup |

Diff file entries include path, status, line counts, hashes, binary status and
an optional bounded `patch` preview when the service can prove the textual diff
from session-local state.

## Operation

`workspace-execution` runs under the `features` compose profile for the broad
feature stack and under the `material` profile for the material execution path.
It must remain internal-only. It depends on `docker-proxy` for the current
ephemeral runner backend and uses `ai-local-command-sandbox:latest` for
non-material command execution.

Root-level operational commands:

```bash
AI_COMPOSE_PROFILES=core,storage,features make up
python scripts/workspace_execution_smoke.py
AI_COMPOSE_PROFILES=core,storage,material make up
make material-runtime-profiles-smoke
```

When the feature profile is enabled, `make up` builds the runner image before
starting Compose. `scripts/workspace_execution_smoke.py` enters the running
`orc-workspace-execution` container, creates a short session, runs a tiny
command inside the disposable copy, checks diff/artifacts, and closes the
session.

`make material-runtime-profiles-smoke` validates the material path profile:
`symbiont`, `execution-policy-operator`, `material-builder`,
`workspace-execution` and `material-execution-kernel`.

Workspace source materialization copies only useful project inputs. Recursive
workspace snapshots skip cache/build/dependency directories such as `.git`,
`.local`, `.venv`, `node_modules`, `__pycache__`, `build` and `dist`; callers
that need generated dependencies should recreate them inside the disposable
session. If materialization fails, the partial scratch session is removed before
returning the structured error.

Host path inputs are supported only as read-only snapshots into the disposable
session. They are intentionally generic for personal Linux machines:

- when the user directly asks the system to analyze/read a specific absolute
  path, callers send `kind=host_path` with `access_origin=direct_user_request`;
  that request grants a task-scoped read-only snapshot for that path;
- when the system infers that another host path would be useful, callers send
  `access_origin=system_inferred`; the request fails closed with
  `host_read_approval_required` unless `user_approved=true` and an
  `approval_id` are present;
- host path source materialization never writes to the user machine; durable
  writes and publication remain owned by `storage_guardian`;
- the configured host/container mount is only a physical visibility bridge for
  read-only snapshots, not a preapproved root list or scenario template.

Host path source shape:

```json
{
  "kind": "host_path",
  "path": "/absolute/path/requested-by-user",
  "access_origin": "direct_user_request",
  "read_only": true
}
```

Initial request shape:

```json
{
  "idempotency_key": "optional-user-or-runtime-key",
  "source": {
    "kind": "workspace",
    "root_ref": "ai-local",
    "paths": ["features"]
  },
  "execution_profile": "standard",
  "network": "disabled",
  "ttl_seconds": 3600,
  "metadata": {
    "requested_by": "orchestrator"
  }
}
```

Initial command shape:

```json
{
  "idempotency_key": "optional-command-key",
  "cwd": ".",
  "argv": ["pytest", "tests/orchestrator/test_agentic_command_tool.py"],
  "stdin_ref": null,
  "timeout_seconds": 120,
  "risk_evidence_ref": "execution_policy:report-id",
  "allow_profile": "test",
  "validation_profile": "python-basic"
}
```

Batch file materialization shape:

```json
{
  "idempotency_key": "mat_x:batch:1",
  "root": "project-root",
  "vm_session_id": "vm_x",
  "files": [
    {
      "path": "app/main.py",
      "content_b64": "cHJpbnQoJ29rJykK",
      "sha256": "sha256:..."
    }
  ],
  "mode": "replace",
  "verify_hashes": true,
  "forbid_symlink_escape": true,
  "requires_vm_backed_sandbox": true
}
```

Patch apply shape:

```json
{
  "idempotency_key": "mat_x:patch:issue_1",
  "vm_session_id": "vm_x",
  "patches": [
    {
      "path": "app/main.py",
      "expected_old_sha256": "sha256:...",
      "unified_diff": "--- a/app/main.py\n+++ b/app/main.py\n@@ -1 +1 @@\n-old\n+new\n"
    }
  ],
  "verify": true,
  "forbid_symlink_escape": true,
  "requires_vm_backed_sandbox": true
}
```

Command responses must be structured:

```json
{
  "run_id": "run_123",
  "status": "completed",
  "exit_code": 0,
  "stdout_ref": "log:stdout",
  "stderr_ref": "log:stderr",
  "duration_ms": 4200,
  "changed": false,
  "diff_ref": null,
  "artifacts": []
}
```

Raw stdout/stderr are evidence data, not the cross-agent control interface.

Validation profiles are typed hints owned by this feature. They do not bypass
`execution_policy_operator` or orchestrator policy; they only declare the runner tools needed
for a validation command. If an expected tool such as `python`, `pytest`,
`curl`, `docker`, `node` or `npm` is unavailable in the runner, the command
returns `status="blocked"` with error code `validation_tool_unavailable`
instead of turning the validation into a silent skip.

Material-generation callers should choose the narrowest profile that matches
the generated project evidence. Runner network remains disabled unless the
selected validation profile explicitly advertises `allows_network=true`; Docker
CLI checks must use a profile that advertises `allows_docker_cli=true`.
Profiles can also advertise `network_scope`, `requires_validation_command` and
`supervises_background_services`. For example, `python-api` uses VM-local
loopback for bounded API health checks but still does not enable external
network egress.

| Profile | Use |
| --- | --- |
| `python-basic` | Python syntax/import smoke such as `python -m compileall .`. |
| `python-pytest` | Generated pytest suites. |
| `python-api` | API smoke tests that need Python, pytest and curl. |
| `docker-compose-static` | Static compose validation such as `docker compose config` or `docker-compose config`. |
| `docker-compose-runtime` | Controlled compose build/up/log/down validation through a VM-backed compose runtime proxy when configured; otherwise blocks with `docker_runtime_unavailable`. |
| `stateful-postgres` | PostgreSQL persistence smoke checks. |
| `stateful-redis` | Redis event or queue smoke checks. |
| `worker-queue` | Submit job, observe worker processing, verify final state. |
| `cli` | CLI help, submit and inspect/status checks. |
| `artifact` | Generated artifact expected-root/hash validation. |

## Idempotency

- Session create idempotency key: caller key plus source descriptor hash,
  execution profile, and TTL class.
- Command run idempotency key: session id plus command argv/cwd/stdin hash,
  input state hash, and execution profile.
- Artifact identity: content hash plus relative artifact path and media type.
- Publish idempotency key: session id plus artifact content hash plus target
  storage intent supplied by `storage_guardian`.

Retries must return the prior completed record when the idempotency key and
state hash match. A state mismatch must fail closed.

## Execution Model

The target runtime has two layers:

1. A manager service exposes the feature API, records sessions, enforces TTLs,
   and requests policy evidence.
2. Short-lived runner containers execute commands with only the session copy and
   artifact directory mounted.

Runner containers must default to:

- non-root user;
- dropped Linux capabilities;
- no Docker socket;
- no host project or storage write mounts;
- read-only root filesystem where practical;
- memory, CPU, PID, and timeout limits;
- network disabled unless a policy-approved profile enables it;
- explicit environment allowlist;
- optional `runsc`/gVisor runtime when
  `WORKSPACE_EXECUTION_SANDBOX_RUNTIME=runsc`;
- explicit fallback to Docker only when
  `WORKSPACE_EXECUTION_REQUIRE_RUNTIME=false`;
- output redaction before logs leave the feature.

The manager may need access to a Docker or container runtime API, but that
access must not be mounted into child runners.

## VM-Backed Material Execution Contract

Generated or untrusted material commands must set
`requires_vm_backed_sandbox=true`, or equivalent metadata such as
`generated_project_trust=untrusted` or `must_use_vm_backed_sandbox=true`.

When that requirement is present, this feature blocks command execution unless
the request references a ready VM-backed session with isolation proof. If the VM
backend is unavailable, the response is a typed `vm_runtime_unavailable` or
backend-specific setup issue; there is no fallback to host execution.

VM session contracts expose:

- requested image/profile/resource limits;
- `host_execution_used=false`;
- `host_docker_socket_exposed=false`;
- `fallback_to_host_allowed=false`;
- `network_mode=none` by default;
- env scrub and writable-root proof fields;
- cleanup evidence.

The active local VM backend is `microvm`. It runs short-lived QEMU/KVM VMs with
the trusted `ai-local-command-sandbox:latest` root filesystem serialized as an
initramfs, network disabled, and results returned through a structured serial
envelope. Generated project content is passed into the VM as data and commands
run as an unprivileged VM user. The backend is wired through central `config/`
generated environment values such as `WORKSPACE_EXECUTION_VM_BACKEND`,
`WORKSPACE_EXECUTION_VM_IMAGE_REF`, `WORKSPACE_EXECUTION_VM_KERNEL_PATH`,
`WORKSPACE_EXECUTION_VM_KVM_DEVICE`, `WORKSPACE_EXECUTION_VM_CACHE_ROOT` and
resource limits.

Current V3.2 state:

- VM lifecycle, batch write and patch contracts exist;
- capabilities expose `vm_backed_sessions_required_for_generated_code=true`;
- capabilities expose `host_execution_fallback=false`;
- when QEMU/KVM and a readable kernel are available, capabilities expose
  `vm_backed_sessions=true`, `vm_runtime_status=ready` and
  `vm_backend=microvm`;
- `microvm` batch writes, commands, patches and artifact packaging restore
  results back into the disposable session with `host_execution_used=false`;
- the trusted VM rootfs includes Docker CLI and Docker Compose CLI for static
  Compose validation such as `docker-compose config`, plus `curl` and
  `iproute2` for bounded VM-local API probes, without exposing a host Docker
  socket;
- the VM init brings `lo` up for loopback-only service probes; QEMU still boots
  with `-net none`, so this is not external network access;
- the VM command runner scrubs env by default, removes Docker host variables and
  executes with network `none`;
- `vm-proxy`/`external` without a control URL still report
  `vm_runtime_status=not_configured`, and configured backends without an
  implemented workspace client report `vm_runtime_status=unimplemented`;
- material sessions block rather than execute generated code on the host when a
  VM backend is not ready.
- Docker Compose static validation is covered by the VM-backed command path;
- Docker Compose runtime validation (`build`, `up`, `logs`, `down`) is delegated
  only to a configured VM-backed compose runtime proxy that advertises
  `compose_runtime=true`, `vm_backed=true`, `host_execution_used=false`,
  `host_docker_socket_exposed=false` and `fallback_to_host_allowed=false`;
- the active in-repo proxy implementation is a dedicated Docker-in-Docker
  runtime used as a VM-backed compose proxy: the host control plane starts only
  trusted runtime containers, and generated project containers are created
  inside the disposable inner daemon without receiving the host Docker socket;
- compose runtime responses include service names, inner container ids, health
  observations, log collection status, and `docker-compose down -v` cleanup
  evidence; completion consumers must treat missing cleanup after runtime
  validation as blocking evidence;
- profiles that require an isolated container runtime declare
  `requires_isolated_container_runtime=true`; without a safe configured proxy
  they block with typed `docker_runtime_unavailable` evidence.
- `python-api`, `stateful-postgres`, `stateful-redis`, `worker-queue` and `cli`
  are generic contract-driven smoke profiles. They require project-specific
  validation commands supplied by the material contract or observed capability
  metadata; the workspace owner must not infer framework-specific commands.
  Failed smokes return typed profile errors such as
  `api_health_smoke_failed`, `stateful_service_unhealthy`,
  `worker_queue_smoke_failed` or `cli_smoke_failed`.

Compose runtime proxy config is owned by the root config center:

```text
WORKSPACE_EXECUTION_COMPOSE_RUNTIME_URL=
WORKSPACE_EXECUTION_COMPOSE_RUNTIME_TOKEN_FILE=
WORKSPACE_EXECUTION_COMPOSE_RUNTIME_TIMEOUT_SECONDS=30
```

An empty URL keeps the fail-closed default. The proxy contract is:

```text
GET  /v1/compose-runtime/capabilities
POST /v1/compose-runtime/run
```

The run request sends the session workspace as an archive plus command metadata.
Returned workspace/artifact archives are restored only after the response
proves VM-backed isolation and no host Docker socket exposure.

Runtime verification:

```bash
make vm-workspace-runtime-smoke
make material-kernel-smoke
make material-runtime-profiles-smoke
python scripts/workspace_vm_smoke.py
python scripts/command_sandbox_audit.py
docker compose --profile core --profile storage --profile material config --quiet
```

These commands must exercise the live VM-backed owner path. They are not
documentation-only checks.

## Policy Actions Draft

The orchestrator policy registry should receive explicit actions when runtime
implementation begins:

- `workspace.sandbox.create`: medium risk.
- `workspace.sandbox.read`: low risk.
- `workspace.sandbox.execute`: medium risk.
- `workspace.sandbox.destructive`: high risk.
- `workspace.sandbox.publish`: medium risk, because storage guardian still owns
  the durable write.
- `workspace.sandbox.apply_real`: deny. Applying changes to real host paths is
  outside this feature.

The final names may change only when the capability manifest and policy registry
are updated together.

## Events

The feature should publish structured lifecycle evidence for:

- session created;
- input attached;
- command started;
- command completed;
- diff generated;
- artifact discovered;
- artifact published;
- session closed;
- session cleanup completed or failed.

The orchestrator event ledger remains the audit source for autonomous runtime
decisions.

## Migration Plan

1. Create this spec and local skill.
2. Add the runtime package, local contracts, and tests.
3. Add central config, generated env, compose service, Docker image, and Docker
   governance catalog entries.
4. Add feature capability manifest entries and explicit orchestrator policy
   actions.
5. Implement session create/close with TTL cleanup.
6. Implement snapshot/input materialization without managed storage writes.
7. Implement command execution in isolated runner containers.
8. Implement diff and artifact listing.
9. Implement artifact publication through `storage_guardian`.
10. Migrate the existing read-only command tool to call this API for execution
    profiles that this feature owns, then remove duplicate runner ownership from
    the orchestrator path.

## Verification

Docs and guidance changes:

```bash
find features/workspace_execution -maxdepth 5 -type f -print
sed -n '1,40p' features/workspace_execution/.agents/skills/workspace-execution/SKILL.md
git diff --check -- features/workspace_execution
```

Future code changes should add targeted local tests under
`features/workspace_execution/tests/`, plus orchestrator dispatch,
policy registry, Docker governance, config resolver, and storage publication
tests whenever those boundaries change.
