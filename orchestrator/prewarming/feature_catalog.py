"""Feature catalog — loads service definitions from TOML for prediction routing."""

from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_PREWARM_DISABLED_POLICIES = {"never"}


@dataclass(frozen=True)
class FeatureDefinition:
    """Metadata for a single predictable service/feature."""

    feature_id: str
    display_name: str = ""
    description: str = ""
    container_name: str = ""
    capabilities: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    file_extensions: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    example_queries: tuple[str, ...] = ()
    negative_keywords: tuple[str, ...] = ()
    negative_patterns: tuple[str, ...] = ()
    startup_cost: str = "low"  # "low" | "medium" | "high"
    uses_gpu: bool = False
    prewarm_policy: str = "standard"  # "standard" | "aggressive" | "conservative" | "never"
    prewarm_threshold: float = 0.75  # Per-feature confidence threshold for prewarm
    ttl_idle: int = 300
    priority: int = 5  # 1=highest, 10=lowest

    def intent_documents(self) -> tuple[str, ...]:
        """Return generic service-intent documents for semantic routing.

        These documents describe durable service capabilities, inputs, operations,
        and identifiers. They deliberately exclude example prompts so routing
        does not overfit to a small set of phrased requests.
        """
        docs: list[str] = []
        if self.description:
            docs.append(self.description)
        for label, values in (
            ("capabilities", self.capabilities),
            ("inputs", self.inputs),
            ("operations", self.operations),
            ("keywords", self.keywords),
            ("file types", self.file_extensions),
        ):
            if values:
                docs.append(f"{label}: {' '.join(values)}")
        identifiers = " ".join(
            part for part in (self.feature_id, self.display_name, self.container_name)
            if part
        )
        if identifiers:
            docs.append(f"service identifiers: {identifiers}")
        return tuple(docs)


class FeatureCatalog:
    """Loads and manages feature definitions from a TOML catalog file.

    Supports hot-reload via mtime check (same pattern as ModelRegistry).
    """

    def __init__(self, catalog_path: Path) -> None:
        self._path = catalog_path
        self._features: dict[str, FeatureDefinition] = {}
        self._keyword_index: dict[str, list[str]] = {}  # keyword → [feature_ids]
        self._extension_index: dict[str, list[str]] = {}  # ext → [feature_ids]
        self._compiled_patterns: dict[str, list[re.Pattern[str]]] = {}  # feature_id → [patterns]
        self._neg_compiled_patterns: dict[str, list[re.Pattern[str]]] = {}  # feature_id → [neg patterns]
        self._mtime: float = 0.0
        self._load()

    def _load(self) -> None:
        """Load or reload the catalog from disk."""
        if not self._path.exists():
            log.warning("Prewarm catalog not found at %s — using empty catalog", self._path)
            return

        mtime = os.path.getmtime(self._path)
        if mtime == self._mtime:
            return  # No change

        with open(self._path, "rb") as f:
            data = tomllib.load(f)

        features: dict[str, FeatureDefinition] = {}
        keyword_idx: dict[str, list[str]] = {}
        extension_idx: dict[str, list[str]] = {}
        pattern_idx: dict[str, list[re.Pattern[str]]] = {}
        neg_pattern_idx: dict[str, list[re.Pattern[str]]] = {}

        for fid, fdata in data.get("features", {}).items():
            kw = tuple(fdata.get("keywords", []))
            capabilities = tuple(fdata.get("capabilities", []))
            inputs = tuple(fdata.get("inputs", []))
            operations = tuple(fdata.get("operations", []))
            exts = tuple(fdata.get("file_extensions", []))
            patterns = tuple(fdata.get("patterns", []))
            examples = tuple(fdata.get("example_queries", []))
            neg_kw = tuple(fdata.get("negative_keywords", []))
            neg_patterns = tuple(fdata.get("negative_patterns", []))

            feat = FeatureDefinition(
                feature_id=fid,
                display_name=fdata.get("display_name", fid),
                description=fdata.get("description", ""),
                container_name=fdata.get("container_name", fid.replace("_", "-")),
                capabilities=capabilities,
                inputs=inputs,
                operations=operations,
                keywords=kw,
                file_extensions=exts,
                patterns=patterns,
                example_queries=examples,
                negative_keywords=neg_kw,
                negative_patterns=neg_patterns,
                startup_cost=fdata.get("startup_cost", "low"),
                uses_gpu=fdata.get("uses_gpu", False),
                prewarm_policy=fdata.get("prewarm_policy", "standard"),
                prewarm_threshold=fdata.get("prewarm_threshold", 0.75),
                ttl_idle=fdata.get("ttl_idle", 300),
                priority=fdata.get("priority", 5),
            )
            features[fid] = feat

            # Build keyword index
            for kw_item in kw:
                keyword_idx.setdefault(kw_item.lower(), []).append(fid)

            # Build extension index
            for ext in exts:
                ext_clean = ext.lstrip(".").lower()
                extension_idx.setdefault(ext_clean, []).append(fid)

            # Compile regex patterns
            compiled: list[re.Pattern[str]] = []
            for pat in patterns:
                try:
                    compiled.append(re.compile(pat, re.IGNORECASE))
                except re.error:
                    log.warning("Invalid regex pattern in catalog for %s: %s", fid, pat)
            pattern_idx[fid] = compiled

            # Compile negative patterns
            neg_compiled: list[re.Pattern[str]] = []
            for pat in neg_patterns:
                try:
                    neg_compiled.append(re.compile(pat, re.IGNORECASE))
                except re.error:
                    log.warning("Invalid negative regex in catalog for %s: %s", fid, pat)
            neg_pattern_idx[fid] = neg_compiled

        self._features = features
        self._keyword_index = keyword_idx
        self._extension_index = extension_idx
        self._compiled_patterns = pattern_idx
        self._neg_compiled_patterns = neg_pattern_idx
        self._mtime = mtime
        log.info("Loaded prewarm catalog: %d features from %s", len(features), self._path)

    def reload_if_changed(self) -> None:
        """Reload catalog if the file has been modified."""
        if self._path.exists():
            mtime = os.path.getmtime(self._path)
            if mtime != self._mtime:
                self._load()

    def get_all(self) -> dict[str, FeatureDefinition]:
        """Return all feature definitions."""
        return self._features

    def get_prewarm_targets(self) -> dict[str, FeatureDefinition]:
        """Return features whose policy allows predictive lifecycle starts."""
        return {
            fid: feat
            for fid, feat in self._features.items()
            if self.is_prewarm_target(fid)
        }

    def is_prewarm_target(self, feature_id: str) -> bool:
        """Return whether a feature may participate in predictive prewarming."""
        feat = self._features.get(feature_id)
        if not feat:
            return False
        return feat.prewarm_policy.strip().lower() not in _PREWARM_DISABLED_POLICIES

    def get(self, feature_id: str) -> FeatureDefinition | None:
        """Get a single feature by ID."""
        return self._features.get(feature_id)

    def get_by_keyword(self, keyword: str) -> list[str]:
        """Get feature IDs that match a keyword."""
        return self._keyword_index.get(keyword.lower(), [])

    def get_by_extension(self, ext: str) -> list[str]:
        """Get feature IDs that match a file extension."""
        return self._extension_index.get(ext.lstrip(".").lower(), [])

    def get_patterns(self, feature_id: str) -> list[re.Pattern[str]]:
        """Get compiled patterns for a feature."""
        return self._compiled_patterns.get(feature_id, [])

    def get_negative_patterns(self, feature_id: str) -> list[re.Pattern[str]]:
        """Get compiled negative patterns for a feature."""
        return self._neg_compiled_patterns.get(feature_id, [])

    def get_gpu_features(self) -> list[str]:
        """Return IDs of features that require GPU."""
        return [fid for fid, f in self.get_prewarm_targets().items() if f.uses_gpu]

    @property
    def feature_ids(self) -> list[str]:
        """All registered feature IDs."""
        return list(self._features.keys())

    @property
    def prewarm_target_ids(self) -> list[str]:
        """Feature IDs that may be used as lifecycle prewarm targets."""
        return list(self.get_prewarm_targets().keys())
