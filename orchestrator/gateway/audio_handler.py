"""Thin audio gateway proxy.

The audio_transcribe agent owns path extraction, validation, job creation,
polling, streaming details and output metadata. The gateway only detects the
interactive shortcut and delegates through dispatch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def _audio_route_hints() -> dict[str, tuple[str, ...]]:
    """Load owner-published audio route hints from service capabilities."""
    from orchestrator.capabilities.catalog import get_service_capability_manifest

    manifest = get_service_capability_manifest("audio_transcribe")
    if manifest is None:
        return {}
    return manifest.route_hints


@lru_cache(maxsize=1)
def _audio_dispatch_contract() -> tuple[str, str]:
    """Return canonical dispatch path and policy action from the owner manifest."""
    from orchestrator.capabilities.catalog import get_service_capability_manifest

    manifest = get_service_capability_manifest("audio_transcribe")
    if manifest is None:
        return "/v1/transcribe", "audio.transcribe"
    path = str(manifest.transport.get("path") or "/v1/transcribe")
    return path, manifest.policy_action


def is_audio_query(query: str) -> bool:
    """Return True when a request is likely asking for transcription."""

    q_lower = query.lower()
    hints = _audio_route_hints()
    action_signals = set(hints.get("action_signals", ()))
    patterns = hints.get("patterns", ())
    extensions = hints.get("file_extensions", ())

    words = {word.strip(".,!?:;\"'()[]{}") for word in q_lower.split()}
    if words & action_signals:
        return True
    if any(pattern in q_lower for pattern in patterns):
        return True
    return any(f".{ext}" in q_lower for ext in extensions)


async def stream_audio_transcription(
    query: str,
    *,
    feature_client: Any | None = None,
    wait_seconds: float = 1800.0,
) -> AsyncIterator[str]:
    """Delegate an audio transcription request to the audio_transcribe agent."""

    if feature_client is None:
        yield (
            "Servico `audio_transcribe` indisponivel no dispatch atual. "
            "Nao foi executada nenhuma transcricao.\n"
        )
        return

    path, policy_action = _audio_dispatch_contract()
    response = feature_client.invoke_endpoint(
        "audio_transcribe",
        method="POST",
        path=path,
        payload={
            "query": query,
            "wait_seconds": wait_seconds,
            "poll_interval_seconds": 3.0,
            "metadata": {"caller": "orchestrator.gateway.audio_handler"},
        },
        timeout=max(wait_seconds + 15.0, 30.0),
        policy_action=policy_action,
    )
    if not response.success:
        yield (
            "Servico `audio_transcribe` indisponivel ou bloqueado pelo dispatch: "
            f"{response.error or 'erro desconhecido'}\n"
        )
        return

    data = response.data or {}
    content = data.get("content")
    if isinstance(content, str) and content.strip():
        yield content.rstrip() + "\n"
        return

    yield "Pedido de transcricao aceite pelo servico `audio_transcribe`.\n"
