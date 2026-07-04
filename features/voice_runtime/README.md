# Voice Runtime

`features/voice_runtime` is the future local-first voice interaction runtime.
It is intentionally separate from `agents/audio_transcribe`.

Current state: prototype contract active.

See `SPEC.md` for ownership, architecture, planned contracts and verification.

## Fake PCM Gateway

This sends a raw PCM16 mono fixture to `audio-streaming` and prints structured
voice runtime events. It does not access the microphone.

```bash
env PYTHONPATH=features/voice_runtime python -m voice_runtime \
  --pcm /path/to/sample.pcm \
  --api-key-file infra/docker/secrets/audio_transcribe_api_key \
  --url wss://127.0.0.1:9010/ws/stream \
  --language pt
```

Only `voice.final_transcript` events are intended to cross into orchestrator
execution paths.

For a host-side microphone smoke using PipeWire's `pw-record` CLI:

```bash
env PYTHONPATH=features/voice_runtime python -m voice_runtime \
  --record-seconds 4 \
  --api-key-file infra/docker/secrets/audio_transcribe_api_key \
  --url wss://127.0.0.1:9010/ws/stream \
  --language pt
```

This remains a prototype harness, not the production CPAL/Rust gateway.
The host-side smoke path uses the debug-published port
`wss://127.0.0.1:9010/ws/stream`. The internal Docker URL
`wss://audio-streaming:8087/ws/stream` only resolves inside the Compose network.
Some PipeWire builds can return a non-zero exit code after satisfying
`--sample-count`; the harness treats the capture as usable when the PCM file is
present and non-empty, and reports stdout/stderr only when no capture was
produced.
