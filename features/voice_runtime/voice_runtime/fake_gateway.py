"""CLI harness for sending PCM fixtures through the voice runtime boundary."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from voice_runtime.gateway import stream_pcm_file_to_audio_transcribe
from voice_runtime.host_audio import PipeWireRecorderConfig, record_pcm_with_pw_record
from voice_runtime.types import (
    HOST_AUDIO_STREAM_URL,
    PCM16_16KHZ_30MS_FRAME_BYTES,
    TranscriptForwardEvent,
    VoiceRuntimeConfig,
    VoiceTurnEvent,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send a PCM16 fixture to audio_transcribe streaming")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pcm", type=Path, help="Raw PCM16 little-endian mono fixture")
    source.add_argument("--record-seconds", type=float, help="Capture raw PCM from host PipeWire first")
    parser.add_argument("--api-key-file", type=Path, help="Audio transcribe API key file")
    parser.add_argument("--api-key", help="Audio transcribe API key")
    parser.add_argument("--url", default=HOST_AUDIO_STREAM_URL)
    parser.add_argument("--language", default="auto")
    parser.add_argument("--sample-rate", default=16000, type=int)
    parser.add_argument("--chunk-bytes", default=PCM16_16KHZ_30MS_FRAME_BYTES, type=int)
    parser.add_argument("--target", help="Optional PipeWire target node name or serial for --record-seconds")
    parser.add_argument("--tls-verify", dest="tls_verify", action="store_true", default=True)
    parser.add_argument("--tls-no-verify", dest="tls_verify", action="store_false")
    return parser


async def run(args: argparse.Namespace) -> list[TranscriptForwardEvent]:
    api_key = args.api_key or ""
    if not api_key and args.api_key_file:
        api_key = args.api_key_file.read_text(encoding="utf-8").strip()
    if not api_key:
        raise SystemExit("Provide --api-key or --api-key-file")

    config = VoiceRuntimeConfig(
        audio_stream_url=args.url,
        language=args.language,
        sample_rate=args.sample_rate,
        chunk_bytes=args.chunk_bytes,
        tls_verify=args.tls_verify,
    )

    pcm_path = args.pcm
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if pcm_path is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="voice-runtime-")
        pcm_path = Path(temp_dir.name) / "capture.pcm"
        record_pcm_with_pw_record(
            pcm_path,
            PipeWireRecorderConfig(
                sample_rate=args.sample_rate,
                duration_seconds=args.record_seconds,
                target=args.target,
            ),
        )

    def print_event(event: VoiceTurnEvent) -> None:
        print(json.dumps(event.model_dump(mode="json", exclude_none=True), ensure_ascii=False))

    def print_final(event: TranscriptForwardEvent) -> None:
        print(json.dumps(event.model_dump(mode="json"), ensure_ascii=False))

    try:
        return await stream_pcm_file_to_audio_transcribe(
            pcm_path,
            config,
            api_key=api_key,
            on_event=print_event,
            on_final_transcript=print_final,
        )
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def main() -> None:
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
