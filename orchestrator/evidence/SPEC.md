# Orchestrator Evidence Helpers

The orchestrator owns generic evidence acquisition needed to route and ground
agentic work. It may inspect user-requested local workspaces in read-only mode
to build compact context for downstream agents, but it must not implement
feature, agent, storage, RAG, or document-processing domain behavior.

## Scope

- Resolve explicit local paths mentioned by the user or normalized prompt.
- Map host paths to container-visible roots when generated config exposes those
  mappings.
- Perform bounded, read-only workspace discovery.
- Produce compact evidence context for routing, prewarming, material planning,
  and final synthesis.
- Record what was inspected, what was skipped, and why.

## Boundaries

- The orchestrator does not extract documents, transcribe audio, publish files,
  own storage lifecycle, or parse domain-specific content beyond safe metadata
  and short text excerpts.
- Document extraction belongs to `features/extrator`.
- Audio transcription belongs to `agents/audio_transcribe`.
- Durable publication and managed storage paths belong to `storage_guardian`.
- Material file planning/content belongs to `agents/material_builder`.

## Safety

- Default mode is read-only.
- Discovery is progressive and bounded by file count, depth, bytes, and excluded
  directory rules.
- The helpers must not search the whole filesystem to infer a project.
- Hidden benchmark/evaluation files must be skipped by default.
- Large or unsupported files are represented by metadata and warnings instead of
  being fully read.

## Material Context Contract

For material-output tasks that mention a local path, the gateway may attach a
`material_evidence_context` metadata field. The value is a compact
`EvidenceBundle`-like dictionary containing:

- `workspace`
- `boundary_root`
- `workspace_map`
- `relevant_files`
- `file_observations`
- `commands`
- `enrichment_plan`
- `evidence_summary`
- `missing_evidence`
- `confidence`
- `cache_fingerprint`

The bundle is routing/planning evidence only. Downstream owners decide whether
to reuse existing extractor/transcriptor outputs, run fresh owner pipelines, or
record missing evidence.

## Owner Enrichment

When a compact material evidence bundle identifies documents or media, the
orchestrator may ask the owning services for semantic digests through dispatch
HTTP calls:

- `features/extrator` for document/office/PDF/tabular extraction digests.
- `agents/audio_transcribe` for audio/video transcription digests.

The orchestrator must treat these calls as owner responses. It may attach
compact `enrichment_results` to the material context, but it must not parse
owner private storage, import owner packages, or present a missing digest as
observed content.

System-generated owner requests must preserve language context separately from
the internal working prompt. In particular, audio transcription language must be
passed as an explicit metadata hint, or left as `auto`; the English text of an
orchestrator-generated dispatch query must not force an English transcription.
