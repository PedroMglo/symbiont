# storage_guardian

`storage_guardian` is the local-first lifecycle storage manager and durable
filesystem authority for `ai-local`.

The owning spec is [SPEC.md](SPEC.md). The default architecture is Storage
Guardian v2 Local-First: one user, one machine, SQLite WAL catalog, local
content-addressed blobs, directory registry, object registry, operation ledger,
and local custody/audit trail. Postgres or external object stores are optional
advanced backends, not base requirements.

It scans registered stores, classifies files, plans hot/warm/cold lifecycle
actions, archives warm and cold candidates, writes external manifests,
verifies archives, updates a global index, and restores only into a safe
restore directory.

It also exposes an agent storage gateway: agents create immutable objects,
controlled upload sessions, governed directories, copy/move/rename/delete
operations, and materialized outputs through storage_guardian, so policy,
hashes, ownership, zones, operation ledger rows and chain-of-custody stay in
one registry. Agents do not receive direct filesystem write paths for managed
stores.

The orchestrator can also delegate storage questions through
`POST /internal/storage/query`. That endpoint owns the natural-language
mapping to read-only status, archive lookup, explicit archive, restore and
archive-recovery operations; dispatch only routes the request to the storage
authority.

Storage layout is governed by an immutable v2 service schema in
`config/storage_guardian.yaml`. Every managed store and expected directory
belongs to exactly one service, fallback writes are grouped under
`pending_external/services/<service>/stores/<store>`, and the schema hash is
checked at startup.

The current lifecycle profile treats the compressed archive as the canonical
copy after verification:

- original sources are removed after archive verification when
  `destructive_actions_enabled=true` and `delete_original_sources=true`
- live Qdrant storage and live DB files are blocked
- unregistered paths are ignored
- lossy transforms are blocked
- restore never overwrites an existing path
- model stores are cataloged unless explicitly configured as managed
- no backup copy of the original source is created

## Commands

```bash
python -m storage_guardian.cli --config config/storage_guardian.yaml status
python -m storage_guardian.cli --config config/storage_guardian.yaml effective-config
python -m storage_guardian.cli --config config/storage_guardian.yaml scan
python -m storage_guardian.cli --config config/storage_guardian.yaml plan
python -m storage_guardian.cli --config config/storage_guardian.yaml run-cycle
python -m storage_guardian.cli --config config/storage_guardian.yaml archive-members path/to/archive.manifest.json
python -m storage_guardian.cli --config config/storage_guardian.yaml read-archive-text path/to/archive.manifest.json relative/path.txt
python -m storage_guardian.cli --config config/storage_guardian.yaml storage-policies
python -m storage_guardian.cli --config config/storage_guardian.yaml storage-schema
python -m storage_guardian.cli --config config/storage_guardian.yaml directories
python -m storage_guardian.cli --config config/storage_guardian.yaml operations
python -m storage_guardian.cli --config config/storage_guardian.yaml create-object --agent graphify --store graph_exports --logical-name summary.json --content-base64 eyJvayI6dHJ1ZX0= --idempotency-key graphify-summary-001
python -m storage_guardian.cli --config config/storage_guardian.yaml create-dir --agent graphify --store graph_exports --relative-path reports --idempotency-key graphify-reports-dir-001
python -m storage_guardian.cli --config config/storage_guardian.yaml copy-object obj_x --agent graphify --target-logical-name summary-copy.json --idempotency-key graphify-copy-001
python -m storage_guardian.cli --config config/storage_guardian.yaml move-object obj_x --agent graphify --target-logical-name summary-final.json --idempotency-key graphify-move-001
python -m storage_guardian.cli --config config/storage_guardian.yaml rename-object obj_x --agent graphify --logical-name summary-final.json --idempotency-key graphify-rename-001
python -m storage_guardian.cli --config config/storage_guardian.yaml create-upload --agent graphify --store graph_exports --logical-name summary.json --expected-size 11 --sha256 sha256:<digest>
python -m storage_guardian.cli --config config/storage_guardian.yaml append-upload upl_x path/to/summary.json
python -m storage_guardian.cli --config config/storage_guardian.yaml commit-upload upl_x --sha256 sha256:<digest> --idempotency-key graphify-upload-001
python -m storage_guardian.cli --config config/storage_guardian.yaml promote-object obj_x --agent graphify --target-zone approved
python -m storage_guardian.cli --config config/storage_guardian.yaml delete-object obj_x --agent graphify --reason superseded
```

Docker Compose profile:

```bash
make storage-guardian-up
```

API:

```text
GET  /health
GET  /status
GET  /stores
GET  /archives
GET  /archives/{archive_id}
GET  /archives/{archive_id}/summary
GET  /storage/policies
GET  /storage/schema
GET  /storage/directories
GET  /storage/objects
GET  /storage/objects/{object_id}
GET  /storage/operations
POST /internal/scan
POST /internal/plan
POST /internal/run-cycle
POST /internal/storage/query
POST /internal/archive-recovery/inspect
POST /internal/storage/objects
POST /internal/storage/directories
POST /internal/storage/uploads
PUT  /internal/storage/uploads/{upload_id}
POST /internal/storage/uploads/{upload_id}/commit
POST /internal/storage/copy/{object_id}
POST /internal/storage/move/{object_id}
POST /internal/storage/rename/{object_id}
POST /internal/storage/objects/{object_id}/read-text
POST /internal/storage/materialize
POST /internal/storage/promote/{object_id}
POST /internal/storage/delete/{object_id}
POST /internal/restore
POST /internal/restore/{archive_id}
POST /internal/read-archive-text
GET  /archives/{archive_id}/members
GET  /metrics
GET  /effective-config
```

`/effective-config` exposes the derived values calculated from
`config/storage_guardian.yaml`.
