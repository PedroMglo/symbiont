"""Config Center ownership gate for generated Compose interpolations."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
GENERATE_MESSAGE: Final = "generate Config Center env with config.resolver"

OPTIONAL_EMPTY_GENERATED_ENV_KEYS: Final = frozenset(
    {
        "AI_LOCAL_GIT_URL_INSTEAD_OF",
        "AI_STORAGE_EXPECTED_DEVICE_LABEL",
        "AI_STORAGE_EXPECTED_DEVICE_UUID",
        "AI_STORAGE_EXTERNAL_ROOT",
        "AI_STORAGE_GUARDIAN_EXTERNAL_ROOT",
        "AI_STORAGE_MIGRATION_ACTIVATION_RECEIPT",
        "LLAMA_CPP_CODE_VULKAN_HOST_ICD",
        "ORC_LIFECYCLE_PRE_WARM",
        "REDIS_HEALTHCHECK_PATH",
        "WORKSPACE_EXECUTION_VM_CONTROL_TOKEN_FILE",
        "WORKSPACE_EXECUTION_VM_CONTROL_URL",
    }
)

_ENV_TOKEN = re.compile(r"[A-Z][A-Z0-9_]*")
_OPERATORS: Final = (":-", ":?", ":+", "-", "?", "+")


@dataclass(frozen=True, slots=True)
class ComposeInterpolation:
    start: int
    end: int
    line: int
    column: int
    key: str
    operator: str | None


def compose_source_paths(project_root: Path = PROJECT_ROOT) -> tuple[Path, ...]:
    candidates = {
        project_root / "compose.yml",
        *(project_root / "infra" / "docker" / "compose").glob("*.yml"),
        *(project_root / "infra" / "docker" / "compose").glob("*.yaml"),
        *(project_root / "infra" / "gateway").glob("compose*.yml"),
        *(project_root / "infra" / "gateway").glob("compose*.yaml"),
    }
    return tuple(sorted(path for path in candidates if path.is_file()))


def _is_escaped_dollar(text: str, start: int) -> bool:
    dollar_count = 0
    cursor = start
    while cursor >= 0 and text[cursor] == "$":
        dollar_count += 1
        cursor -= 1
    return dollar_count % 2 == 0


def _balanced_interpolation_end(text: str, start: int) -> int | None:
    depth = 1
    cursor = start + 2
    while cursor < len(text):
        if text.startswith("${", cursor):
            depth += 1
            cursor += 2
            continue
        if text[cursor] == "}":
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    return None


def _interpolation_key_and_operator(body: str) -> tuple[str, str | None] | None:
    match = _ENV_TOKEN.match(body)
    if match is None:
        return None
    key = match.group(0)
    remainder = body[match.end() :]
    if not remainder:
        return key, None
    for operator in _OPERATORS:
        if remainder.startswith(operator):
            return key, operator
    return key, "invalid"


def iter_compose_interpolations(text: str) -> Iterator[ComposeInterpolation]:
    cursor = 0
    while True:
        start = text.find("${", cursor)
        if start < 0:
            return
        cursor = start + 2
        if _is_escaped_dollar(text, start):
            continue
        end = _balanced_interpolation_end(text, start)
        if end is None:
            continue
        parsed = _interpolation_key_and_operator(text[start + 2 : end - 1])
        if parsed is None:
            continue
        key, operator = parsed
        line_start = text.rfind("\n", 0, start) + 1
        yield ComposeInterpolation(start=start, end=end, line=text.count("\n", 0, start) + 1, column=start - line_start + 1, key=key, operator=operator)


def required_operator(key: str) -> str:
    return "?" if key in OPTIONAL_EMPTY_GENERATED_ENV_KEYS else ":?"


def required_interpolation(key: str) -> str:
    return f"${{{key}{required_operator(key)}{GENERATE_MESSAGE}}}"


def _projection_errors(generated_values: Mapping[str, str]) -> list[str]:
    errors: list[str] = []
    invalid_keys = sorted(key for key in generated_values if _ENV_TOKEN.fullmatch(key) is None)
    if invalid_keys:
        errors.append("generated env projection contains invalid keys: " + ", ".join(invalid_keys))
    unclassified_empty = sorted(key for key, value in generated_values.items() if value == "" and key not in OPTIONAL_EMPTY_GENERATED_ENV_KEYS)
    if unclassified_empty:
        errors.append("generated env projection contains unclassified empty fields: " + ", ".join(unclassified_empty))
    return errors


def validate_generated_compose_interpolations(generated_values: Mapping[str, str], *, paths: Sequence[Path] | None = None, project_root: Path = PROJECT_ROOT) -> list[str]:
    errors = _projection_errors(generated_values)
    generated_keys = frozenset(generated_values)
    sources = tuple(paths) if paths is not None else compose_source_paths(project_root)
    for path in sources:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"could not read Compose source {path}: {exc}")
            continue
        expected_path = path
        try:
            expected_path = path.relative_to(project_root)
        except ValueError:
            pass
        for interpolation in iter_compose_interpolations(text):
            if interpolation.key not in generated_keys:
                continue
            expected = required_operator(interpolation.key)
            if interpolation.operator == expected:
                continue
            observed = interpolation.operator or "unguarded"
            errors.append(f"{expected_path}:{interpolation.line}:{interpolation.column}: generated key {interpolation.key} must use {expected!r} required interpolation, observed {observed!r}")
    return errors


def rewrite_generated_compose_interpolations(text: str, generated_values: Mapping[str, str]) -> tuple[str, int]:
    generated_keys = frozenset(generated_values)
    replacements: list[tuple[int, int, str]] = []
    covered_until = -1
    for interpolation in iter_compose_interpolations(text):
        if interpolation.key not in generated_keys:
            continue
        if interpolation.operator == required_operator(interpolation.key):
            continue
        if interpolation.start < covered_until:
            continue
        replacements.append((interpolation.start, interpolation.end, required_interpolation(interpolation.key)))
        covered_until = interpolation.end
    rewritten = text
    for start, end, replacement in reversed(replacements):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    return rewritten, len(replacements)
