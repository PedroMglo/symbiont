"""Speaker profiles: future-ready interface for speaker identification."""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class SpeakerProfile:
    """Metadata about a known speaker."""

    def __init__(
        self,
        speaker_id: str,
        display_name: Optional[str] = None,
        aliases: Optional[list[str]] = None,
    ) -> None:
        self.speaker_id = speaker_id
        self.display_name = display_name or speaker_id
        self.aliases = aliases or []


class SpeakerProfileStore:
    """Store for speaker profiles.

    Currently a no-op placeholder. Future versions may support:
    - Mapping SPEAKER_XX → user-provided names
    - Persisting speaker embeddings for cross-session recognition
    - Reusing aliases within the same job
    """

    def __init__(self, persist: bool = False) -> None:
        self._persist = persist
        self._profiles: dict[str, SpeakerProfile] = {}

    def register_name(self, speaker_id: str, display_name: str) -> None:
        """Register a display name for a speaker ID."""
        if speaker_id in self._profiles:
            self._profiles[speaker_id].display_name = display_name
        else:
            self._profiles[speaker_id] = SpeakerProfile(
                speaker_id=speaker_id, display_name=display_name
            )

    def get_display_name(self, speaker_id: str) -> str:
        """Get display name for a speaker, or return the ID itself."""
        profile = self._profiles.get(speaker_id)
        return profile.display_name if profile else speaker_id

    def resolve_speakers(self, speaker_ids: list[str]) -> dict[str, str]:
        """Resolve a list of speaker IDs to display names."""
        return {sid: self.get_display_name(sid) for sid in speaker_ids}

    def clear(self) -> None:
        """Clear all profiles."""
        self._profiles.clear()
