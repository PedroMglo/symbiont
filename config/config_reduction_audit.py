"""Audit TOML configuration that can move behind autoconfiguration."""

from __future__ import annotations

import argparse
import json
import time
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JSON_PATH = ROOT / ".local" / "generated" / "config-reduction-audit.json"
DEFAULT_MD_PATH = ROOT / "docs" / "generated" / "config-reduction-audit.md"

SCAN_ROOTS = (
    ROOT / "config" / "orc",
    ROOT / "config" / "rag",
    ROOT / "agents",
    ROOT / "features",
    ROOT / "storage_guardian",
    ROOT / "obsidian-rag",
    ROOT / "orchestrator" / "capabilities",
    ROOT / "orchestrator" / "prewarming",
    ROOT / "infra" / "security",
)

GENERATED_CATEGORIES = {
    "generated_service_registry",
    "generated_storage_path",
    "inferred_runtime_budget",
    "private_local_config",
    "removed_compatibility_path",
}

PERFORMANCE_TERMS = (
    "timeout",
    "ttl",
    "interval",
    "worker",
    "workers",
    "concurrent",
    "concurrency",
    "batch",
    "budget",
    "limit",
    "limits",
    "threshold",
    "percent",
    "memory",
    "cpu",
    "vram",
    "swap",
    "retry",
    "retries",
    "samples",
    "retention",
    "num_ctx",
    "num_predict",
    "top_k",
)

PATH_TERMS = (
    "path",
    "paths",
    "dir",
    "dirs",
    "directory",
    "db_path",
    "cache_dir",
    "data_dir",
    "output_dir",
    "graph_vault_dir",
)

SERVICE_WIRING_TERMS = (
    "url",
    "host",
    "port",
    "health_interval",
    "healthcheck_path",
    "healthcheck_timeout",
)

CLASSIFIED_MANUAL_MIGRATIONS = {
    "agent_behavior_policy": "keep_agent_behavior_policy",
    "agentic_control_policy": "keep_agentic_control_policy",
    "audio_processing_policy": "keep_audio_processing_policy",
    "data_contract": "keep_data_contract",
    "document_processing_policy": "keep_document_processing_policy",
    "feature_behavior_policy": "keep_feature_behavior_policy",
    "interface_contract": "keep_catalog_contract",
    "language_policy": "keep_language_policy",
    "observability_policy": "keep_observability_policy",
    "prompt_contract": "keep_prompt_contract",
    "quality_policy": "keep_quality_policy",
    "rag_processing_policy": "keep_rag_processing_policy",
    "routing_policy": "keep_routing_policy",
    "security_policy": "keep_security_policy",
    "session_policy": "keep_session_policy",
}


@dataclass(frozen=True)
class ConfigFinding:
    file: str
    key: str
    category: str
    migration: str
    reason: str
    value_type: str
    sample: str


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _relative(path: Path, root: Path = ROOT) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    if not isinstance(data, dict):
        return {}
    return data


def _candidate_files(scan_roots: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for root in scan_roots:
        if root.is_file() and root.suffix == ".toml":
            files.append(root)
            continue
        if not root.exists():
            continue
        for path in root.rglob("*.toml"):
            if path.name in {"pyproject.toml", ".gitleaks.toml"}:
                continue
            files.append(path)
    return sorted(set(files))


def _sample(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=True, sort_keys=True)
    if len(text) > 96:
        return text[:93] + "..."
    return text


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        items: list[tuple[str, Any]] = []
        for key, child in sorted(value.items()):
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            items.extend(_flatten(child, child_prefix))
        return items
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        items = []
        for item in value:
            items.extend(_flatten(item, f"{prefix}[]"))
        return items
    return [(prefix, value)]


def _contains_local_path(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith("~/") or "/home/" in value or "/Users/" in value
    if isinstance(value, list):
        return any(_contains_local_path(item) for item in value)
    return False


def _leaf(key: str) -> str:
    return key.rsplit(".", 1)[-1].replace("[]", "")


def _is_endpoint_contract(key: str) -> bool:
    return any(
        token in key
        for token in (
            "dispatch.agent_endpoints",
            "dispatch.feature_endpoints",
            "dispatch.source_map",
        )
    )


def _is_capability_manifest_field(file: str, key: str) -> bool:
    if file.endswith("service_capabilities.toml") and key.startswith("service_capabilities[]."):
        return True
    if file.endswith("orchestrator/capabilities/action_capabilities.toml") and key.startswith("action_capabilities[]."):
        return True
    return False


def _is_command_registry_field(file: str, key: str) -> bool:
    return file.endswith("orchestrator/capabilities/command_registry.toml") and key.startswith("commands[].")


def _classify_prewarming_catalog(file: str, key: str) -> tuple[str, str, str] | None:
    if file != "orchestrator/prewarming/catalog.toml" or not key.startswith("features."):
        return None

    leaf = _leaf(key)
    if leaf == "container_name":
        return _classified_manual(
            "interface_contract",
            "Prewarming lifecycle identities must match the live runtime registry/lifecycle map.",
        )
    if leaf in {
        "display_name",
        "description",
        "capabilities",
        "inputs",
        "operations",
        "keywords",
        "file_extensions",
        "patterns",
        "negative_keywords",
        "negative_patterns",
        "example_queries",
    }:
        return _classified_manual(
            "routing_policy",
            "Prewarming service-intent and signal fields are orchestrator-owned routing policy.",
        )
    if leaf in {"startup_cost", "uses_gpu", "prewarm_policy", "prewarm_threshold", "ttl_idle", "priority"}:
        return _classified_manual(
            "agentic_control_policy",
            "Prewarming resource and lifecycle knobs are explicit orchestrator control policy.",
        )
    return _classified_manual(
        "routing_policy",
        "Prewarming catalog fields are owner-local service-intent policy unless promoted to central config.",
    )


def _starts_with_any(value: str, prefixes: tuple[str, ...]) -> bool:
    return value.startswith(prefixes)


def _classified_manual(category: str, reason: str) -> tuple[str, str, str]:
    return (category, CLASSIFIED_MANUAL_MIGRATIONS[category], reason)


def classify_field(file: str, key: str, value: Any) -> tuple[str, str, str]:
    """Return category, migration target and explanation for one TOML field."""

    lowered = key.lower()
    leaf = _leaf(lowered)

    if any(term in leaf for term in ("password", "secret", "api_key", "token")):
        return (
            "secret_reference",
            "keep_env_or_secret_file",
            "Secret material or secret env references must stay outside generated config.",
        )

    prewarming_catalog = _classify_prewarming_catalog(file, lowered)
    if prewarming_catalog is not None:
        return prewarming_catalog

    if _is_capability_manifest_field(file, lowered):
        return _classified_manual(
            "interface_contract",
            "Owner-published capability manifests are dispatch/API contracts consumed as runtime metadata.",
        )

    if _is_command_registry_field(file, lowered):
        return _classified_manual(
            "interface_contract",
            "Terminal command registry entries are declarative command/API contracts, not inferred runtime config.",
        )

    if file.startswith("orchestrator/capabilities/") and _starts_with_any(
        lowered,
        (
            "context_routes[].",
            "source_selection.",
            "local_command_shortcuts.",
            "escalation_policy.",
            "workspace_capabilities[].",
        ),
    ):
        return _classified_manual(
            "routing_policy",
            "Orchestrator capability routing manifests are explicit runtime policy owned by the orchestrator.",
        )

    if file in {
        "orchestrator/capabilities/escalation_policy.toml",
        "orchestrator/capabilities/local_command_shortcuts.toml",
        "orchestrator/capabilities/source_selection.toml",
    }:
        return _classified_manual(
            "routing_policy",
            "Orchestrator escalation, source-selection and shortcut manifests are declarative routing policy.",
        )

    if file == "infra/security/policy-actions.toml" and lowered.startswith("policy_actions."):
        return _classified_manual(
            "security_policy",
            "Permission action risk classes are explicit security policy owned by infra/security.",
        )

    if _is_endpoint_contract(lowered) and leaf in {"method", "path", "feature"}:
        return (
            "interface_contract",
            "keep_toml_until_service_contract_registry_exists",
            "HTTP method/path maps are service API contracts, not host-specific tuning.",
        )

    if leaf in SERVICE_WIRING_TERMS or leaf.endswith("_url"):
        return (
            "generated_service_registry",
            "config.ports/service registry -> .env.services.generated",
            "Service URLs, hosts and ports are runtime wiring that the resolver can generate.",
        )

    if file.endswith("config/orc/providers.toml") and lowered.startswith(("repos.", "calendar.", "rss.", "email.")):
        return (
            "private_local_config",
            "config/private.local.yaml",
            "Personal repos, feeds, calendars and mailboxes should be private local intent, not shared TOML.",
        )

    if file == "config/rag/user.toml" and lowered in {
        "paths.vault_dirs",
        "repos.paths",
        "graphify.graph_vault_dir",
        "rss.feeds",
        "calendar.ics_paths",
    }:
        return (
            "user_intent",
            "keep_user_config",
            "This is user-specific knowledge-source intent.",
        )

    if _contains_local_path(value):
        return (
            "private_local_config",
            "config/private.local.yaml",
            "Local user paths should move to private local config or be discovered.",
        )

    if leaf in PATH_TERMS or leaf.endswith(("_path", "_dir", "_file")):
        return (
            "generated_storage_path",
            "storage resolver -> .env.storage.generated",
            "Storage/cache/database paths should come from the effective storage root.",
        )

    if lowered.startswith("agentic_runtime_profiles."):
        return _classified_manual(
            "agentic_control_policy",
            "Agentic runtime profiles are explicit autonomy/control policy.",
        )

    if lowered.startswith("context_governor."):
        return _classified_manual(
            "agentic_control_policy",
            "Context governor ratios and pressure gates are explicit prompt-safety control policy.",
        )

    if file == "config/orc/i18n.toml" and lowered.startswith("latency."):
        return _classified_manual(
            "language_policy",
            "Language/runtime latency budgets are explicit i18n product policy in the compatibility surface.",
        )

    if lowered.startswith("classification."):
        return _classified_manual(
            "routing_policy",
            "Agent classification choices and limits are explicit routing policy.",
        )

    if lowered.startswith("response."):
        return _classified_manual(
            "feature_behavior_policy",
            "Response shaping and context limits are agent behavior policy.",
        )

    if lowered.startswith("evaluation."):
        return _classified_manual(
            "quality_policy",
            "Quality-critic heuristics and scoring behavior are explicit quality policy.",
        )

    if any(term in leaf for term in PERFORMANCE_TERMS):
        return (
            "inferred_runtime_budget",
            "config.resolver runtime modules",
            "Budgets, timeouts and limits should be inferred from CPU/RAM/GPU/storage and quality policy.",
        )

    if leaf in {"system_prompt", "prompt_template"} or leaf.endswith("_prompt"):
        return _classified_manual(
            "prompt_contract",
            "Prompts are behavioral contracts and should move only to a prompt registry, not runtime inference.",
        )

    if lowered.startswith("agentic_runtime."):
        return _classified_manual(
            "agentic_control_policy",
            "Agentic autonomy, actuator and command-tool gates are explicit safety/control policy.",
        )

    if _starts_with_any(lowered, ("dispatch.", "dynamic_routing.", "routing.", "routing_policy.")):
        return _classified_manual(
            "routing_policy",
            "Routing priorities, task classes and collaboration modes are explicit orchestration policy.",
        )

    if _starts_with_any(lowered, ("admission.downgrade_backend", "admission.default_backend")):
        return _classified_manual(
            "routing_policy",
            "Backend downgrade/default choices describe routing policy; runtime resource limits are inferred elsewhere.",
        )

    if lowered.startswith("classify."):
        return _classified_manual(
            "routing_policy",
            "Classifier vocabulary and follow-up patterns are language-aware routing policy.",
        )

    if _starts_with_any(
        lowered,
        (
            "final_response.",
            "i18n.",
            "i18n_rag.",
            "language_detection.",
            "protected_spans.",
            "spellcheck.",
            "translation.",
        ),
    ) or ("features/translation/config.toml" in file and lowered.startswith("policy.")):
        return _classified_manual(
            "language_policy",
            "Language, spelling and translation behavior is product policy; only runtime budgets should be inferred.",
        )

    if _starts_with_any(lowered, ("i18n_observability.", "observability.", "debug.", "logging.", "metrics.")):
        return _classified_manual(
            "observability_policy",
            "Telemetry capture, privacy and log behavior are explicit observability policy.",
        )

    if lowered.startswith("security."):
        return _classified_manual(
            "security_policy",
            "Allow-lists, upload limits, sandboxing and scanner toggles are explicit security policy.",
        )

    if lowered.startswith("workspace."):
        return _classified_manual(
            "security_policy",
            "Workspace scan roots and host-home mapping are read-scope allow-list policy for workspace-bound features.",
        )

    if lowered.startswith("session."):
        return _classified_manual(
            "session_policy",
            "Session behavior and history retention are user-facing product policy.",
        )

    if _starts_with_any(lowered, ("formats.", "export.")):
        return _classified_manual(
            "data_contract",
            "Input/output formats are external data contracts rather than machine-specific runtime tuning.",
        )

    if leaf.endswith("_binary"):
        return _classified_manual(
            "interface_contract",
            "External executable names are integration contracts for the service image.",
        )

    if file.startswith("config/rag/") or _starts_with_any(
        lowered,
        (
            "cag.",
            "chunking.",
            "context_policy.",
            "graphify.",
            "pipeline.",
            "reranker.",
            "repos.chunking.",
            "repos.collection_name",
            "retrieval.",
        ),
    ):
        return _classified_manual(
            "rag_processing_policy",
            "RAG extraction, chunking, graph and retrieval behavior are processing policy.",
        )

    if "audio_transcribe/config.toml" in file:
        return _classified_manual(
            "audio_processing_policy",
            "Audio preprocessing, transcription, VAD and diarization choices are service behavior policy.",
        )

    if "features/extrator/config.toml" in file:
        return _classified_manual(
            "document_processing_policy",
            "Document parsing, conversion and extraction choices are service behavior policy.",
        )

    if "features/personal_context/config.toml" in file and _starts_with_any(lowered, ("calendar.", "email.", "rss.")):
        return (
            "user_intent",
            "keep_user_config",
            "Personal context sources and windows are user intent, even when defaults are empty/container paths.",
        )

    if "agents/local_evidence_operator/config/code_analysis.toml" in file:
        return _classified_manual(
            "agent_behavior_policy",
            "Local evidence graph and repository scan behavior is agent policy.",
        )

    if "features/research/config.toml" in file:
        return _classified_manual(
            "feature_behavior_policy",
            "Research CAG/RAG behavior is feature policy after generated service wiring has been removed.",
        )

    if _starts_with_any(lowered, ("responder.", "synthesis.", "decomposition.")):
        return _classified_manual(
            "feature_behavior_policy",
            "Agent response, synthesis and decomposition behavior is explicit feature policy.",
        )

    if "model" in leaf or lowered.startswith("llm.backends[]") or lowered.startswith("llm.model_profiles[]"):
        return (
            "model_policy",
            "central model registry",
            "Model identities and capabilities are policy/catalog data; runtime URLs remain generated.",
        )

    if leaf in {"enabled", "mode", "strategy", "policy_mode", "backend", "engine"}:
        return (
            "manual_policy",
            "keep_high_level_intent",
            "Boolean modes and policy choices are explicit product/user intent until a higher-level policy replaces them.",
        )

    if leaf in {"name", "alias", "capabilities", "required_capabilities", "privacy_level", "format"}:
        return (
            "interface_contract",
            "keep_catalog_contract",
            "Names, aliases and capabilities describe service/model contracts.",
        )

    return (
        "manual_review",
        "review",
        "No safe automatic classification rule matched this field.",
    )


def build_config_reduction_audit(
    *,
    scan_roots: Iterable[Path] = SCAN_ROOTS,
    root: Path = ROOT,
    generated_at: str | None = None,
) -> dict[str, Any]:
    files = _candidate_files(scan_roots)
    findings: list[ConfigFinding] = []
    errors: list[dict[str, str]] = []

    for path in files:
        rel = _relative(path, root)
        try:
            doc = _load_toml(path)
        except tomllib.TOMLDecodeError as exc:
            errors.append({"file": rel, "error": str(exc)})
            continue
        for key, value in _flatten(doc):
            category, migration, reason = classify_field(rel, key, value)
            findings.append(
                ConfigFinding(
                    file=rel,
                    key=key,
                    category=category,
                    migration=migration,
                    reason=reason,
                    value_type=type(value).__name__,
                    sample=_sample(value),
                )
            )

    category_counts: dict[str, int] = {}
    file_counts: dict[str, dict[str, int]] = {}
    migration_counts: dict[str, int] = {}
    for finding in findings:
        category_counts[finding.category] = category_counts.get(finding.category, 0) + 1
        migration_counts[finding.migration] = migration_counts.get(finding.migration, 0) + 1
        file_entry = file_counts.setdefault(finding.file, {})
        file_entry[finding.category] = file_entry.get(finding.category, 0) + 1

    candidate_count = sum(count for category, count in category_counts.items() if category in GENERATED_CATEGORIES)
    review_count = category_counts.get("manual_review", 0)
    manual_intent_count = sum(
        count
        for category, count in category_counts.items()
        if category not in GENERATED_CATEGORIES and category != "manual_review"
    )

    top_candidates = [
        asdict(item)
        for item in findings
        if item.category in GENERATED_CATEGORIES
    ][:50]
    review_samples = [
        asdict(item)
        for item in findings
        if item.category == "manual_review"
    ][:50]

    recommendations = _recommendations(category_counts, migration_counts)
    status = "actionable" if candidate_count else "no_candidates"
    if errors:
        status = "partial"

    return {
        "schema_version": 2,
        "generated_at": generated_at or _now(),
        "status": status,
        "summary": {
            "files_scanned": len(files),
            "fields_scanned": len(findings),
            "candidate_count": candidate_count,
            "manual_intent_count": manual_intent_count,
            "manual_review_count": review_count,
            "error_count": len(errors),
        },
        "category_counts": dict(sorted(category_counts.items())),
        "migration_counts": dict(sorted(migration_counts.items())),
        "file_counts": {key: dict(sorted(value.items())) for key, value in sorted(file_counts.items())},
        "top_candidates": top_candidates,
        "review_samples": review_samples,
        "recommendations": recommendations,
        "errors": errors,
    }


def _recommendations(category_counts: dict[str, int], migration_counts: dict[str, int]) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    for migration, count in sorted(migration_counts.items(), key=lambda item: (-item[1], item[0])):
        if migration == "review" or migration.startswith("keep_"):
            continue
        recommendations.append(
            {
                "id": migration.replace("/", "-").replace(" ", "-").lower(),
                "target": migration,
                "candidate_count": count,
                "priority": "high" if count >= 10 else "medium",
            }
        )
    if category_counts.get("manual_review", 0):
        recommendations.append(
            {
                "id": "review-unclassified-fields",
                "target": "review",
                "candidate_count": category_counts["manual_review"],
                "priority": "medium",
            }
        )
    return recommendations


def write_json_report(payload: dict[str, Any], path: Path = DEFAULT_JSON_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Config reduction audit",
        "",
        f"Generated at: `{payload['generated_at']}`",
        "",
        "## Summary",
        "",
        f"- Files scanned: `{summary['files_scanned']}`",
        f"- Fields scanned: `{summary['fields_scanned']}`",
        f"- Candidates for generated/inferred/private config: `{summary['candidate_count']}`",
        f"- Manual intent / contracts / secrets: `{summary['manual_intent_count']}`",
        f"- Manual review fields: `{summary['manual_review_count']}`",
        "",
        "## Category Counts",
        "",
        "| Category | Count |",
        "| --- | ---: |",
    ]
    for category, count in payload["category_counts"].items():
        lines.append(f"| `{category}` | {count} |")

    lines.extend(["", "## Recommendations", "", "| Target | Candidates | Priority |", "| --- | ---: | --- |"])
    for item in payload["recommendations"]:
        lines.append(f"| `{item['target']}` | {item['candidate_count']} | {item['priority']} |")

    if payload.get("review_samples"):
        lines.extend(
            [
                "",
                "## Remaining Manual Review Samples",
                "",
                "| File | Key | Type | Sample |",
                "| --- | --- | --- | --- |",
            ]
        )
        for item in payload["review_samples"][:25]:
            lines.append(
                f"| `{item['file']}` | `{item['key']}` | `{item['value_type']}` | `{item['sample']}` |"
            )

    lines.extend(
        [
            "",
            "## First Candidates",
            "",
            "| File | Key | Category | Migration |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in payload["top_candidates"][:25]:
        lines.append(
            f"| `{item['file']}` | `{item['key']}` | `{item['category']}` | `{item['migration']}` |"
        )
    return "\n".join(lines) + "\n"


def write_markdown_report(payload: dict[str, Any], path: Path = DEFAULT_MD_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(payload), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m config.config_reduction_audit")
    parser.add_argument("--json", action="store_true", help="print the JSON report")
    parser.add_argument("--write", type=Path, default=None, help="write JSON report")
    parser.add_argument("--write-md", type=Path, default=None, help="write Markdown report")
    args = parser.parse_args(argv)

    payload = build_config_reduction_audit()
    if args.write:
        write_json_report(payload, args.write)
    if args.write_md:
        write_markdown_report(payload, args.write_md)
    if args.json or not (args.write or args.write_md):
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
