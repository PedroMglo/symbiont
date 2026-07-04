# Extrator Contract Spec

## Scope

`features/extrator` owns document ETL contracts for extracted evidence,
normalized chunks, table catalogs, conversion records, and non-executing
workspace sandbox preparation plans.

It does not own embeddings, durable storage lifecycle, archive/restore policy,
or sandbox execution. Durable publication must go through `storage_guardian`;
sandboxed command execution must go through `workspace_execution`.

## DocumentEvidence v1

Every completed extraction should expose a versioned `DocumentEvidence`
artifact. The artifact is a JSON contract, not an embedding input by itself.

Required fields:

- `contract_version`: current value `document_evidence.v1`.
- `doc_id`, `source_path`, `source_type`, `mime_type`.
- `source_hash`: SHA-256 of the source bytes.
- `parser_id`, `parser_version`, and optional `parser_confidence`.
- `parser_selection`: ordered candidate list, selected parser, parser attempts,
  and whether fallback was used.
- `quality_metrics`: parser quality signals such as extraction loss, table
  fidelity, chunk stability hash, and emitted chunk count.
- `warnings`: parser or extraction warnings visible to downstream consumers.
- `truncation`: explicit status showing whether content was truncated.
- `security_decisions`: validation decisions made by the extrator boundary.
- `output_paths`: produced local paths or storage refs for generated artifacts.
- `chunks`: `ChunkEvidence` summaries for emitted chunks.
- `tables`: `TableEvidence` summaries for emitted tables.

`ChunkEvidence` and `TableEvidence` must carry the same contract version,
source hash, parser id, parser version, warnings, truncation, and security
decision shape so RAG or material generation can reason about provenance
without importing extrator internals.

`ConversionEvidence` represents a conversion job output. It records the source
path, optional source hash when available, output format, output path/status,
warnings, and security decisions. It does not imply durable storage ownership.
When no explicit conversion destination is provided, path conversions request
materialization next to the source file using the same basename and the target
extension. Directory conversion requests apply that rule per converted file.
The final filesystem write is performed through `storage_guardian`; `extrator`
may only stage conversion artifacts in scratch space before publication.

## Query Action Contract

`POST /v1/extrator/query` must declare the action it took:

- `created_job`: a new extraction or conversion job was created and queued.
- `reused_result`: an existing completed extraction result matched the query.
- `blocked`: the query could not be processed because the requested source was
  missing, invalid, outside policy, or otherwise rejected.
- `no_action`: no processable document or conversion intent was found.

The action must be present as a top-level response field and repeated in
`metadata.query_action` for callers that only inspect metadata.

When `/v1/extrator/query` is called by an orchestrator or another runtime
component with typed metadata, the owner-published capability/action metadata is
the authority for choosing extraction vs conversion. Capabilities
`document_etl`, `document_extraction`, and `rag_bundle`, or action `extract`,
must select an extraction job even if the natural-language query includes
incidental output-format words from the original user prompt. Capability
`file_conversion` or action `convert` may select a conversion job when the
query also supplies a supported conversion target format. The text parser is a
fallback for direct natural-language calls that do not include typed routing
metadata.

Query responses that select a path include `metadata.job_kind` and
`metadata.job_kind_source`, where `metadata_capability` means the decision came
from the typed caller contract and `query_text` means it came from the natural
language parser.

## DocumentDiagnostic v1

`POST /v1/extrator/diagnostics/path` exposes a side-effect-free preflight
diagnostic for document autonomy. It validates the requested path against the
extrator boundary and returns:

- `sensitivity`: generic sensitivity level and matched signals from filename or
  a bounded text sample;
- `language`: deterministic language hint and confidence when text is available;
- `structure`: path kind, extension, source type, MIME, size, and structural
  hints such as tabular, code, or multi-document input;
- `ocr`: whether OCR is needed, impossible to know before parsing, or not
  expected, plus current OCR enablement;
- `cost`: coarse extraction/conversion cost tier and bounded size/item
  estimates;
- `workflow`: the recommended next action: `extract`, `convert`,
  `sandbox_required`, or `blocked`.

The diagnostic is advisory metadata. It must not manage durable storage, run
embeddings, or execute risky conversion commands. Queries that select a path
also include `metadata.document_diagnostic` so the orchestrator can decide from
owner-published contracts rather than hardcoded extension lists.

## Parser Intelligence v1

Parser selection must be explainable. `parse_file()` records:

- ordered candidate parsers;
- each attempted parser with status `selected` or `unavailable`;
- unavailable reason when an adapter declines work;
- whether a fallback was used before the selected parser succeeded.

Fallback from an unavailable parser to a later parser is a parser-selection
event, not a silent success.

The extraction pipeline must attach deterministic parser quality metrics to
completed evidence:

- `extraction_loss`: heuristic loss based on source bytes versus extracted
  markdown bytes;
- `table_fidelity`: ratio of populated emitted tables when tables are expected
  or present;
- `chunk_stability`: current-run deterministic chunking signal plus a hash of
  emitted chunk content hashes.

Golden corpus tests should cover parser selection and these quality signals for
specialized local formats before optional generic parsers.

## RAG Bundle v1

Every completed extraction should expose a `rag_bundle.v1` manifest for
`obsidian-rag`.

The manifest is produced by `extrator`, but it must state:

- `consumer`: `obsidian-rag`;
- `embedding_owner`: `obsidian-rag`;
- `storage_owner`: `storage_guardian`;
- `embeddings_included`: always `false`;
- artifact refs for normalized chunks, document evidence, tables, graph
  candidates, metadata, normalized markdown, and optional source reference;
- SHA-256 hashes for artifacts when local scratch files are available;
- chain-of-custody data anchored on the source hash and document evidence;
- whether RAG can reprocess without rereading the original source.

`can_reprocess_without_original` may be true only when normalized chunks and
`DocumentEvidence` are present. Consumers must still treat durable publication
as a `storage_guardian` responsibility; `extrator` must not write durable
objects except through that API.

## Managed Storage Projections

When `extrator` publishes extraction artifacts, it must ask
`storage_guardian` to create service-local managed projections under the
configured `extrator` stores. Source copies belong to the `extrator_uploads`
store and normalized/evidence/RAG artifacts belong to the `extrator_silver` or
`extrator_gold` stores. Reuse of completed extraction results is valid only
when the manifest records the current projection contract and each published
artifact has a managed projection path.

Current projection contract: `extrator_storage_projection.v3`.

Projection folders must be human-readable and versioned by the first managed
publication time for a source file version. Each extracted source file is
projected under a date folder and then a file/time folder:

```text
input/YYYY-MM-DD/<sanitized-original-filename>__HHMMSSffffffZ/<artifact>
output/YYYY-MM-DD/<sanitized-original-filename>__HHMMSSffffffZ/<artifact>
```

For example:
`output/2026-07-01/Relatório.docx__091011654321Z/document.md`.

Reprocessing the same source bytes with the same source hash, source type,
configuration hash and current projection contract must reuse the completed
extraction even when the file is requested through a different path, mount or
partition. The path is provenance, not the reusable identity. A changed source
hash or incompatible source type creates a new extraction and timestamped
folder. The timestamped folder is a managed Storage Guardian projection, not
the source of truth; immutable object records and hashes remain authoritative
for provenance and reuse decisions.

The extrator manifest is owner-local critical state and must be configured on a
persistent state root in runtime deployments. Scratch paths such as `/temp` may
hold staging artifacts, but they must not be the only index that proves whether
source bytes were already extracted. If a runtime manifest is missing or
recreated while managed projections still exist, extrator may rebuild its local
manifest from the published `gold`, `silver`, and `uploads` projections by
matching `source_hash` and compatible `source_type`, then restore chunk/table
records needed by downstream semantic digests. This recovery path is generic
for managed projections and must not depend on a user path, machine partition,
benchmark folder, or document name.

## Capability Manifest

`features/extrator/service_capabilities.toml` is the owner-published capability
manifest for the orchestrator. It advertises document diagnosis, workflow
selection, extraction, conversion, RAG bundle production, and sandbox
preparation as metadata only. It does not duplicate parser, storage, RAG, or
execution behavior outside the feature owner.
