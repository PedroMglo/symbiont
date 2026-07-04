#!/usr/bin/env python3
"""Generate the authoritative make-infra Docker CI/CD image matrix for ai-local.

The integrated root repo owns Docker validation and publishing. This script
keeps that gate scoped to images built by `make infra`: the resolved Compose
graph, mandatory direct targets, and explicit Dockerfile-only build steps that
`infra/docker/scripts/infra_ops.py` runs during `make infra`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import docker_policy  # noqa: E402,I001


GENERATED_DIR = ROOT / ".local" / "generated"
INVENTORY_JSON = GENERATED_DIR / "docker-ci-inventory.json"
INVENTORY_MD = GENERATED_DIR / "docker-ci-inventory.md"
IMAGE_BUILD_CATALOG = ROOT / "config" / "docker" / "image-build-catalog.toml"

MAKE_INFRA_DOCKERFILE_ONLY_IMAGES = (
    ROOT / "infra" / "docker" / "images" / "command-sandbox" / "Dockerfile",
)
DEFAULT_DOCKERFILE_ONLY_IMAGES = MAKE_INFRA_DOCKERFILE_ONLY_IMAGES


def _slug(value: str) -> str:
    text = value.replace("_", "-").lower()
    text = re.sub(r"[^a-z0-9_.-]+", "-", text)
    return text.strip("-") or "image"


def _path_for_job(path: Path) -> str:
    resolved = path.resolve(strict=False)
    try:
        rel = resolved.relative_to(ROOT)
    except ValueError:
        return str(resolved)
    return rel.as_posix() or "."


def _resolve_context(path: Any) -> Path:
    if not path:
        return ROOT
    candidate = Path(str(path))
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve(strict=False)


def _resolve_dockerfile(context: Path, dockerfile: Any) -> Path:
    candidate = Path(str(dockerfile or "Dockerfile"))
    if not candidate.is_absolute():
        candidate = context / candidate
    return candidate.resolve(strict=False)


def _replace_image_tag(image: str | None, tag_suffix: str, *, fallback: str) -> str:
    if not image:
        return f"{fallback}:{tag_suffix}"
    last_segment = image.rsplit("/", 1)[-1]
    if ":" in last_segment:
        return f"{image.rsplit(':', 1)[0]}:{tag_suffix}"
    return f"{image}:{tag_suffix}"


def _component_for_service(service_name: str, service: dict[str, Any]) -> str:
    image = str(service.get("image") or "")
    if service_name == "ollama-proxy" and image.startswith("ai-local-base:"):
        return "base"
    return _slug(service_name)


def _component_for_dockerfile(path: Path) -> str:
    parts = set(path.parts)
    if "command-sandbox" in parts:
        return "command-sandbox"
    if path.name == "Dockerfile.base":
        return "base"
    stem = path.name
    for suffix in (".Dockerfile", ".dockerfile", ".base", ".Dockerfile.base", ".Dockerfile"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    if stem == "Dockerfile":
        stem = path.parent.name
    return _slug(stem)


def _context_for_dockerfile(path: Path) -> Path:
    if "command-sandbox" in path.parts:
        return path.parent
    return ROOT


def _dockerfile_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _needs_base(build: dict[str, Any], dockerfile: Path) -> bool:
    args = build.get("args") or {}
    if isinstance(args, dict) and "AI_LOCAL_BASE_TAG" in args:
        return True
    text = _dockerfile_text(dockerfile)
    return "AI_LOCAL_BASE_TAG" in text or "FROM ai-local-base:" in text


def _needs_audio_runtime(build: dict[str, Any], dockerfile: Path) -> bool:
    args = build.get("args") or {}
    if isinstance(args, dict) and "AI_LOCAL_IMAGE_TAG" in args:
        return True
    text = _dockerfile_text(dockerfile)
    return "AI_LOCAL_IMAGE_TAG" in text or "FROM ai-local-audio-runtime:" in text


def _smoke_kind(component: str) -> str:
    if component == "command-sandbox":
        return "command-sandbox"
    if component in {"rag", "symbiont"}:
        return component
    return "python"


def _build_entry(
    *,
    component: str,
    context: Path,
    dockerfile: Path,
    image: str | None,
    source: str,
    service: str,
    profiles: list[str],
    tag_suffix: str,
    needs_base: bool,
    needs_audio_runtime: bool = False,
    target: str = "",
    build_args: list[str] | None = None,
) -> dict[str, Any]:
    fallback = "obsidian-rag" if component == "rag" else f"ai-local-{component}"
    tag = _replace_image_tag(image, tag_suffix, fallback=fallback)
    entry = {
        "component": component,
        "service": service,
        "dockerfile": _path_for_job(dockerfile),
        "context": _path_for_job(context),
        "tag": tag,
        "needs_base": needs_base,
        "needs_audio_runtime": needs_audio_runtime,
        "smoke": _smoke_kind(component),
        "profiles": sorted(str(item) for item in profiles),
        "source": source,
        "artifact": _slug(component),
    }
    if target:
        entry["target"] = target
    if build_args:
        entry["build_args"] = sorted(str(item) for item in build_args)
    return entry


def _compose_build_entries(compose: dict[str, Any], *, tag_suffix: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for service_name, service in sorted((compose.get("services") or {}).items()):
        build = service.get("build")
        if not isinstance(build, dict):
            continue
        context = _resolve_context(build.get("context"))
        dockerfile = _resolve_dockerfile(context, build.get("dockerfile"))
        component = _component_for_service(str(service_name), service)
        entries.append(
            _build_entry(
                component=component,
                context=context,
                dockerfile=dockerfile,
                image=service.get("image"),
                source="compose",
                service=str(service_name),
                profiles=list(service.get("profiles") or []),
                tag_suffix=tag_suffix,
                needs_base=_needs_base(build, dockerfile),
                needs_audio_runtime=_needs_audio_runtime(build, dockerfile),
            )
        )
    return entries


def _dockerfile_only_entries(
    used_dockerfiles: set[str],
    *,
    tag_suffix: str,
    dockerfiles: tuple[Path, ...],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for dockerfile in dockerfiles:
        resolved = dockerfile.resolve(strict=False)
        dockerfile_key = _path_for_job(resolved)
        if dockerfile_key in used_dockerfiles:
            continue
        component = _component_for_dockerfile(resolved)
        context = _context_for_dockerfile(resolved)
        entries.append(
            _build_entry(
                component=component,
                context=context,
                dockerfile=resolved,
                image=None,
                source="dockerfile",
                service=component,
                profiles=[],
                tag_suffix=tag_suffix,
                needs_base=_needs_base({}, resolved),
                needs_audio_runtime=_needs_audio_runtime({}, resolved),
            )
        )
    return entries


def _read_direct_targets() -> tuple[dict[str, Any], ...]:
    if not IMAGE_BUILD_CATALOG.exists():
        return ()
    data = tomllib.loads(IMAGE_BUILD_CATALOG.read_text(encoding="utf-8"))
    targets = data.get("direct_targets", [])
    if not isinstance(targets, list):
        return ()
    return tuple(item for item in targets if isinstance(item, dict) and item.get("mandatory", False))


def _component_for_direct_target(target: dict[str, Any]) -> str:
    name = _slug(str(target.get("name") or "direct-image"))
    for prefix in ("ai-local-", "obsidian-"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _image_for_direct_target(target: dict[str, Any], tag_suffix: str) -> str:
    image_template = str(target.get("image") or "")
    if not image_template:
        return f"ai-local-{_component_for_direct_target(target)}:{tag_suffix}"
    return image_template.format(tag=tag_suffix)


def _direct_target_entries(*, tag_suffix: str, direct_targets: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for target in direct_targets:
        context = _resolve_context(target.get("context") or ".")
        dockerfile = _resolve_dockerfile(context, target.get("dockerfile") or "Dockerfile")
        component = _component_for_direct_target(target)
        target_stage = str(target.get("target") or "")
        build_args = [str(item) for item in target.get("build_args") or []]
        entries.append(
            _build_entry(
                component=component,
                context=context,
                dockerfile=dockerfile,
                image=_image_for_direct_target(target, tag_suffix),
                source="catalog",
                service=component,
                profiles=[],
                tag_suffix=tag_suffix,
                needs_base=("AI_LOCAL_BASE_TAG" in build_args) or _needs_base({"args": {item: "" for item in build_args}}, dockerfile),
                needs_audio_runtime=("AI_LOCAL_IMAGE_TAG" in build_args)
                or _needs_audio_runtime({"args": {item: "" for item in build_args}}, dockerfile),
                target=target_stage,
                build_args=build_args,
            )
        )
    return entries


def _external_entries(compose: dict[str, Any]) -> list[dict[str, Any]]:
    by_image: dict[str, dict[str, Any]] = {}
    for service_name, service in sorted((compose.get("services") or {}).items()):
        if service.get("build"):
            continue
        image = str(service.get("image") or "").strip()
        if not image:
            continue
        entry = by_image.setdefault(
            image,
            {
                "component": _slug(service_name),
                "image": image,
                "services": [],
                "profiles": set(),
                "artifact": _slug(f"external-{service_name}"),
            },
        )
        entry["services"].append(str(service_name))
        entry["profiles"].update(str(item) for item in service.get("profiles") or [])

    normalized: list[dict[str, Any]] = []
    for entry in by_image.values():
        normalized.append(
            {
                "component": entry["component"],
                "image": entry["image"],
                "services": sorted(entry["services"]),
                "profiles": sorted(entry["profiles"]),
                "artifact": entry["artifact"],
            }
        )
    return sorted(normalized, key=lambda item: (item["component"], item["image"]))


def build_ci_inventory(
    compose: dict[str, Any],
    *,
    tag_suffix: str = "ci",
    dockerfiles: tuple[Path, ...] = DEFAULT_DOCKERFILE_ONLY_IMAGES,
    direct_targets: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    build_images = _compose_build_entries(compose, tag_suffix=tag_suffix)
    build_images.extend(
        _direct_target_entries(
            tag_suffix=tag_suffix,
            direct_targets=_read_direct_targets() if direct_targets is None else direct_targets,
        )
    )
    used_dockerfiles = {str(item["dockerfile"]) for item in build_images}
    build_images.extend(
        _dockerfile_only_entries(used_dockerfiles, tag_suffix=tag_suffix, dockerfiles=dockerfiles)
    )

    deduped: dict[str, dict[str, Any]] = {}
    for item in build_images:
        key = f"{item['component']}|{item['context']}|{item['dockerfile']}|{item.get('target', '')}"
        deduped[key] = item
    build_images = sorted(
        deduped.values(),
        key=lambda item: (
            item["component"] != "base",
            item["component"] != "audio-runtime",
            item["component"],
        ),
    )
    release_images = [item for item in build_images if item["component"] != "base"]
    external_images = _external_entries(compose)

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tag_suffix": tag_suffix,
        "build_images": build_images,
        "release_images": release_images,
        "external_images": external_images,
        "build_count": len(build_images),
        "release_count": len(release_images),
        "external_count": len(external_images),
    }


def build_inventory_from_compose(*, tag_suffix: str) -> dict[str, Any]:
    catalog = docker_policy.load_catalog()
    compose = docker_policy.compose_config(catalog)
    return build_ci_inventory(compose, tag_suffix=tag_suffix)


def _matrix(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {"image": items}


def github_outputs(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "build_matrix": _matrix(payload["build_images"]),
        "release_matrix": _matrix(payload["release_images"]),
        "external_matrix": _matrix(payload["external_images"]),
        "build_count": str(payload["build_count"]),
        "release_count": str(payload["release_count"]),
        "external_count": str(payload["external_count"]),
    }


def write_github_output(path: Path, payload: dict[str, Any]) -> None:
    outputs = github_outputs(payload)
    with path.open("a", encoding="utf-8") as fh:
        for key, value in outputs.items():
            text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
            delimiter = f"__AI_LOCAL_{key.upper()}__"
            fh.write(f"{key}<<{delimiter}\n{text}\n{delimiter}\n")


def _doc(payload: dict[str, Any]) -> str:
    lines = [
        "# Docker CI Inventory",
        "",
        f"Generated at: `{payload['generated_at']}`",
        f"Tag suffix: `{payload['tag_suffix']}`",
        "",
        "## Project Images",
        "",
        "| Component | Service | Dockerfile | Context | Target | Needs base | Needs audio runtime | Smoke | Source |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in payload["build_images"]:
        row = {**item, "target": item.get("target") or "-"}
        lines.append(
            "| `{component}` | `{service}` | `{dockerfile}` | `{context}` | `{target}` | `{needs_base}` | `{needs_audio_runtime}` | `{smoke}` | `{source}` |".format(
                **row,
            )
        )
    lines.extend(
        [
            "",
            "## External Images",
            "",
            "| Image | Services | Profiles |",
            "| --- | --- | --- |",
        ]
    )
    for item in payload["external_images"]:
        lines.append(
            f"| `{item['image']}` | {', '.join(f'`{service}`' for service in item['services'])} | "
            f"{', '.join(item['profiles']) or '-'} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_inventory(payload: dict[str, Any]) -> None:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    INVENTORY_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    INVENTORY_MD.write_text(_doc(payload), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag-suffix", default="ci", help="local tag suffix for validation builds")
    parser.add_argument("--write", action="store_true", help="write .local/generated inventory artifacts")
    parser.add_argument("--json", action="store_true", help="print the full inventory payload as JSON")
    parser.add_argument("--github-output", type=Path, help="append matrix outputs to a GitHub Actions output file")
    args = parser.parse_args(argv)

    payload = build_inventory_from_compose(tag_suffix=args.tag_suffix)

    if args.write:
        write_inventory(payload)
    if args.github_output:
        write_github_output(args.github_output, payload)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "Docker CI inventory: "
            f"{payload['build_count']} project image(s), "
            f"{payload['external_count']} external image(s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
