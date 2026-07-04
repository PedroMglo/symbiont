"""Job creation helpers for extrator."""

from __future__ import annotations

from extrator.manifest import get_manifest
from extrator.pipeline import serialize_request
from extrator.types import ConversionPathRequest, ExtractionPathRequest, JobKind


def create_extraction_job(request: ExtractionPathRequest) -> str:
    return get_manifest().create_job(JobKind.EXTRACTION, serialize_request(request))


def create_conversion_job(request: ConversionPathRequest) -> str:
    return get_manifest().create_job(JobKind.CONVERSION, serialize_request(request))
