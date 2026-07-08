"""Whisper transcription using faster-whisper/CTranslate2."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from audio_transcribe.config import get_config
from audio_transcribe.errors import ModelLoadError, TranscriptionError
from audio_transcribe.gpu import (
    clear_gpu_cache,
    get_gpu_info,
    select_compute_type,
    select_device,
    wait_for_gpu_memory,
)
from audio_transcribe.types import TranscriptSegment, TranscriptionResult, WordTimestamp

logger = logging.getLogger(__name__)


def _model_vram_floor_mb(model_name: str) -> int:
    """Conservative free-VRAM floor for faster-whisper model loading."""
    name = (model_name or "").lower()
    if "large" in name and "distil" not in name:
        return 5200
    if "distil-large" in name:
        return 3000
    if "medium" in name:
        return 2200
    if "small" in name:
        return 768
    if "base" in name:
        return 700
    if "tiny" in name:
        return 500
    return 1400


def _is_cuda_load_error(exc: Exception) -> bool:
    err = str(exc).lower()
    return any(marker in err for marker in ("out of memory", "cuda", "cudnn", "cublas", "failed to allocate"))


class WhisperTranscriber:
    """Transcription engine using faster-whisper.

    Design:
    - Singleton model instance reused across jobs
    - Model loaded lazily on first transcription
    - Model only reloaded if config changes (model name/device)
    - Single GPU transcription at a time (controlled by queue semaphore)
    """

    def __init__(self) -> None:
        self._model = None
        self._model_name: str = ""
        self._device: str = ""
        self._compute_type: str = ""
        self._loaded = False

    @property
    def model_loaded(self) -> bool:
        return self._loaded

    @property
    def current_model(self) -> str:
        return self._model_name

    @property
    def current_device(self) -> str:
        return self._device

    @property
    def current_compute_type(self) -> str:
        return self._compute_type

    def _candidate_gpu_models(self, requested_model: str) -> list[str]:
        cfg = get_config()
        candidates = [requested_model]
        if cfg.gpu_policy.allow_model_downgrade:
            candidates.extend([cfg.gpu_policy.degraded_model, "small", "base", "tiny"])
        result: list[str] = []
        for candidate in candidates:
            if candidate and candidate not in result:
                result.append(candidate)
        return result

    def _select_gpu_model(self, requested_model: str) -> str | None:
        cfg = get_config()
        candidates = self._candidate_gpu_models(requested_model)

        def _first_fit(free_mb: int) -> str | None:
            if free_mb <= 0:
                return requested_model
            for candidate in candidates:
                required = max(_model_vram_floor_mb(candidate), cfg.gpu_policy.min_free_vram_mb)
                if free_mb >= required:
                    return candidate
            return None

        info = get_gpu_info(refresh=True)
        selected = _first_fit(info.vram_free_mb)
        if selected:
            if selected != requested_model:
                logger.warning(
                    "GPU VRAM free=%sMB is below %sMB for %s; using %s on GPU",
                    info.vram_free_mb,
                    max(_model_vram_floor_mb(requested_model), cfg.gpu_policy.min_free_vram_mb),
                    requested_model,
                    selected,
                )
            return selected

        minimum_viable = min(
            max(_model_vram_floor_mb(candidate), cfg.gpu_policy.min_free_vram_mb)
            for candidate in candidates
        )
        info = wait_for_gpu_memory(
            minimum_viable,
            timeout_seconds=cfg.gpu_policy.wait_timeout_seconds,
            poll_seconds=cfg.gpu_policy.wait_poll_seconds,
        )
        selected = _first_fit(info.vram_free_mb)
        if selected and selected != requested_model:
            logger.warning(
                "GPU VRAM free=%sMB after wait; using %s instead of %s",
                info.vram_free_mb,
                selected,
                requested_model,
            )
        return selected

    def _resolve_load_plan(
        self,
        requested_model: str,
        requested_device: str,
        requested_compute: str,
    ) -> tuple[str, str, str]:
        cfg = get_config()
        if requested_device != "cuda" and not cfg.gpu_policy.prefer_gpu:
            return requested_model, "cpu", select_compute_type(requested_compute, "cpu")

        actual_device = select_device(requested_device)
        if actual_device != "cuda":
            return requested_model, "cpu", select_compute_type(requested_compute, "cpu")

        selected_model = self._select_gpu_model(requested_model)
        if selected_model is None:
            info = get_gpu_info(refresh=True)
            logger.warning(
                "GPU available but not healthy for audio: free=%sMB, minimum=%sMB",
                info.vram_free_mb,
                cfg.gpu_policy.min_free_vram_mb,
            )
            if cfg.gpu_policy.allow_cpu_degradation:
                return requested_model, "cpu", select_compute_type(requested_compute, "cpu")

        return selected_model or requested_model, "cuda", select_compute_type(requested_compute, "cuda")

    def _load_whisper_model(self, model_name: str, device: str, compute_type: str) -> None:
        cfg = get_config()
        from faster_whisper import WhisperModel

        self._model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            download_root=cfg.transcription.download_root,
        )
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._loaded = True

    def load_model(
        self,
        model_name: Optional[str] = None,
        device: Optional[str] = None,
        compute_type: Optional[str] = None,
    ) -> None:
        """Load or reload the Whisper model."""
        cfg = get_config()
        model_name = model_name or cfg.transcription.model
        requested_device = device or cfg.transcription.device
        requested_compute = compute_type or cfg.transcription.compute_type

        actual_model, actual_device, actual_compute = self._resolve_load_plan(
            model_name,
            requested_device,
            requested_compute,
        )

        # Skip reload if same config
        if (
            self._loaded
            and self._model_name == actual_model
            and self._device == actual_device
            and self._compute_type == actual_compute
        ):
            return

        logger.info(
            f"Loading model: {actual_model} | device={actual_device} | "
            f"compute_type={actual_compute}"
        )

        try:
            self._load_whisper_model(actual_model, actual_device, actual_compute)
            logger.info(f"Model loaded: {actual_model} on {actual_device}")
        except (RuntimeError, Exception) as e:
            if actual_device == "cuda" and _is_cuda_load_error(e):
                clear_gpu_cache()
                if cfg.gpu_policy.allow_model_downgrade:
                    for alternate_model in self._candidate_gpu_models(model_name):
                        if alternate_model == actual_model:
                            continue
                        alternate_required = max(
                            _model_vram_floor_mb(alternate_model),
                            cfg.gpu_policy.min_free_vram_mb,
                        )
                        gpu_info = wait_for_gpu_memory(
                            alternate_required,
                            timeout_seconds=cfg.gpu_policy.wait_timeout_seconds,
                            poll_seconds=cfg.gpu_policy.wait_poll_seconds,
                        )
                        if gpu_info.vram_free_mb and gpu_info.vram_free_mb < alternate_required:
                            continue
                        try:
                            alternate_compute = select_compute_type(requested_compute, "cuda")
                            logger.warning(
                                "CUDA failed loading %s (%s); retrying %s on GPU",
                                actual_model,
                                e,
                                alternate_model,
                            )
                            self._load_whisper_model(alternate_model, "cuda", alternate_compute)
                            logger.info("Model loaded (GPU alternate): %s", alternate_model)
                            return
                        except Exception as alternate_gpu_err:
                            clear_gpu_cache()
                            logger.warning("GPU alternate model %s failed: %s", alternate_model, alternate_gpu_err)

                if not cfg.gpu_policy.allow_cpu_degradation:
                    self._loaded = False
                    raise ModelLoadError(
                        message=f"Failed to load model '{actual_model}' on GPU",
                        detail=str(e),
                    )

                logger.warning("CUDA failed (%s), degrading to CPU", e)
                cpu_compute = select_compute_type(requested_compute, "cpu")
                try:
                    cpu_model = actual_model if actual_model != model_name else cfg.gpu_policy.degraded_model
                    self._load_whisper_model(cpu_model, "cpu", cpu_compute)
                    logger.info(
                        f"Model loaded (CPU degradation): {self._model_name} | "
                        f"compute_type={cpu_compute}"
                    )
                    return
                except Exception as degradation_err:
                    self._loaded = False
                    raise ModelLoadError(
                        message=f"Failed to load model '{actual_model}' (GPU and CPU)",
                        detail=str(degradation_err),
                    )
            self._loaded = False
            raise ModelLoadError(
                message=f"Failed to load model '{actual_model}'",
                detail=str(e),
            )

    def transcribe_segment(
        self,
        audio_path: Path,
        language: str = "auto",
        segment_offset: float = 0.0,
    ) -> list[TranscriptSegment]:
        """Transcribe a single audio segment.

        Args:
            audio_path: Path to WAV audio segment
            language: Language code or 'auto' for detection
            segment_offset: Absolute timestamp offset for this segment

        Returns:
            List of TranscriptSegment with absolute timestamps
        """
        if not self._loaded or self._model is None:
            self.load_model()

        cfg = get_config()
        lang = None if language == "auto" else language

        try:
            segments_iter, info = self._model.transcribe(
                str(audio_path),
                language=lang,
                beam_size=cfg.transcription.beam_size,
                word_timestamps=cfg.transcription.word_timestamps,
                vad_filter=cfg.transcription.vad_filter,
            )

            result_segments: list[TranscriptSegment] = []
            detected_language = info.language if info else ""

            for idx, seg in enumerate(segments_iter):
                # Build word timestamps if available
                words: list[WordTimestamp] = []
                if seg.words:
                    for w in seg.words:
                        words.append(WordTimestamp(
                            word=w.word,
                            start=w.start + segment_offset,
                            end=w.end + segment_offset,
                            confidence=w.probability if hasattr(w, "probability") else None,
                        ))

                result_segments.append(TranscriptSegment(
                    index=idx,
                    start=seg.start + segment_offset,
                    end=seg.end + segment_offset,
                    text=seg.text.strip(),
                    confidence=seg.avg_logprob if hasattr(seg, "avg_logprob") else None,
                    language=detected_language,
                    no_speech_prob=seg.no_speech_prob if hasattr(seg, "no_speech_prob") else None,
                    words=words,
                ))

            return result_segments

        except Exception as e:
            raise TranscriptionError(
                message=f"Transcription failed for {audio_path.name}",
                detail=str(e),
            )

    def transcribe_full(
        self,
        audio_path: Path,
        language: str = "auto",
    ) -> TranscriptionResult:
        """Transcribe a full audio file (for short files without segmentation)."""
        start_time = time.time()
        segments = self.transcribe_segment(audio_path, language=language)
        elapsed = time.time() - start_time

        duration = segments[-1].end if segments else 0.0
        logger.info(
            f"Transcribed {duration:.1f}s audio in {elapsed:.1f}s "
            f"(RTF: {elapsed / max(duration, 0.1):.2f})"
        )

        detected_lang = segments[0].language if segments else ""
        return TranscriptionResult(
            segments=segments,
            language=detected_lang,
            duration_seconds=duration,
        )

    def unload_model(self) -> None:
        """Unload model to free memory."""
        self._model = None
        self._loaded = False
        self._model_name = ""
        self._device = ""
        self._compute_type = ""
        logger.info("Model unloaded")


# Module singleton
_transcriber: Optional[WhisperTranscriber] = None


def get_transcriber() -> WhisperTranscriber:
    """Get or create the global transcriber instance."""
    global _transcriber
    if _transcriber is None:
        _transcriber = WhisperTranscriber()
    return _transcriber


def reset_transcriber() -> None:
    """Reset transcriber singleton (for testing)."""
    global _transcriber
    if _transcriber:
        _transcriber.unload_model()
    _transcriber = None
