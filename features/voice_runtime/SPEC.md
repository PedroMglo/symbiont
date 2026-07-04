# Voice Runtime Feature Spec

## Status

Prototype contract active. This feature now includes typed voice turn contracts,
a deterministic turn manager, and a host-side fake PCM gateway harness for
testing the audio streaming boundary without a real microphone.

It also includes an optional `pw-record` capture harness for host smoke tests.
It intentionally does not implement production microphone device selection,
playback, TTS, wake word, AEC, ducking or barge-in yet.
The `pw-record` harness accepts a non-zero process exit when the raw PCM output
file exists and is non-empty, because some PipeWire/pw-cat combinations return
non-zero after completing a bounded `--sample-count` capture.

## Owner

`features/voice_runtime` owns the voice interaction runtime around STT, not STT
itself.

Owned responsibilities:

- host-side `voice_gateway` lifecycle and configuration
- PipeWire/WirePlumber device graph integration on Linux
- microphone capture, playback, device selection and session permissions
- push-to-talk as the first validated interaction mode
- future wake word, physical VAD, AEC, ducking and barge-in policy
- turn manager states: `idle`, `listening`, `user_speaking`, `transcribing`,
  `thinking`, `speaking`, `interrupted`, `error_recoverable`
- forwarding only final STT transcripts to the orchestrator over a typed API or
  event contract
- coordinating local TTS playback with a separate TTS service
- fake PCM fixture gateway tests that prove STT stream contract behavior without
  accessing host audio devices
- optional PipeWire CLI capture smoke path for local operator testing

Not owned:

- STT models, transcription exports, audio upload, STT WebSocket protocol:
  owned by `agents/audio_transcribe`
- final answer synthesis, policy, ledger and tool execution: owned by
  `orchestrator`
- durable audio/transcript storage policy: owned by `storage_guardian`
- TTS synthesis implementation: future separate service

## Architecture

Default Linux local-first stack:

1. `voice_gateway` runs as `systemd --user` on the host.
2. `voice_gateway` owns PipeWire/WirePlumber access and captures PCM frames.
3. `voice_gateway` sends PCM16 mono 16 kHz frames to
   `audio_transcribe` streaming. Inside Docker the internal contract is
   `wss://audio-streaming:8087/ws/stream`; from the host-side prototype harness
   use the debug-published `wss://127.0.0.1:9010/ws/stream`. The Python
   harness sends 30 ms PCM frames by default (`960` bytes at PCM16/16 kHz) to
   match the realtime VAD contract.
4. `audio_transcribe` emits unstable partials and final transcript events.
5. `voice_runtime` forwards only `final_transcript` to the orchestrator.
6. Orchestrator plans/responds through existing policy and dispatch boundaries.
7. A separate local TTS service synthesizes audio.
8. `voice_gateway` owns playback, cancellation and barge-in handling.

Implementation preference:

- CPAL/Rust for production host gateway.
- GStreamer if richer media pipeline/AEC routing is required.
- Python/PortAudio only for prototypes.

## Public Contracts To Add Later

The first local contracts are active in `voice_runtime.types`:

- `VoiceRuntimeConfig`
- `VoiceTurnState`
- `VoiceTurnEvent`
- `TranscriptForwardEvent`
- `VoiceGatewayStatus`

No service API is active yet. When an API implementation starts, add typed
contracts before code:

- `VoiceGatewayStatus`
- gateway fake PCM fixture test contract

Required metrics:

- `mic_frame_drop_count`
- `vad_activation_rate`
- `turn_end_latency_ms`
- `stt_partial_latency_ms`
- `stt_final_latency_ms`
- `tts_first_audio_ms`
- `barge_in_success_rate`

## Verification

Current planning-only verification:

```bash
env PYTHONPATH=features/voice_runtime PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  pytest -p pytest_asyncio.plugin features/voice_runtime/tests -q
```

```bash
ruff check features/voice_runtime
```

Future implementation verification must not require a real microphone in CI.
Use a fake gateway that sends PCM fixtures and validates final transcript events.
