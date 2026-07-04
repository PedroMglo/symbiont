#!/usr/bin/env python3
"""Configure user-owned RAG sources in config/rag/user.toml."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "rag" / "user.toml"
DEFAULT_REPORT = ROOT / ".local" / "generated" / "rag-sources.report.json"


def _load_config() -> dict[str, Any]:
    if tomllib is None or not CONFIG_PATH.exists():
        return {}
    return tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        clean = value.strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        unique.append(clean)
    return unique


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_list(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def _render_config(data: dict[str, Any]) -> str:
    paths = data.get("paths", {})
    retrieval = data.get("retrieval", {})
    router = data.get("router", {})
    reranker = data.get("reranker", {})
    debug = data.get("debug", {})
    repos = data.get("repos", {})
    graphify = data.get("graphify", {})
    observability = data.get("observability", {})

    vault_dirs = [str(value) for value in paths.get("vault_dirs", [])]
    repo_paths = [str(value) for value in repos.get("paths", [])]

    return f"""# Obsidian RAG - User Configuration
# Edit this file or run `make rag-sources ARGS="--vault-dir ~/Notes --repo-path ~/src"`.
# Values here override config/rag/internal.toml. RAG_* env vars override both.

[paths]
# Personal Markdown/Obsidian sources. Empty by default for portable installs.
vault_dirs = {_toml_list(vault_dirs)}

[sync]
backend = {_toml_string(data.get("sync", {}).get("backend", "direct"))}

[retrieval]
context_mode = {_toml_string(retrieval.get("context_mode", "auto"))}
token_budget = {int(retrieval.get("token_budget", 6000))}

[api]
# API key is provided through Docker secrets/env in normal ai-local usage.

[router]
enabled = {_toml_bool(bool(router.get("enabled", True)))}

[reranker]
enabled = {_toml_bool(bool(reranker.get("enabled", True)))}

[debug]
enabled = {_toml_bool(bool(debug.get("enabled", False)))}
log_level = {_toml_string(str(debug.get("log_level", "INFO")))}
log_format = {_toml_string(str(debug.get("log_format", "text")))}

[repos]
# Local repositories to index. Empty by default for portable installs.
paths = {_toml_list(repo_paths)}

[graphify]
enabled = {_toml_bool(bool(graphify.get("enabled", False)))}
backend = {_toml_string(str(graphify.get("backend", "ollama")))}
graph_vault_dir = {_toml_string(str(graphify.get("graph_vault_dir", "data/graphify-vault")))}
auto_update = {_toml_bool(bool(graphify.get("auto_update", True)))}
extract_mode = {_toml_string(str(graphify.get("extract_mode", "deep")))}

[observability]
enabled = {_toml_bool(bool(observability.get("enabled", True)))}
clickhouse_database = {_toml_string(str(observability.get("clickhouse_database", "obsidian_rag")))}
clickhouse_username = {_toml_string(str(observability.get("clickhouse_username", "default")))}
resource_sampling = {_toml_bool(bool(observability.get("resource_sampling", True)))}
"""


def _existing_sources(vaults: list[str], repos: list[str]) -> list[dict[str, Any]]:
    checks = []
    for kind, values in (("vault", vaults), ("repo", repos)):
        for raw in values:
            path = Path(raw).expanduser()
            checks.append(
                {
                    "kind": kind,
                    "path": raw,
                    "exists": path.exists(),
                    "resolved": str(path.resolve()) if path.exists() else str(path),
                }
            )
    return checks


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    data = _load_config()
    data.setdefault("paths", {})
    data.setdefault("repos", {})
    data.setdefault("graphify", {})

    if args.clear:
        data["paths"]["vault_dirs"] = []
        data["repos"]["paths"] = []

    current_vaults = [str(value) for value in data.get("paths", {}).get("vault_dirs", [])]
    current_repos = [str(value) for value in data.get("repos", {}).get("paths", [])]
    data["paths"]["vault_dirs"] = _unique([*current_vaults, *args.vault_dir])
    data["repos"]["paths"] = _unique([*current_repos, *args.repo_path])

    if args.graphify is not None:
        data["graphify"]["enabled"] = args.graphify
    if args.graph_vault_dir:
        data["graphify"]["graph_vault_dir"] = args.graph_vault_dir

    rendered = _render_config(data)
    source_checks = _existing_sources(data["paths"]["vault_dirs"], data["repos"]["paths"])
    missing = [check for check in source_checks if not check["exists"]]
    changed = bool(args.clear or args.vault_dir or args.repo_path or args.graphify is not None or args.graph_vault_dir)

    if changed and not args.dry_run:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(rendered, encoding="utf-8")

    return {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config_path": str(CONFIG_PATH),
        "changed": changed and not args.dry_run,
        "dry_run": args.dry_run,
        "vault_dirs": data["paths"]["vault_dirs"],
        "repo_paths": data["repos"]["paths"],
        "graphify_enabled": bool(data["graphify"].get("enabled", False)),
        "graph_vault_dir": str(data["graphify"].get("graph_vault_dir", "data/graphify-vault")),
        "source_checks": source_checks,
        "ok": True,
        "warnings": [f"source path does not exist yet: {check['path']}" for check in missing],
    }


def write_report(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def print_text(payload: dict[str, Any]) -> None:
    print("== ai-local RAG sources ==")
    print(f"Config: {payload['config_path']}")
    print(f"Vaults: {len(payload['vault_dirs'])}")
    for value in payload["vault_dirs"]:
        print(f"  - {value}")
    print(f"Repos: {len(payload['repo_paths'])}")
    for value in payload["repo_paths"]:
        print(f"  - {value}")
    print(f"Graphify: {'enabled' if payload['graphify_enabled'] else 'disabled'}")
    print(f"Graph vault dir: {payload['graph_vault_dir']}")
    for warning in payload["warnings"]:
        print(f"WARN {warning}")
    if payload["changed"]:
        print("\nUpdated config/rag/user.toml")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-dir", action="append", default=[], help="Markdown/Obsidian directory to add.")
    parser.add_argument("--repo-path", action="append", default=[], help="Repository directory to add.")
    parser.add_argument("--clear", action="store_true", help="Remove configured vaults and repos before adding new ones.")
    graphify = parser.add_mutually_exclusive_group()
    graphify.add_argument("--graphify", dest="graphify", action="store_true", default=None)
    graphify.add_argument("--no-graphify", dest="graphify", action="store_false")
    parser.add_argument("--graph-vault-dir", default="", help="Directory for Graphify exports.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write-report", nargs="?", const=str(DEFAULT_REPORT), metavar="PATH")
    args = parser.parse_args(argv)

    payload = build_payload(args)
    if args.write_report:
        write_report(payload, Path(args.write_report))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_text(payload)
        if args.write_report:
            print(f"\nGenerated: {args.write_report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
