"""Transcription pipeline: orchestrates all processing stages."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from audio_transcribe.config import get_config
from audio_transcribe.errors import JobCancelledError
from audio_transcribe.jobs import get_job, update_job
from audio_transcribe.observability import JobObserver
from audio_transcribe.storage import (
    copy_input_to_job,
    get_job_dir,
    load_checkpoints,
    save_checkpoint,
    save_job_record,
)
from audio_transcribe.types import (
    AudioSegment,
    JobStage,
    JobStatus,
    SegmentCheckpoint,
    SegmentStatus,
    SemanticSummary,
    TranscriptSegment,
    TranscriptionResult,
)

logger = logging.getLogger(__name__)


async def process_job(job_id: str) -> None:
    """Main pipeline entry point. Called by the job queue worker."""
    record = get_job(job_id)

    # Check if already cancelled
    if record.status == JobStatus.CANCELLED:
        return

    observer = JobObserver(job_id)
    observer.start_job()

    try:
        update_job(job_id, status=JobStatus.RUNNING, stage=JobStage.VALIDATING)

        # =====================================================================
        # Stage: Validation
        # =====================================================================
        observer.start_stage("validating")
        input_path = _validate_and_copy_input(job_id, record.input_path)
        observer.end_stage()

        _check_cancelled(job_id)

        # =====================================================================
        # Stage: Preprocessing
        # =====================================================================
        observer.start_stage("preprocessing")
        update_job(job_id, stage=JobStage.PREPROCESSING)

        from audio_transcribe.preprocessing import AudioPreprocessor

        preprocessor = AudioPreprocessor()
        processed_dir = get_job_dir(job_id) / "processed_audio"
        processed_dir.mkdir(parents=True, exist_ok=True)
        result = await preprocessor.process(input_path, processed_dir)
        audio_path = result.audio_path
        metadata = result.metadata

        observer.set_audio_duration(metadata.duration_seconds)
        update_job(job_id, total_duration=metadata.duration_seconds)
        observer.end_stage()

        _check_cancelled(job_id)

        # =====================================================================
        # Stage: Audio Quality
        # =====================================================================
        cfg = get_config()
        quality_report = None
        if cfg.audio_quality.enabled:
            observer.start_stage("audio_quality")
            update_job(job_id, stage=JobStage.AUDIO_QUALITY)

            from audio_transcribe.audio_quality import AudioQualityAnalyzer

            analyzer = AudioQualityAnalyzer()
            quality_report = await asyncio.to_thread(
                analyzer.analyze, audio_path, metadata.duration_seconds
            )

            for warning in quality_report.warnings:
                update_job(job_id, warning=warning)

            observer.end_stage()

        _check_cancelled(job_id)

        # =====================================================================
        # Stage: Noise Reduction
        # =====================================================================
        if cfg.preprocessing.noise_reduction:
            observer.start_stage("noise_reduction")
            update_job(job_id, stage=JobStage.NOISE_REDUCTION)

            from audio_transcribe.noise_reduction import create_noise_reducer

            reducer = create_noise_reducer(enabled=True)
            nr_output = processed_dir / "audio_nr.wav"
            audio_path = await asyncio.to_thread(reducer.process, audio_path, nr_output)

            observer.end_stage()

        _check_cancelled(job_id)

        # =====================================================================
        # Stage: Segmentation
        # =====================================================================
        observer.start_stage("segmentation")
        update_job(job_id, stage=JobStage.SEGMENTATION)

        from audio_transcribe.segmentation import LongAudioSegmenter
        from audio_transcribe.vad import VADProcessor

        segments_dir = get_job_dir(job_id) / "segments"
        segments_dir.mkdir(parents=True, exist_ok=True)

        vad_processor = None
        if cfg.vad.enabled:
            vad_processor = VADProcessor(
                min_speech_duration_ms=cfg.vad.min_speech_duration_ms,
                min_silence_duration_ms=cfg.vad.min_silence_duration_ms,
                speech_pad_ms=cfg.vad.speech_pad_ms,
                sample_rate=cfg.preprocessing.sample_rate,
            )

        segmenter = LongAudioSegmenter(
            max_segment_duration=cfg.performance.max_segment_duration_seconds,
            overlap_seconds=cfg.performance.segment_overlap_seconds,
            strategy=cfg.performance.segment_strategy,
            vad_processor=vad_processor,
        )

        audio_segments = await asyncio.to_thread(
            segmenter.segment, audio_path, metadata.duration_seconds, segments_dir
        )
        observer.set_segments_count(len(audio_segments))
        observer.end_stage()

        _check_cancelled(job_id)

        # =====================================================================
        # Stage: Transcription
        # =====================================================================
        observer.start_stage("transcription")
        update_job(job_id, stage=JobStage.TRANSCRIPTION)

        from audio_transcribe.transcription import get_transcriber
        from audio_transcribe.queue import get_queue

        transcriber = get_transcriber()
        options = record.options
        queue = get_queue()
        all_transcript_segments: list[TranscriptSegment] = []
        load_device = options.device
        gpu_lease = None

        if options.device in {"auto", "cuda"}:
            try:
                from audio_transcribe.resource_governor import request_audio_gpu_lease

                gpu_lease = await request_audio_gpu_lease(
                    job_id,
                    model_name=options.model,
                    duration_seconds=metadata.duration_seconds,
                )
                if not gpu_lease.granted:
                    reason = gpu_lease.decision.reason
                    if cfg.gpu_policy.cpu_fallback_enabled:
                        load_device = "cpu"
                        update_job(job_id, warning=f"GPU deferred by Resource Governor; using CPU fallback: {reason}")
                    else:
                        retry = min(gpu_lease.decision.retry_after_seconds or 10, 30)
                        update_job(job_id, warning=f"GPU deferred by Resource Governor; waiting {retry}s: {reason}")
                        await asyncio.sleep(retry)
                        await gpu_lease.release()
                        gpu_lease = await request_audio_gpu_lease(
                            job_id,
                            model_name=options.model,
                            duration_seconds=metadata.duration_seconds,
                        )
                        if not gpu_lease.granted:
                            raise RuntimeError(f"audio GPU lease denied: {gpu_lease.decision.reason}")
            except Exception as exc:
                logger.debug("Resource Governor audio lease skipped: %s", exc)

        # Serialize model load and transcription so two jobs cannot race for VRAM.
        try:
            async with queue.gpu_semaphore:
                await asyncio.to_thread(
                    transcriber.load_model,
                    model_name=options.model,
                    device=load_device,
                    compute_type=options.compute_type,
                )

                observer.set_model_info(
                    model=transcriber.current_model,
                    device=transcriber.current_device,
                    compute_type=transcriber.current_compute_type,
                    batch_size=cfg.performance.batch_size,
                )

                all_transcript_segments = await _transcribe_segments(
                    job_id, transcriber, audio_segments, audio_path,
                    options.language, cfg, observer,
                )
        finally:
            if gpu_lease is not None:
                await gpu_lease.release()

        # Build TranscriptionResult
        raw_result = TranscriptionResult(
            segments=all_transcript_segments,
            language=all_transcript_segments[0].language if all_transcript_segments else "",
            duration_seconds=metadata.duration_seconds,
        )

        observer.end_stage()
        _check_cancelled(job_id)

        # =====================================================================
        # Stage: Diarization (optional)
        # =====================================================================
        speaker_segments = []
        if options.diarization and cfg.diarization.enabled:
            observer.start_stage("diarization")
            update_job(job_id, stage=JobStage.DIARIZATION)

            from audio_transcribe.diarization import get_diarizer

            diarizer = get_diarizer()
            speaker_segments = await asyncio.to_thread(
                diarizer.diarize,
                audio_path,
                cfg.diarization.min_speakers,
                cfg.diarization.max_speakers,
            )

            observer.end_stage()

        _check_cancelled(job_id)

        # =====================================================================
        # Stage: Speaker Alignment
        # =====================================================================
        if speaker_segments:
            observer.start_stage("speaker_alignment")
            update_job(job_id, stage=JobStage.SPEAKER_ALIGNMENT)

            from audio_transcribe.speaker_alignment import SpeakerAligner

            aligner = SpeakerAligner()
            all_transcript_segments = await asyncio.to_thread(
                aligner.align, all_transcript_segments, speaker_segments
            )
            speakers = aligner.get_speaker_list(all_transcript_segments)
            observer.set_speakers_count(len(speakers))

            # Update raw result with speaker info
            raw_result.segments = all_transcript_segments

            observer.end_stage()

        _check_cancelled(job_id)

        # =====================================================================
        # Stage: Postprocessing
        # =====================================================================
        observer.start_stage("postprocessing")
        update_job(job_id, stage=JobStage.POSTPROCESSING)

        from audio_transcribe.postprocessing import TranscriptPostProcessor

        postprocessor = TranscriptPostProcessor()
        clean_transcript = await asyncio.to_thread(
            postprocessor.process, all_transcript_segments
        )

        observer.end_stage()
        _check_cancelled(job_id)

        # =====================================================================
        # Stage: Semantic Extraction
        # =====================================================================
        semantic = SemanticSummary()
        if cfg.semantic_extraction.enabled:
            observer.start_stage("semantic_extraction")
            update_job(job_id, stage=JobStage.SEMANTIC_EXTRACTION)

            from audio_transcribe.semantic_extraction import SemanticExtractor

            extractor = SemanticExtractor()
            semantic = await asyncio.to_thread(extractor.extract, clean_transcript)

            observer.end_stage()

        _check_cancelled(job_id)

        # =====================================================================
        # Stage: Export
        # =====================================================================
        observer.start_stage("exporting")
        update_job(job_id, stage=JobStage.EXPORTING)

        from audio_transcribe.exporters import export_all, publish_job_record

        observer.end_job()
        metrics = observer.metrics

        artifacts = await asyncio.to_thread(
            export_all,
            job_id=job_id,
            raw_result=raw_result,
            clean_transcript=clean_transcript,
            semantic=semantic,
            record=record,
            metrics=metrics,
            quality_report=quality_report,
            formats=options.formats,
            rag_ready=options.rag_ready,
            include_speakers_in_subtitles=cfg.export.include_speakers_in_subtitles,
        )

        # =====================================================================
        # Complete
        # =====================================================================
        record = get_job(job_id)
        record.artifacts = artifacts
        record.metrics = metrics
        record.status = JobStatus.COMPLETED
        record.stage = JobStage.COMPLETED
        record.progress = 100.0
        record.processed_duration_seconds = metadata.duration_seconds
        save_job_record(record)

        update_job(job_id, status=JobStatus.COMPLETED, stage=JobStage.COMPLETED, progress=100.0)
        record = get_job(job_id)
        published_job_json = await asyncio.to_thread(publish_job_record, job_id, record)
        if published_job_json:
            record.artifacts.job_json = published_job_json
            save_job_record(record)
        logger.info(
            f"Job {job_id} completed | "
            f"duration={metadata.duration_seconds:.1f}s | "
            f"RTF={metrics.realtime_factor:.2f}"
        )

    except JobCancelledError:
        update_job(job_id, status=JobStatus.CANCELLED, stage=JobStage.CANCELLED)
        logger.info(f"Job {job_id} cancelled")

    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}", exc_info=True)
        observer.end_job()
        update_job(
            job_id,
            status=JobStatus.FAILED,
            stage=JobStage.FAILED,
            error=str(e)[:500],
        )

    finally:
        await asyncio.to_thread(_restore_gpu_health_after_job)


def _restore_gpu_health_after_job() -> None:
    """Unload the ASR model when it leaves the GPU below the configured floor."""
    from audio_transcribe.config import get_config
    from audio_transcribe.gpu import clear_gpu_cache, get_gpu_info
    from audio_transcribe.transcription import get_transcriber

    cfg = get_config()
    info = get_gpu_info(refresh=True)
    free_before = info.vram_free_mb or 0
    if not info.available or free_before >= cfg.gpu_policy.min_free_vram_mb:
        return

    transcriber = get_transcriber()
    transcriber.unload_model()
    clear_gpu_cache()

    info_after = get_gpu_info(refresh=True)
    logger.info(
        "GPU health restored after job: free_vram=%sMB -> %sMB (min=%sMB)",
        free_before,
        info_after.vram_free_mb,
        cfg.gpu_policy.min_free_vram_mb,
    )


def _validate_and_copy_input(job_id: str, input_path: Optional[str]) -> Path:
    """Validate input and copy to job directory."""
    from audio_transcribe.errors import InvalidInputError

    if not input_path:
        raise InvalidInputError(message="No input path specified")

    source = Path(input_path)
    if not source.exists():
        raise InvalidInputError(message=f"Input file not found: {source.name}")

    # Copy to job input dir
    dest = copy_input_to_job(source, job_id)
    return dest


def _check_cancelled(job_id: str) -> None:
    """Check if job was cancelled and raise if so."""
    record = get_job(job_id)
    if record.status == JobStatus.CANCELLED:
        raise JobCancelledError(message=f"Job {job_id} was cancelled")


async def _transcribe_segments(
    job_id: str,
    transcriber,
    audio_segments: list[AudioSegment],
    audio_path: Path,
    language: str,
    cfg,
    observer: JobObserver,
) -> list[TranscriptSegment]:
    """Transcribe all segments with checkpoint support."""
    from audio_transcribe.preprocessing import AudioPreprocessor

    all_segments: list[TranscriptSegment] = []
    total_duration = sum(s.duration for s in audio_segments)
    processed_so_far = 0.0

    # Check for existing checkpoints (resume support)
    existing_checkpoints = load_checkpoints(job_id)
    completed_indices = {cp.index for cp in existing_checkpoints if cp.status == SegmentStatus.COMPLETED}

    max_retries = 2
    preprocessor = AudioPreprocessor()

    for seg in audio_segments:
        # Skip already completed segments (resume)
        if seg.index in completed_indices:
            # Load from checkpoint
            cp = next(c for c in existing_checkpoints if c.index == seg.index)
            if cp.transcript_text:
                # Reconstruct minimal segment
                all_segments.append(TranscriptSegment(
                    index=seg.index,
                    start=seg.start,
                    end=seg.end,
                    text=cp.transcript_text,
                ))
            processed_so_far += seg.duration
            continue

        # Determine audio source for this segment
        if len(audio_segments) == 1:
            seg_audio_path = audio_path
        else:
            # Extract segment audio
            seg_dir = get_job_dir(job_id) / "segments"
            seg_audio_path = seg_dir / f"segment_{seg.index:04d}.wav"
            await preprocessor.extract_segment(audio_path, seg_audio_path, seg.start, seg.duration)

        # Transcribe with retry
        checkpoint = SegmentCheckpoint(
            segment_id=seg.segment_id,
            index=seg.index,
            start=seg.start,
            end=seg.end,
        )

        success = False
        for attempt in range(max_retries + 1):
            try:
                checkpoint.status = SegmentStatus.RUNNING
                checkpoint.attempts = attempt + 1

                seg_result = await asyncio.to_thread(
                    transcriber.transcribe_segment,
                    seg_audio_path,
                    language,
                    seg.start if len(audio_segments) > 1 else 0.0,
                )

                all_segments.extend(seg_result)
                checkpoint.status = SegmentStatus.COMPLETED
                checkpoint.transcript_text = " ".join(s.text for s in seg_result)
                success = True
                break

            except Exception as e:
                logger.warning(
                    f"Segment {seg.index} attempt {attempt + 1} failed: {e}"
                )
                checkpoint.error = str(e)[:200]
                if attempt < max_retries:
                    observer.increment_retries()

        if not success:
            checkpoint.status = SegmentStatus.FAILED
            logger.error(f"Segment {seg.index} failed after {max_retries + 1} attempts")

        # Save checkpoint
        if cfg.performance.enable_segment_checkpoints:
            save_checkpoint(job_id, checkpoint)

        # Update progress
        processed_so_far += seg.duration
        if total_duration > 0:
            progress = (processed_so_far / total_duration) * 90  # Leave 10% for post-processing
            update_job(job_id, progress=progress, processed_duration=processed_so_far)

    return all_segments
