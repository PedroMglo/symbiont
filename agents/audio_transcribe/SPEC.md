# Audio Transcribe Agent Spec

## Owner

`agents/audio_transcribe` owns local speech-to-text behavior:

- batch transcription jobs from validated `input_path`
- multipart upload ingestion
- realtime STT streaming over authenticated WebSocket
- VAD-driven segmentation for STT
- transcription job lifecycle, recovery, cleanup and queue observability
- transcript, subtitle, metadata and RAG-ready export contracts
- audio transcription request/response models

It does not own microphone capture, PipeWire/WirePlumber, host audio devices,
playback, TTS, wake word, push-to-talk, turn management, barge-in, final answer
synthesis or agentic tool execution.

## Public Interfaces

Batch base URL: `https://audio-transcribe:8080`

Streaming base URL: `https://audio-streaming:8087`

Authentication is fail-closed by default. Runtime requests use
`X-API-Key: <token>` or `Authorization: Bearer <token>`. The token is loaded
from `AUDIO_TRANSCRIBE_API_KEY`, `AUDIO_TRANSCRIBE_API_KEY_FILE`, or the Docker
secret `/run/secrets/audio_transcribe_api_key`. Local unauthenticated dev mode
requires `AUDIO_TRANSCRIBE_SECURITY_ALLOW_UNAUTHENTICATED_DEV=true`.

| Method | Path | Contract |
| --- | --- | --- |
| `GET` | `/health` | Operational health, GPU/model state, queue stats and recovery counters |
| `POST` | `/v1/transcribe` | Canonical dispatch endpoint: `AudioQueryRequest -> AudioQueryResponse` |
| `POST` | `/transcriptions` | Create job from validated container-visible `input_path` |
| `POST` | `/transcriptions/upload` | Create job from multipart upload with chunked quota enforcement |
| `GET` | `/transcriptions/{job_id}` | Job status/progress |
| `GET` | `/transcriptions/{job_id}/result` | Completed job result |
| `GET` | `/transcriptions` | List jobs |
| `POST` | `/transcriptions/{job_id}/cancel` | Cancel queued/running job |
| `DELETE` | `/transcriptions/{job_id}` | Delete job scratch outputs |
| `POST` | `/cleanup?dry_run=true|false` | Delete expired terminal job outputs |
| `GET` | `/models` | Supported transcription models |
| `GET` | `/config` | Sanitized runtime config |
| `GET` | `/metrics` | Queue/job/upload/recovery/latency metrics |
| `WS` | `/ws/stream` | Realtime PCM16 STT stream on the streaming service |

## Realtime Streaming Contract

Canonical internal URL: `wss://audio-streaming:8087/ws/stream`.

The WebSocket must authenticate before `accept()`. Tokens are accepted only via
`X-API-Key` or `Authorization: Bearer`; raw query-string tokens are not part of
the contract.

Client sequence:

1. Send JSON `RealtimeStreamConfig`.
2. Send binary PCM16 little-endian mono frames at 16 kHz by default.
3. Send text `END` to close the session.

Server event types:

- `session_started`
- `speech_started`
- `segment_submitted`
- `partial_transcript`
- `final_transcript`
- `stream_warning`
- `stream_error`
- `session_closed`

Partials are unstable context. Only `final_transcript` may be forwarded to the
orchestrator for user-visible action, tool execution, or answer synthesis. More
aggressive streaming execution requires a future local-agreement policy in the
voice runtime, not a silent change to this STT contract.

Public realtime types live under `streaming.types` and are implemented without a
top-level `types.py` file because `agents/audio_transcribe/streaming` is placed
directly on `PYTHONPATH` by current tests and runtime scripts.

## Queue And Recovery

On startup the service loads persisted jobs and requeues persisted
`queued`/`running` jobs as `queued`. Redis-backed queues expose pending,
processing and dead-letter counts, retry failed processing up to the configured
budget, and can recover stale `processing` entries.

Config knobs:

- `AUDIO_TRANSCRIBE_JOBS_REDIS_RETRY_ATTEMPTS`
- `AUDIO_TRANSCRIBE_JOBS_REDIS_PROCESSING_TIMEOUT_SECONDS`
- `AUDIO_TRANSCRIBE_JOBS_JOB_TTL_HOURS`

## Storage

Scratch inputs and outputs must stay under configured audio scratch roots and
must pass scratch-path validation. In the Docker runtime, the persisted owner
tree is `${AUDIO_TRANSCRIBE_DATA_DIR}` with `input/`, `output/` and `tmp/`
subdirectories; `input/` is read-only to the service and completed exports
remain under `output/<job_id>/` using the defined layout (`input`,
`processed_audio`, `segments`, `checkpoints`, `transcripts`, `subtitles`,
`rag_ready`, `metadata`, `logs`). Durable publication is delegated to
`storage_guardian`. Export publication is required by default:

- `AUDIO_TRANSCRIBE_EXPORT_PUBLISH_POLICY=required` fails the export when
  durable publication fails.
- `AUDIO_TRANSCRIBE_EXPORT_PUBLISH_POLICY=optional` is reserved for isolated
  development/tests and keeps local scratch paths only inside validated scratch
  roots.

Published transcript artifacts carry `audio_transcription_reuse.v1` metadata
and the current managed projection contract
`audio_transcription_projection.v2`: source content hash,
privacy-preserving source path hash, requested transcription options, job id,
artifact kind and the Storage Guardian managed projection. Natural-language
transcription requests may reuse active Storage Guardian objects with compatible
options before creating a new job. Reuse is valid only when at least one primary
textual artifact (`transcript_txt`, `transcript_md`, `transcript_clean_json` or
`rag_ready_json`) is readable through the Storage Guardian read-text contract,
has a `projection_materialized_path` recorded by Storage Guardian and matches
the current projection contract version. Metadata-only, orphaned, pre-projection
or older-projection Storage Guardian records must be skipped so the owner can
reprocess and publish a fresh durable transcript with the expected managed
layout. This reuse remains owned by `audio_transcribe`; the orchestrator only
routes the request and must not duplicate the matching logic.

The reusable identity for batch audio is the source input content hash plus the
compatible transcription options, not only the path or job id. If the same audio
bytes are requested again from a different path, the agent should reuse the
newest compatible completed local job or active Storage Guardian publication
instead of creating a second transcription job. Managed Storage Guardian
projections for published transcript artifacts must be grouped by date and then
by a human-readable file/time folder:

```text
output/YYYY-MM-DD/<sanitized-original-filename>__HHMMSSffffffZ/<artifact>
```

The source input content hash and compatible transcription options remain the
reuse identity and are recorded in metadata; the visible folder layout is for
user navigation, while immutable object/version history remains in Storage
Guardian.

When a natural-language request reuses published transcript objects, the agent
must attach a bounded semantic digest for downstream evidence consumers whenever
Storage Guardian can read the owned transcript text. The digest may be built
from `transcript_txt`, `transcript_md`, `transcript_clean_json` or
`rag_ready_json` artifacts through the Storage Guardian read-text contract. If
the text cannot be read, the response must keep the Storage Guardian refs and
state the missing semantic evidence instead of implying that transcript content
was inspected. When a request waits for a queued/running local job and the job
reaches `completed`, the response must refresh the job metadata from the
persisted job record so downstream consumers receive outputs, summaries and
semantic digest material in the same call.

Natural-language dispatch requests may be generated by another local service.
For those requests, transcription language must come from explicit request
metadata (`audio_language`, `language`, `language_hint` or `user_language`) or
remain `auto`. The owner must not infer a forced language from generic
system-generated wording such as “reuse audio evidence”, because that wording is
not evidence about the audio language.

No durable user-machine writes, archive/restore decisions, or managed storage
policy may be implemented in this agent.

## Orchestrator Boundary

`agents/service_capabilities.toml` is the owner-published dispatch and routing
metadata source for `audio_transcribe`. The orchestrator gateway may consume
that manifest, but it must not import audio-specific private term lists or
duplicate audio path parsing.

The orchestrator owns policy, ledger, final answer synthesis, dispatch and tool
execution. It calls `/v1/transcribe` through the dispatch boundary.

## Voice Runtime Boundary

Full local-first voice is a sibling runtime, not an expansion of STT ownership.
`features/voice_runtime` owns the future host-side voice plan:

- PipeWire/WirePlumber microphone and playback boundary
- `voice_gateway` running in the user session, not in the audio container
- push-to-talk, wake word, physical VAD, AEC, ducking and barge-in
- turn manager and final transcript forwarding to orchestrator
- local TTS service integration and playback cancellation

`audio_transcribe` receives audio frames or file refs and returns transcripts.

## Verification

Targeted audio suite:

```bash
env PYTHONPATH=agents/audio_transcribe:agents/audio_transcribe/streaming:storage_guardian/src \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  pytest -p pytest_asyncio.plugin --import-mode=importlib tests/agents/audio_transcribe -q
```

Gateway/manifest contract checks:

```bash
env PYTHONPATH=$HOME/_projects/ai-local PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python -m pytest -p pytest_asyncio.plugin --import-mode=prepend \
  tests/orchestrator/test_audio_handler_paths.py \
  tests/orchestrator/test_capability_schema_refs.py::test_capability_manifest_schema_refs_resolve_to_model_schemas \
  tests/orchestrator/test_agentic_runtime.py::test_agentic_manifests_are_phase_1_complete -q
```
