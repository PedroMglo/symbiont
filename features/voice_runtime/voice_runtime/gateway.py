"""Host gateway primitives for streaming PCM to audio_transcribe."""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from voice_runtime.turn_manager import VoiceTurnManager
from voice_runtime.types import TranscriptForwardEvent, VoiceRuntimeConfig, VoiceTurnEvent

DEFAULT_HOST_AUDIO_STREAM_URL = "wss://127.0.0.1:9010/ws/stream"
_DOCKER_AUDIO_STREAM_HOSTS = frozenset({"audio-streaming", "orc-audio-streaming"})


class WebSocketSession(Protocol):
    """Minimal async WebSocket session protocol used by the gateway."""

    async def send(self, data: str | bytes) -> None:
        ...

    async def recv(self) -> str | bytes:
        ...


VoiceEventHandler = Callable[[VoiceTurnEvent], Awaitable[None] | None]
FinalTranscriptHandler = Callable[[TranscriptForwardEvent], Awaitable[None] | None]


def iter_pcm_file(path: Path, *, chunk_bytes: int) -> Iterable[bytes]:
    """Yield fixed-size PCM chunks from a fixture or captured file."""
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_bytes)
            if not chunk:
                break
            yield chunk


def validate_host_audio_stream_url(url: str) -> None:
    """Reject Docker-only service names in the host-side gateway prototype."""
    parsed = urlsplit(url)
    if parsed.hostname in _DOCKER_AUDIO_STREAM_HOSTS:
        raise RuntimeError(
            "voice_runtime runs on the host; Docker service name "
            f"{parsed.hostname!r} is not a valid host endpoint. Use "
            f"{DEFAULT_HOST_AUDIO_STREAM_URL} from the host, or run the client "
            "inside the Compose network with an explicitly documented internal contract."
        )


async def run_streaming_turn(
    session: WebSocketSession,
    pcm_chunks: Iterable[bytes] | AsyncIterator[bytes],
    config: VoiceRuntimeConfig,
    *,
    on_event: VoiceEventHandler | None = None,
    on_final_transcript: FinalTranscriptHandler | None = None,
) -> list[TranscriptForwardEvent]:
    """Run one push-to-talk/fake PCM turn against an already connected session."""
    manager = VoiceTurnManager(config)
    forwarded: list[TranscriptForwardEvent] = []

    await _emit(on_event, manager.start_listening())
    await session.send(
        json.dumps(
            {
                "language": config.language,
                "sample_rate": config.sample_rate,
                "channels": config.channels,
                "sample_format": config.sample_format,
                "metadata": {"gateway_id": config.gateway_id, "mode": config.mode},
            }
        )
    )

    async for chunk in _aiter_chunks(pcm_chunks):
        if chunk:
            await session.send(chunk)
    await session.send("END")
    await _emit(on_event, manager.mark_transcribing())

    while True:
        try:
            raw = await asyncio.wait_for(session.recv(), timeout=config.receive_timeout_seconds)
        except (asyncio.TimeoutError, StopAsyncIteration):
            break

        if isinstance(raw, bytes):
            continue
        try:
            stt_event = json.loads(raw)
        except json.JSONDecodeError:
            await _emit(on_event, manager.recoverable_error("Invalid JSON event from audio stream"))
            continue

        event_type = str(stt_event.get("type") or "")
        if event_type == "partial_transcript":
            await _emit(on_event, manager.accept_partial(str(stt_event.get("text") or "")))
        elif event_type == "final_transcript":
            text = str(stt_event.get("text") or "").strip()
            if text:
                event, final = manager.accept_final(text, metadata={"stt_event": stt_event})
                forwarded.append(final)
                await _emit(on_event, event)
                await _emit_final(on_final_transcript, final)
        elif event_type == "speech_started":
            await _emit(on_event, manager.mark_user_speaking())
        elif event_type == "stream_warning":
            await _emit(
                on_event,
                VoiceTurnEvent(
                    type="stream_warning",
                    turn_id=manager.turn_id,
                    state=manager.state,
                    text=str(stt_event.get("message") or ""),
                    source_event_type=event_type,
                    metadata={"stt_event": stt_event},
                ),
            )
        elif event_type == "stream_error":
            await _emit(on_event, manager.recoverable_error(str(stt_event.get("message") or "stream error")))
        elif event_type == "session_closed":
            await _emit(on_event, manager.close())
            break

    return forwarded


async def stream_pcm_file_to_audio_transcribe(
    pcm_path: Path,
    config: VoiceRuntimeConfig,
    *,
    api_key: str,
    on_event: VoiceEventHandler | None = None,
    on_final_transcript: FinalTranscriptHandler | None = None,
) -> list[TranscriptForwardEvent]:
    """Connect to audio_transcribe streaming and send a PCM fixture/file."""
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("Install voice-runtime-feature[gateway] to use the WebSocket gateway") from exc

    validate_host_audio_stream_url(config.audio_stream_url)
    headers = {"X-API-Key": api_key}
    ssl_context = None
    if config.audio_stream_url.startswith("wss://") and not config.tls_verify:
        ssl_context = ssl._create_unverified_context()

    connect_kwargs: dict[str, Any] = {"ssl": ssl_context} if ssl_context is not None else {}
    chunks = iter_pcm_file(pcm_path, chunk_bytes=config.chunk_bytes)
    try:
        async with websockets.connect(
            config.audio_stream_url,
            additional_headers=headers,
            **connect_kwargs,
        ) as session:
            return await run_streaming_turn(
                session,
                chunks,
                config,
                on_event=on_event,
                on_final_transcript=on_final_transcript,
            )
    except (ConnectionRefusedError, TimeoutError, socket.gaierror, OSError) as exc:
        raise RuntimeError(_format_connection_error(config.audio_stream_url, exc)) from exc


async def _aiter_chunks(chunks: Iterable[bytes] | AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    if hasattr(chunks, "__aiter__"):
        async for chunk in chunks:  # type: ignore[union-attr]
            yield chunk
        return
    for chunk in chunks:  # type: ignore[union-attr]
        yield chunk


async def _emit(handler: VoiceEventHandler | None, event: VoiceTurnEvent) -> None:
    if handler is None:
        return
    result = handler(event)
    if result is not None:
        await result


async def _emit_final(handler: FinalTranscriptHandler | None, event: TranscriptForwardEvent) -> None:
    if handler is None:
        return
    result = handler(event)
    if result is not None:
        await result


def _format_connection_error(url: str, exc: BaseException) -> str:
    hint = (
        f"Use {DEFAULT_HOST_AUDIO_STREAM_URL} from the host and make sure the "
        "audio-streaming service is running with the debug port published "
        "(heavy profile plus debug compose override)."
    )
    return f"Could not connect to audio_transcribe streaming at {url!r}: {exc}. {hint}"
