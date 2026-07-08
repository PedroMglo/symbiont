"""Read-only Git regression archaeology for local workspaces."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from typing import Any

from sharedai.evidence.reporting import append_key_value_section, append_storage_reference


@dataclass(frozen=True)
class GitCommandResult:
    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True)
class CommitInfo:
    commit: str
    subject: str
    body: str = ""
    parents: tuple[str, ...] = ()


class GitHistoryUnavailable(RuntimeError):
    """Raised when the canonical Git history reader cannot inspect the repo."""


def resolve_git_workspace(path: str | None, *, host_home_prefix: str | None = None) -> Path | None:
    raw = (path or "").strip()
    if not raw or "\x00" in raw:
        return None
    candidates = [Path(raw)]
    host_home = (host_home_prefix or os.environ.get("HOST_HOME_PREFIX") or "").strip().rstrip("/")
    if host_home and raw == host_home:
        candidates.append(Path("/host_home"))
    elif host_home and raw.startswith(f"{host_home}/"):
        candidates.append(Path("/host_home") / raw[len(host_home) + 1 :])
    parts = Path(raw).parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "home":
        candidates.append(Path("/host_home").joinpath(*parts[3:]))
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.is_dir():
            return resolved
    return None


def find_nested_repo(workspace: Path) -> Path | None:
    """Find the most likely nested Git repository under a workspace."""

    root = workspace.resolve()
    direct = root / "repo"
    if (direct / ".git").exists():
        return direct
    if (root / ".git").exists():
        return root
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if child.is_dir() and (child / ".git").exists():
            return child
    return None


def build_git_regression_report(workspace: Path, query: str) -> dict[str, Any]:
    repo = find_nested_repo(workspace)
    if repo is None:
        return {
            "workspace": str(workspace),
            "repo": None,
            "error": "git_repository_not_found",
            "candidates": [],
            "report": [],
        }

    terms = _query_terms(query)
    try:
        commits = _commits(repo)
    except GitHistoryUnavailable as exc:
        return {
            "workspace": str(workspace.resolve()),
            "repo": str(repo),
            "error": "git_history_unavailable",
            "detail": str(exc),
            "candidates": [],
            "report": [],
        }
    candidates = [_score_commit(repo, commit, terms) for commit in commits]
    candidates.sort(key=lambda item: item["score"], reverse=True)
    likely = candidates[0] if candidates else None
    current_logic = _current_logic_evidence(repo, terms)
    false_leads = [item for item in candidates[1:6] if item["score"] > 0]
    regression_test, regression_test_metadata = _suggest_regression_test(repo, query, current_logic, likely)
    minimal_patch, minimal_patch_metadata = _suggest_minimal_patch(repo, current_logic, likely)

    return {
        "workspace": str(workspace.resolve()),
        "repo": str(repo),
        "query_terms": terms,
        "likely_regression": likely,
        "candidates": candidates[:10],
        "current_logic": current_logic,
        "minimal_failing_test": regression_test,
        "minimal_failing_test_metadata": regression_test_metadata,
        "minimal_patch": minimal_patch,
        "minimal_patch_metadata": minimal_patch_metadata,
        "validation_commands": [
            f"cd {repo.name}",
            "python -m unittest",
            "git show --stat --patch <suspect-commit>",
            "git diff -- <changed-runtime-file> <changed-test-file>",
        ],
        "false_leads": false_leads,
        "risks": [
            "prove the reported duplicated observable action happens exactly once",
            "keep existing successful flows covered by the current test suite",
            "separate immediate duplicate side effects from retry/idempotency concerns",
            "prefer one canonical side-effect path when code both performs an action directly and emits an event for that action",
        ],
    }


def format_git_regression_report(report: dict[str, Any], *, published_uri: str | None = None) -> str:
    likely = report.get("likely_regression") or {}
    lines = ["# Git regression archaeology report", ""]
    append_storage_reference(lines, published_uri)
    if report.get("error"):
        lines.append(f"Error: {report['error']}")
        return "\n".join(lines).strip() + "\n"
    append_key_value_section(
        lines,
        "Executive summary",
        [
            ("suspect_commit", f"`{str(likely.get('commit', ''))[:7]}` {likely.get('subject', '')}"),
            ("assertion_mode", report.get("minimal_failing_test_metadata", {}).get("assertion_mode", "unknown")),
            ("patch_kind", report.get("minimal_patch_metadata", {}).get("kind", "unknown")),
            ("next_safe_step", "add the regression test, apply/review the minimal patch, then run the repo test suite"),
        ],
    )

    lines.extend([
        "",
        "## Likely regression commit",
        f"- `{str(likely.get('commit', ''))[:7]}` {likely.get('subject', '')}",
        f"- score: {likely.get('score', 0)}",
        "- why: " + "; ".join(likely.get("reasons", [])[:8]),
        "",
        "## Relevant business logic",
    ])
    for item in report.get("current_logic", {}).get("evidence", []):
        lines.append(f"- `{item['path']}:{item['line']}` {item['text']}")

    lines.extend([
        "",
        "## Minimal failing test",
        f"- assertion_mode: {report.get('minimal_failing_test_metadata', {}).get('assertion_mode', 'unknown')}",
        f"- expected_literal_source: {report.get('minimal_failing_test_metadata', {}).get('expected_literal_source', 'none')}",
        "```python",
        report.get("minimal_failing_test", "").rstrip(),
        "```",
        "",
        "## Minimal patch",
        f"- patch_kind: {report.get('minimal_patch_metadata', {}).get('kind', 'unknown')}",
        f"- patch_confidence: {report.get('minimal_patch_metadata', {}).get('confidence', 'unknown')}",
        "```diff",
        report.get("minimal_patch", "").rstrip(),
        "```",
        "",
        "## False leads considered",
    ])
    for item in report.get("false_leads", [])[:5]:
        lines.append(f"- `{str(item.get('commit', ''))[:7]}` {item.get('subject', '')}: {', '.join(item.get('reasons', [])[:4])}")

    lines.extend(["", "## Validation commands"])
    for command in report.get("validation_commands", []):
        lines.append(f"- `{command}`")

    lines.extend(["", "## Regression risks"])
    for risk in report.get("risks", []):
        lines.append(f"- {risk}")
    return "\n".join(lines).strip() + "\n"


def _query_terms(query: str) -> list[str]:
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_-]{2,}", (query or "").lower())
    stop = {
        "the", "and", "with", "this", "that", "inside", "local", "files", "produce", "task",
        "estás", "trabalhar", "num", "lab", "local", "stress", "test", "agentic", "todas",
        "evidências", "estão", "dentro", "pasta", "atual", "agora", "resolve", "cenário",
        "for", "not", "git", "cover", "covers", "only", "should", "would",
    }
    terms: list[str] = []
    for word in words:
        if word not in stop and word not in terms:
            terms.append(word)
    return terms[:40]


_CODE_SUFFIXES = frozenset({
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".ts",
    ".tsx",
})
_TEXT_SUFFIXES = _CODE_SUFFIXES | frozenset({".json", ".md", ".toml", ".yaml", ".yml"})
_SIDE_EFFECT_RE = re.compile(
    r"\b(send|notify|email|emit|dispatch|publish|enqueue|write|delete|charge|invoice|post|put|request|save|create|update|subscribe)\w*\b",
    re.I,
)
_EVENT_RE = re.compile(r"\b(emit|dispatch|publish|enqueue|subscribe|event|handler|listener)\w*\b", re.I)
_DUPLICATE_TERMS = frozenset({"duplicate", "duplicated", "double", "twice", "again", "repeat", "repeated"})


def _commits(repo: Path) -> list[CommitInfo]:
    result = _git(repo, "log", "--all", "--format=%H%x1f%P%x1f%s%x1f%b%x1e")
    if result.returncode != 0:
        detail = result.stderr.strip() or f"git log failed with status {result.returncode}"
        raise GitHistoryUnavailable(detail)
    commits: list[CommitInfo] = []
    for record in result.stdout.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1f", 3)
        if len(parts) < 3:
            continue
        commit, parents, subject = parts[:3]
        body = parts[3] if len(parts) == 4 else ""
        commits.append(CommitInfo(commit=commit, subject=subject, body=body, parents=tuple(parents.split())))
    return commits


def _score_commit(repo: Path, info: CommitInfo, terms: list[str]) -> dict[str, Any]:
    patch = _show_commit_patch(repo, info)
    changed_paths = _changed_paths(patch)
    added_lines = [line[1:].strip() for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
    haystack = f"{info.subject}\n{info.body}\n{' '.join(changed_paths)}\n{patch}".lower()
    subject_haystack = f"{info.subject}\n{info.body}".lower()
    score = 0
    reasons: list[str] = []

    term_hits = [term for term in terms if term in haystack]
    subject_hits = [term for term in terms if term in subject_haystack]
    if term_hits:
        score += min(10, len(set(term_hits)))
        reasons.append("matches query terms: " + ", ".join(sorted(set(term_hits))[:8]))
    if subject_hits:
        score += min(6, len(set(subject_hits)) * 2)
        reasons.append("commit message matches reported symptom")

    runtime_paths = [path for path in changed_paths if _is_runtime_path(path)]
    if runtime_paths:
        score += 4
        reasons.append("touches runtime source: " + ", ".join(runtime_paths[:4]))

    test_doc_paths = [path for path in changed_paths if _is_test_or_doc_path(path)]
    if test_doc_paths and not runtime_paths:
        score -= 6
        reasons.append("only touches tests/docs/config-looking files")

    side_effect_additions = [line for line in added_lines if _is_observable_action_line(line)]
    if side_effect_additions and runtime_paths:
        score += 5
        reasons.append("adds observable side-effect code")
    elif side_effect_additions:
        reasons.append("mentions side-effect terms outside runtime source")
    if side_effect_additions and runtime_paths and _query_suggests_duplicate(terms):
        score += 4
        reasons.append("side-effect addition is suspicious for a duplicate-action report")

    event_additions = [line for line in added_lines if _EVENT_RE.search(line)]
    if event_additions and runtime_paths:
        score += 2
        reasons.append("near event/dispatcher flow")

    if any(line.lstrip().startswith(("if ", "elif ", "case ")) for line in added_lines) and term_hits:
        score += 2
        reasons.append("adds conditional branch matching symptom vocabulary")

    subject_lower = info.subject.lower()
    if any(word in subject_lower for word in ("format", "lint", "typo", "readme", "doc")) and not runtime_paths:
        score -= 2
        reasons.append("message looks documentation/format-only")
    if any("fixture" in path.lower() for path in changed_paths) and not runtime_paths:
        score -= 2
        reasons.append("fixture-only looking change")

    if not reasons:
        reasons.append("low-confidence keyword overlap only")
    return {
        "commit": info.commit,
        "subject": info.subject,
        "score": score,
        "reasons": reasons,
        "changed_paths": changed_paths,
        "patch_excerpt": _excerpt_patch(patch),
    }


def _current_logic_evidence(repo: Path, terms: list[str]) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    interesting_terms = {term.lower() for term in terms if len(term) >= 4}
    for path in _iter_text_files(repo):
        rel = path.relative_to(repo).as_posix()
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            lower = stripped.lower()
            term_hits = sorted(term for term in interesting_terms if term in lower)
            side_effect = bool(_SIDE_EFFECT_RE.search(stripped))
            if not term_hits and not side_effect:
                continue
            evidence.append({
                "path": rel,
                "line": index + 1,
                "text": stripped,
                "raw_text": line.rstrip(),
                "scope": _nearest_scope(lines, index),
                "score": len(term_hits) * 2 + (3 if side_effect else 0) + (1 if _is_runtime_path(rel) else 0),
                "term_hits": term_hits[:6],
                "side_effect": side_effect,
            })
    evidence.sort(key=lambda item: (-int(item["score"]), item["path"], int(item["line"])))
    return {"evidence": evidence[:40], "files_considered": len(list(_iter_text_files(repo)))}


def _suggest_regression_test(
    repo: Path,
    query: str,
    current_logic: dict[str, Any],
    likely: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    evidence = current_logic.get("evidence", [])
    target = _first_relevant_function(evidence)
    test_path = _best_test_file(repo, target, _query_terms(query))
    symptom_name = _safe_identifier("_".join(_query_terms(query)[:6]) or "reported_regression")
    lines = []
    metadata: dict[str, Any] = {
        "target": target,
        "test_path": test_path,
        "assertion_mode": "side_effect_only",
        "expected_literal_source": "none",
    }
    if test_path:
        lines.append(f"# Add this regression case near the closest existing coverage in {test_path}.")
    else:
        lines.append("# Add this regression case near the closest existing coverage.")
    if likely:
        lines.append(f"# Suspect commit: {str(likely.get('commit', ''))[:12]} {likely.get('subject', '')}")
    if target:
        lines.append(f"def test_{symptom_name}_does_not_duplicate_observable_side_effect(self):")
        body, body_metadata = _python_test_body_from_repo(repo, test_path, target, evidence)
        lines.extend(body)
        metadata.update(body_metadata)
    else:
        lines.extend([
            f"def test_{symptom_name}_does_not_duplicate_observable_side_effect(self):",
            "    # Arrange the smallest fixture that reaches the suspect branch.",
            "    # Act once through the public API named in the task.",
            "    # Assert the business result is still correct.",
            "    # Assert the externally visible side effect count is exactly 1.",
            "    assert observed_side_effect_count == 1",
        ])
    return "\n".join(lines) + "\n", metadata


def _suggest_minimal_patch(repo: Path, current_logic: dict[str, Any], likely: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    evidence = current_logic.get("evidence", [])
    direct = _direct_side_effect_to_remove(evidence)
    if direct is None:
        excerpt = "\n".join((likely or {}).get("patch_excerpt", [])[:8])
        if excerpt:
            return (
                "# No safe one-line patch was derived automatically.\n"
                "# Start from the suspect commit excerpt and remove/guard the duplicated side-effect path.\n"
                f"{excerpt}\n"
            ), {"kind": "conceptual_patch", "confidence": "low", "reason": "no_canonical_side_effect_line"}
        return (
            "# No safe patch was derived automatically; inspect the suspect side-effect paths first.\n",
            {"kind": "none", "confidence": "low", "reason": "no_patch_candidate"},
        )
    path = direct["path"]
    text = direct.get("raw_text") or direct["text"]
    patch = _single_line_removal_patch(repo, path, int(direct.get("line") or 0))
    if patch:
        return patch, {
            "kind": "applicable_unified_diff",
            "confidence": "medium",
            "removed_line": text.strip(),
            "path": path,
            "line": direct.get("line"),
        }
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@\n"
        f"-{text}\n"
        "# Conceptual patch only: preserve one canonical side-effect path.\n",
        {"kind": "conceptual_patch", "confidence": "low", "path": path, "line": direct.get("line")},
    )


def _single_line_removal_patch(repo: Path, rel_path: str, line_number: int) -> str:
    path = repo / rel_path
    if line_number <= 0:
        return ""
    try:
        original = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return ""
    index = line_number - 1
    if index < 0 or index >= len(original):
        return ""
    modified = original[:index] + original[index + 1 :]
    diff = unified_diff(
        [line.rstrip("\n") for line in original],
        [line.rstrip("\n") for line in modified],
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
        lineterm="",
        n=3,
    )
    return "\n".join(diff).rstrip() + "\n"


def _excerpt_patch(patch: str, *, max_lines: int = 16) -> list[str]:
    lines = []
    for line in patch.splitlines():
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            lines.append(line)
        if len(lines) >= max_lines:
            break
    return lines


def _show_commit_patch(repo: Path, info: CommitInfo) -> str:
    result = _git(repo, "show", "--stat", "--patch", "--find-renames", "--find-copies", info.commit)
    if result.returncode == 0 and result.stdout:
        return result.stdout
    return ""


def _changed_paths(patch: str) -> list[str]:
    paths: set[str] = set()
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3]
                if path.startswith("b/"):
                    path = path[2:]
                paths.add(path)
        elif line.startswith("+++ b/"):
            paths.add(line[6:].strip())
    return sorted(path for path in paths if path and path != "/dev/null")


def _query_suggests_duplicate(terms: list[str]) -> bool:
    return any(term in _DUPLICATE_TERMS or "duplic" in term for term in terms)


def _is_runtime_path(path: str) -> bool:
    lowered = path.lower()
    if _is_test_or_doc_path(path):
        return False
    return Path(lowered).suffix in _CODE_SUFFIXES


def _is_test_or_doc_path(path: str) -> bool:
    lowered = path.lower()
    parts = lowered.split("/")
    return (
        "test" in parts
        or "tests" in parts
        or lowered.startswith("docs/")
        or "/docs/" in lowered
        or Path(lowered).suffix in {".md", ".rst", ".txt"}
    )


def _iter_text_files(repo: Path, *, max_size: int = 256_000):
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        if ".git" in path.relative_to(repo).parts:
            continue
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > max_size:
                continue
        except OSError:
            continue
        yield path


def _nearest_scope(lines: list[str], index: int) -> str | None:
    current = lines[index]
    current_indent = len(current) - len(current.lstrip(" "))
    if current_indent == 0 and not current.lstrip().startswith(("def ", "class ", "function ")):
        return None
    for pos in range(index, max(-1, index - 80), -1):
        raw = lines[pos]
        stripped = raw.strip()
        match = re.match(r"(def|class|function)\s+([A-Za-z_][A-Za-z0-9_]*)", stripped)
        if not match:
            continue
        candidate_indent = len(raw) - len(raw.lstrip(" "))
        if pos == index or candidate_indent < current_indent:
            return match.group(2)
    return None


def _first_relevant_function(evidence: list[dict[str, Any]]) -> str | None:
    scores: dict[str, int] = {}
    for item in evidence:
        scope = str(item.get("scope") or "")
        if scope and not scope.startswith("test_"):
            score = int(item.get("score") or 0)
            if not scope.startswith("_"):
                score += 3
            if _SIDE_EFFECT_RE.search(str(item.get("text") or "")):
                score += 2
            scores[scope] = scores.get(scope, 0) + score
        match = re.search(r"\bdef\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", str(item.get("text") or ""))
        if match and not match.group(1).startswith("test_"):
            scores[match.group(1)] = scores.get(match.group(1), 0) + int(item.get("score") or 0) + 1
    if not scores:
        return None
    return max(scores.items(), key=lambda item: (item[1], not item[0].startswith("_"), item[0]))[0]


def _best_test_file(repo: Path, target: str | None, terms: list[str]) -> str | None:
    best: tuple[int, str] | None = None
    for path in _iter_text_files(repo):
        rel = path.relative_to(repo).as_posix()
        if "test" not in rel.lower():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        score = 0
        if target and target.lower() in text:
            score += 8
        score += sum(1 for term in terms if term in text)
        if score <= 0:
            continue
        candidate = (score, rel)
        if best is None or candidate > best:
            best = candidate
    return best[1] if best else None


def _python_test_body_from_repo(
    repo: Path,
    test_path: str | None,
    target: str,
    evidence: list[dict[str, Any]],
) -> tuple[list[str], dict[str, Any]]:
    imported_names = _imported_names(repo / test_path) if test_path else set()
    branch_literals = _branch_literals(evidence)
    has_unittest = bool(test_path and (repo / test_path).is_file() and "unittest" in (repo / test_path).read_text(encoding="utf-8", errors="replace"))
    indent = "    "
    body: list[str] = []
    metadata: dict[str, Any] = {
        "assertion_mode": "side_effect_only",
        "expected_literal_source": "none",
    }

    positional_params = _target_positional_params(repo, target)

    cycle_literal = None
    if any("cycle_position" in str(item.get("text") or "") for item in evidence):
        cycle_literal = _choose_literal(branch_literals, preferred=("mid_cycle", "mid-cycle", "midcycle", "start"))

    body.append(f"{indent}subject = object()")
    call_args = ["subject"]
    for position, param in enumerate(positional_params[1:], 1):
        if "=" in param or param.startswith("*"):
            continue
        name = _safe_identifier(param) or f"arg_{position}"
        body.append(f"{indent}{name} = object()")
        call_args.append(name)
    if cycle_literal and cycle_literal != "start":
        call_args.append(f'cycle_position="{cycle_literal}"')
    body.append(f"{indent}result = {target}({', '.join(call_args)})")

    expected_literal = _result_literal_evidence(repo)
    if expected_literal:
        metadata["assertion_mode"] = "proven_literal_and_side_effect"
        metadata["expected_literal_source"] = expected_literal["source"]
        metadata["expected_literal"] = expected_literal["value"]
        result_key = expected_literal.get("result_key") or "result_value"
        assertion = "self.assertEqual" if has_unittest else "assert"
        if assertion == "self.assertEqual":
            body.append(f'{indent}{assertion}(result.get("{result_key}"), "{expected_literal["value"]}")')
        else:
            body.append(f'{indent}{assertion} result.get("{result_key}") == "{expected_literal["value"]}"')
    else:
        body.append(f"{indent}# Assert the business result only with a literal proven by existing tests, fixtures, or contract.")

    side_effect_name = _side_effect_counter_name(imported_names)
    assertion = "self.assertEqual" if has_unittest else "assert"
    if side_effect_name and assertion == "self.assertEqual":
        body.append(f"{indent}{assertion}(len({side_effect_name}), 1)")
    elif side_effect_name:
        body.append(f"{indent}{assertion} len({side_effect_name}) == 1")
    else:
        body.append(f"{indent}# Assert the externally visible side effect count is exactly 1, not 2.")
        body.append(f"{indent}assert observed_side_effect_count == 1")
    return body, metadata


def _imported_names(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    names: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"\s*from\s+[\w.]+\s+import\s+(.+)$", line)
        if not match:
            continue
        for raw in match.group(1).split(","):
            name = raw.strip().split(" as ", 1)[0].strip()
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                names.add(name)
    return names


def _target_positional_params(repo: Path, target: str) -> list[str]:
    for path in _iter_text_files(repo):
        rel = path.relative_to(repo).as_posix()
        if not _is_runtime_path(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(rf"def\s+{re.escape(target)}\s*\(([^)]*)\)", text)
        if not match:
            continue
        params: list[str] = []
        for raw in match.group(1).split(","):
            raw_param = raw.strip()
            name = raw_param.split("=", 1)[0].split(":", 1)[0].strip()
            if not name or name in {"self", "cls"}:
                continue
            params.append(f"{name}=..." if "=" in raw_param else name)
        return params
    return []


def _branch_literals(evidence: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for item in evidence:
        text = str(item.get("text") or "")
        for value in re.findall(r"['\"]([^'\"]{2,60})['\"]", text):
            if value not in seen:
                seen.add(value)
                values.append(value)
    return values[:40]


def _choose_literal(values: list[str], *, preferred: tuple[str, ...]) -> str | None:
    lowered = {value.lower(): value for value in values}
    for item in preferred:
        if item in lowered:
            return lowered[item]
    return values[0] if values else None


def _side_effect_counter_name(imported_names: set[str]) -> str | None:
    for name in sorted(imported_names):
        lowered = name.lower()
        if any(token in lowered for token in ("email", "mail", "notifier", "publisher", "dispatcher", "queue")):
            return f"{name}.sent"
    return None


def _result_literal_evidence(repo: Path) -> dict[str, Any] | None:
    candidates: dict[str, dict[str, Any]] = {}
    eventish = {"email", "template", "id", "event", "handler"}
    for path in _iter_text_files(repo):
        rel = path.relative_to(repo).as_posix()
        is_runtime = _is_runtime_path(rel)
        is_test_or_fixture = _is_test_or_doc_path(rel) or any(part in {"fixtures", "fixture", "snapshots"} for part in Path(rel).parts)
        if not is_runtime and not is_test_or_fixture:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(("def ", "class ", "import ", "from ")):
                continue
            if _EVENT_RE.search(line) or _is_observable_action_line(line):
                continue
            result_key = _result_key_for_line(line)
            runtime_result = _runtime_result_literal_context(lines, index, result_key) if is_runtime else None
            is_runtime_result_line = runtime_result is not None
            is_test_expectation_line = is_test_or_fixture and bool(
                re.search(r"\b(assert|expected|expectation|snapshot)\b", line, re.I)
            )
            if not is_runtime_result_line and not is_test_expectation_line:
                continue
            literal_scan_text = _literal_scan_text(lines, index) if runtime_result is not None else line
            for value in re.findall(r"['\"]([^'\"]{2,60})['\"]", literal_scan_text):
                if result_key == value and re.search(rf"['\"]{re.escape(value)}['\"]\s*:", line):
                    continue
                lowered = value.lower()
                score = 0
                source = "runtime"
                effective_result_key = result_key
                if is_test_or_fixture:
                    source = "test_or_fixture"
                    score += 5
                if runtime_result is not None:
                    source = "runtime_returned_value"
                    score += 6
                    effective_result_key = runtime_result
                if effective_result_key:
                    score += 5
                if "-" in value or "_" in value:
                    score += 2
                if lowered in eventish or lowered.endswith("_email") or lowered.endswith("_name"):
                    score -= 6
                if _EVENT_RE.search(value):
                    score -= 3
                if score > 0:
                    existing = candidates.get(value)
                    if existing is None or score > int(existing["score"]):
                        candidates[value] = {
                            "value": value,
                            "score": score,
                            "source": source,
                            "path": rel,
                            "line": index + 1,
                            "result_key": effective_result_key,
                        }
    if candidates:
        best = max(candidates.values(), key=lambda item: (int(item["score"]), len(str(item["value"])), str(item["value"])))
        if int(best["score"]) >= 5:
            return best
    return None


def _runtime_result_literal_context(lines: list[str], index: int, result_key: str | None) -> str | None:
    """Return the observable result key for a runtime literal when it is proven.

    Runtime branch literals are often inputs, labels or event names. They are
    only safe as expected output values when the same function returns the
    literal directly, or returns the assigned variable through a response
    dictionary. Plain test setup like ``result = call(DomainObject("x"))`` is therefore
    excluded elsewhere by requiring assertions/expectations for test files.
    """

    line = lines[index]
    stripped = line.strip()
    returned_dict_literal = re.search(r"return\s+\{[^}]*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*:\s*['\"][^'\"]+['\"]", stripped)
    if returned_dict_literal:
        return returned_dict_literal.group(1)

    if result_key is None:
        return None
    assignment = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*=", stripped)
    if not assignment:
        return None
    variable = assignment.group(1)
    if variable != result_key:
        return None

    start = _function_start_index(lines, index)
    end = _function_end_index(lines, start)
    if start is None:
        return None
    function_lines = lines[index + 1:end]
    for later in function_lines:
        later_stripped = later.strip()
        dict_return = re.search(
            rf"return\s+\{{[^}}]*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*:\s*{re.escape(variable)}\b",
            later_stripped,
        )
        if dict_return:
            return dict_return.group(1)
    return None


def _literal_scan_text(lines: list[str], index: int, *, max_lines: int = 8) -> str:
    """Return the current logical assignment expression for literal scanning."""

    current = lines[index]
    if not re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=", current):
        return current
    collected = [current]
    balance = current.count("(") + current.count("[") + current.count("{")
    balance -= current.count(")") + current.count("]") + current.count("}")
    for pos in range(index + 1, min(len(lines), index + max_lines)):
        stripped = lines[pos].strip()
        if stripped.startswith("return "):
            break
        collected.append(lines[pos])
        balance += lines[pos].count("(") + lines[pos].count("[") + lines[pos].count("{")
        balance -= lines[pos].count(")") + lines[pos].count("]") + lines[pos].count("}")
        if balance <= 0 and stripped.endswith((")", "]", "}",)):
            break
    return "\n".join(collected)


def _function_start_index(lines: list[str], index: int) -> int | None:
    for pos in range(index, -1, -1):
        stripped = lines[pos].strip()
        if re.match(r"(async\s+def|def|function)\s+[A-Za-z_][A-Za-z0-9_]*\s*\(", stripped):
            return pos
    return None


def _function_end_index(lines: list[str], start: int | None) -> int:
    if start is None:
        return len(lines)
    start_line = lines[start]
    start_indent = len(start_line) - len(start_line.lstrip(" "))
    for pos in range(start + 1, len(lines)):
        raw = lines[pos]
        stripped = raw.strip()
        if not stripped:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent <= start_indent and re.match(r"(async\s+def|def|class|function)\s+", stripped):
            return pos
    return len(lines)


def _result_key_for_line(line: str) -> str | None:
    stripped = line.strip()
    assignment = re.match(r"([A-Za-z_][A-Za-z0-9_]*(?:mode|status|state|result|type|code)?)\s*=", stripped)
    if assignment and not stripped.startswith(("if ", "elif ", "while ", "for ")):
        return assignment.group(1)
    returned_dict = re.search(r"return\s+\{[^}]*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*:", stripped)
    if returned_dict:
        return returned_dict.group(1)
    result_get = re.search(r"result\.get\(['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\)", stripped)
    if result_get:
        return result_get.group(1)
    return None


def _safe_identifier(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower()
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"regression_{cleaned}"
    return cleaned[:80]


def _direct_side_effect_to_remove(evidence: list[dict[str, Any]]) -> dict[str, Any] | None:
    runtime = [item for item in evidence if _is_runtime_path(str(item.get("path") or ""))]
    for item in runtime:
        text = str(item.get("text") or "")
        if not _is_observable_action_line(text):
            continue
        if _EVENT_RE.search(text):
            continue
        for other in runtime:
            if other is item or other.get("path") != item.get("path"):
                continue
            item_scope = item.get("scope")
            other_scope = other.get("scope")
            if item_scope or other_scope:
                if item_scope != other_scope:
                    continue
            if abs(int(other.get("line", 0)) - int(item.get("line", 0))) > 12:
                continue
            if _EVENT_RE.search(str(other.get("text") or "")):
                return item
    return None


def _is_observable_action_line(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith(("def ", "class ", "function ", "import ", "from ")):
        return False
    if not _SIDE_EFFECT_RE.search(stripped):
        return False
    if "(" not in stripped:
        return False
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=\s*\(?\s*$", stripped):
        return False
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", stripped) and "." not in stripped.split("=", 1)[0]:
        rhs = stripped.split("=", 1)[1]
        if not re.search(r"[A-Za-z_][A-Za-z0-9_.]*\s*\(", rhs):
            return False
    return True


def _git(repo: Path, *args: str) -> GitCommandResult:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError as exc:
        return GitCommandResult("", str(exc), 127)
    except subprocess.TimeoutExpired as exc:
        return GitCommandResult(exc.stdout or "", exc.stderr or "git command timed out", 124)
    return GitCommandResult(proc.stdout, proc.stderr, proc.returncode)
