# Storage Guardian Local-First v2 Spec

`storage_guardian` is the only authority for durable filesystem writes and
managed user-machine storage in `ai-local`.

## Default Runtime Profile

The default profile is `user_local`:

- one user;
- one local machine;
- no root daemon;
- no required Postgres, MinIO, S3, NAS, or shared storage;
- SQLite WAL control catalog;
- local content-addressed filesystem blobs;
- local audit, operation ledger, and custody trail.

`project_local` is allowed only for development and tests. `advanced_postgres`
is an optional future catalog backend and must not become a base requirement.

## Local Layout

User-local storage follows XDG where possible:

```text
$XDG_DATA_HOME/ai-local/<project_id>/storage_guardian/
  objects/
  versions/
  directories/
  manifests/
  archives/
  materialized/
  uploads/
  quarantine/
  restore/
  scratch/
  service_stores/
  indexes/

$XDG_STATE_HOME/ai-local/<project_id>/storage_guardian/
  catalog.sqlite
  audit.sqlite
  operation_ledger.sqlite
  custody_events.jsonl
  metrics/
  logs/

$XDG_CACHE_HOME/ai-local/<project_id>/storage_guardian/
  temp/
  staging/
  extraction_cache/
  upload_chunks/
```

Development and tests may explicitly use `<project_root>/.local`, but that is
not the user-machine default for real local installs.

## Authority Rule

No agent, feature, orchestrator flow, material builder, RAG, CAG, or Graphy
caller writes directly into managed storage. Durable operations must cross the
Storage Guardian API, service API, CLI, or typed client contract.

Callers may write only to their scratch area:

```text
scratch/<service>/<session_id>/
```

Project scratch is managed by Storage Guardian under:

```text
<project_root>/.local/data/storage_guardian/scratch/project/
```

The repository root must not contain a `.tmp/` storage tree. Temporary
host/container bridge artifacts such as command output and restore-test
manifests must use `AI_LOCAL_PROJECT_SCRATCH_ROOT`, which is declared in
`storage_schema.scratch_roots` and backed by the managed `temp_outputs` store.
Durable retention starts only after import, object creation, materialization, or
promotion through Storage Guardian.

Scratch data becomes durable only after `storage_guardian` imports, creates,
materializes, or promotes it through an operation ledger entry.

## Schema v2

`config/storage_guardian.yaml` declares immutable schema v2. It must cover:

- services;
- stores;
- expected directories;
- directory modes;
- owners;
- zones;
- policies;
- retention;
- materialization roots;
- scratch roots;
- cache roots;
- protected paths;
- forbidden paths;
- operation permissions.

The schema hash remains mandatory for production config. Startup must reject a
declared hash that does not match the canonical immutable schema.

The canonical schema is the declarative `storage_schema` block before
machine-specific environment expansion. Runtime roots such as
`AI_LOCAL_PROJECT_ROOT`, `STORAGE_GUARDIAN_DATA_DIR`, XDG paths, container bind
paths, and user home paths are validated at runtime, but they must not change
the immutable schema hash. This keeps the same config portable across personal
Linux machines and Docker bind layouts while still detecting architectural
schema drift.

## Directory Registry

Every managed directory has a local registry row:

```text
directory_id
service
store
relative_path
parent_directory_id
owner
zone
mode
policy
expected_by_schema
status
created_by
created_at
last_seen_at
protected
writable_by_storage_guardian
readable_by_callers
```

Statuses are:

```text
expected created active missing orphan external quarantined deprecated blocked
```

Structure reconciliation is dry-run first. Apply may create safe expected
directories and record drift. It must not delete unexpected paths unless a
separate policy-approved purge operation is requested.

## Object Registry

Durable files are registered as objects with immutable content hashes. The
physical blob path is content-addressed. Logical names and materialized paths
are controlled projections owned by Storage Guardian.

Copying an object creates a new logical object reference and reuses the
content-addressed blob when policy allows it. Moving an object is visible in
the ledger and never silently mutates storage state.

## Operation Ledger

Every mutating operation records:

```text
operation_id
operation_type
actor
requesting_service
source_ref
target_ref
source_directory_id
target_directory_id
policy_decision
dry_run_result
idempotency_key
preconditions
hash_before
hash_after
status
started_at
finished_at
custody_event_id
rollback_plan
```

Official mutating operations are:

```text
create_directory create_file create_object create_upload commit_upload
copy_object copy_path move_object move_path rename_object soft_delete
hard_purge materialize import_external_path extract_archive archive restore
promote quarantine reconcile_structure
```

Hard purge is blocked unless policy explicitly enables it. Soft delete is the
default deletion behavior.

## Custody

Every successful mutation emits a custody event. Events must make it possible
to answer who created, moved, copied, renamed, deleted, materialized, imported,
restored, archived, or quarantined a directory or object, when it happened, and
which hashes and refs were involved.

## RAG, CAG, And Graphy

RAG, CAG, and Graphy must use `storage_ref`, `object_id`, `digest_ref`,
`source_hash`, bounded excerpts, and metadata. They must not treat private
managed paths as their primary contract.
