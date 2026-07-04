"""File classification helpers."""

from __future__ import annotations

from pathlib import Path

from storage_guardian.types import PolicyConfig

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".json",
    ".jsonl",
    ".csv",
    ".html",
    ".xml",
    ".log",
    ".eml",
    ".sql",
    ".py",
    ".js",
    ".ts",
    ".yaml",
    ".yml",
    ".toml",
    ".sh",
}
DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".pbix", ".pbit"}
AUDIO_EXTENSIONS = {".wav", ".aiff", ".aif", ".mp3", ".m4a", ".opus", ".flac"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}
DB_EXTENSIONS = {".db", ".sqlite", ".sqlite3", ".duckdb"}
MODEL_EXTENSIONS = {".gguf", ".safetensors", ".onnx", ".pt", ".bin"}
SNAPSHOT_SUFFIXES = (".snapshot", ".snapshot.zip", ".snapshot.tar", ".snapshot.tar.zst", ".dump", ".backup")


def classify_path(path: Path, policy: PolicyConfig) -> tuple[str, str]:
    suffix = path.suffix.lower()
    name = path.name.lower()
    if name.endswith(SNAPSHOT_SUFFIXES):
        return "snapshot", "snapshot"
    if suffix in DB_EXTENSIONS:
        return "database", "live_database"
    if suffix in AUDIO_EXTENSIONS:
        return "audio", "file"
    if suffix in IMAGE_EXTENSIONS:
        return "image", "file"
    if suffix in DOCUMENT_EXTENSIONS:
        if suffix in set(policy.get("opaque_extensions", [])):
            return "opaque_document", "file"
        return "document", "file"
    if suffix in MODEL_EXTENSIONS:
        return "model", "model"
    if suffix in TEXT_EXTENSIONS:
        return "text", "file"
    if suffix in set(policy.get("extensions", [])):
        return str(policy.get("detected_type", "policy_match")), "file"
    return "binary", "file"


def backend_for(policy: PolicyConfig, tier: str, detected_type: str, extension: str) -> str:
    extension = extension.lower()
    if detected_type == "audio":
        if extension in {".mp3", ".m4a", ".opus", ".flac"}:
            return "passthrough"
        if extension in {".wav", ".aiff", ".aif"} and policy.get("wav_transform") == "flac_lossless":
            return "zstd"
    if detected_type == "model":
        return "passthrough"
    if tier == "cold":
        return str(policy.get("cold_backend", policy.get("warm_backend", "zstd")))
    return str(policy.get("warm_backend", "zstd"))

