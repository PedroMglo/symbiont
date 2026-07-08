"""Read-only Bash/script safety diagnostics for local workspaces."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any

from sharedai.evidence.reporting import append_key_value_section, append_storage_reference

_SCRIPT_SUFFIXES = (".sh", ".bash", ".zsh", ".ksh")
_DESTRUCTIVE_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("rm-recursive-force", r"\brm\s+[^#\n]*-[A-Za-z]*r[A-Za-z]*f|rm\s+[^#\n]*-[A-Za-z]*f[A-Za-z]*r", "critical"),
    ("find-delete", r"\bfind\b[^#\n]*\s-delete\b", "critical"),
    ("rsync-delete", r"\brsync\b[^#\n]*--delete\b", "high"),
    ("tar-unsafe-extract", r"\btar\b[^#\n]*\s-[A-Za-z]*x[A-Za-z]*\b", "high"),
    ("for-ls-word-splitting", r"\bfor\s+[A-Za-z_][A-Za-z0-9_]*\s+in\s+\$\(\s*ls\b", "high"),
    ("gnu-date-d", r"\bdate\s+-d\b", "medium"),
    ("chmod-recursive", r"\bchmod\b[^#\n]*\s-R\b", "medium"),
    ("chown-recursive", r"\bchown\b[^#\n]*\s-R\b", "medium"),
    ("eval-execution", r"\beval\b", "high"),
    ("curl-pipe-shell", r"\b(curl|wget)\b[^|;\n]*\|\s*(sh|bash)\b", "critical"),
    ("sudo-required", r"\bsudo\b", "medium"),
    ("mktemp-u", r"\bmktemp\b[^#\n]*\s-u\b", "high"),
)
_UNQUOTED_VAR_RE = re.compile(r"(?<![\"'])\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})(?![\"'])")
_COMMAND_LOW_ACTION = "command.run.read_only"
_COMMAND_MEDIUM_ACTION = "command.run.medium"
_COMMAND_DENY_ACTION = "command.run.deny"
_READ_ONLY_COMMANDS = frozenset({
    "awk",
    "cat",
    "cut",
    "df",
    "du",
    "find",
    "git",
    "grep",
    "head",
    "ls",
    "nl",
    "pwd",
    "rg",
    "sed",
    "sort",
    "stat",
    "tail",
    "tr",
    "wc",
})
_WRITE_COMMANDS = frozenset({
    "cp",
    "install",
    "ln",
    "mkdir",
    "mv",
    "patch",
    "tee",
    "touch",
})
_DENIED_COMMANDS = frozenset({
    "bash",
    "chgrp",
    "chmod",
    "chown",
    "curl",
    "dd",
    "docker",
    "eval",
    "fish",
    "mkfs",
    "mount",
    "nc",
    "perl",
    "python",
    "python3",
    "rm",
    "rsync",
    "scp",
    "sh",
    "ssh",
    "su",
    "sudo",
    "umount",
    "wget",
    "wipefs",
    "zsh",
})
_SAFE_GIT_SUBCOMMANDS = frozenset({
    "diff",
    "log",
    "rev-parse",
    "show",
    "status",
})
_SAFE_GIT_BRANCH_FLAGS = frozenset({
    "--show-current",
})
_DENIED_PATH_MARKERS = (
    "/run/docker.sock",
    "/var/lib/docker",
    "/proc/kcore",
    "/etc/shadow",
    "/etc/sudoers",
    ".ssh/",
    "id_rsa",
    "id_ed25519",
    "infra/docker/secrets",
)
_DENIED_TEXT_MARKERS = (
    "$(",
    "`",
    "<<<",
    "<<",
    "||",
    "&&",
    ";",
    "\x00",
)
_REDIRECTION_PATTERN = re.compile(r"(^|\s)([12]?>>?|<)($|\s)")
_WORKSPACE_GENERATION_PROFILE = "workspace_generation"
_WORKSPACE_GENERATION_MAX_COMMAND_LENGTH = 32000
_WORKSPACE_GENERATION_DENIED_EXECUTABLES = _DENIED_COMMANDS
_WORKSPACE_GENERATION_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_WORKSPACE_GENERATION_SEGMENT_RE = re.compile(r"\s*(?:&&|\|\||[;|])\s*")


def resolve_bash_workspace(path: str | None, *, host_home_prefix: str | None = None) -> Path | None:
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


def build_bash_safety_report(workspace: Path, query: str = "") -> dict[str, Any]:
    """Inspect shell scripts read-only and report operational safety risks."""

    del query
    root = workspace.resolve()
    scripts = _find_scripts(root)
    script_reports = [_inspect_script(path, root) for path in scripts]
    issues = [issue for report in script_reports for issue in report.get("issues", [])]
    return {
        "workspace": str(root),
        "analysis_mode": "read_only_shell_static_safety_review",
        "policy": {
            "scripts_executed": False,
            "writes_performed": False,
            "dry_run_required_for_dangerous_actions": True,
            "max_scripts": 40,
            "max_bytes_per_script": 262144,
        },
        "scripts": script_reports,
        "summary": {
            "scripts_seen": len(script_reports),
            "issues_seen": len(issues),
            "critical_count": sum(1 for item in issues if item.get("severity") == "critical"),
            "high_count": sum(1 for item in issues if item.get("severity") == "high"),
            "medium_count": sum(1 for item in issues if item.get("severity") == "medium"),
        },
        "recommended_validation": [
            "bash -n <script>",
            "shellcheck <script> # when available",
            "run dangerous branches only in a disposable sandbox with explicit dry-run flags",
            "review generated diffs before applying script changes",
        ],
        "limitations": [
            "Static analysis cannot prove runtime expansions for all environment values.",
            "This provider does not execute scripts and does not mutate the workspace.",
            "Findings are ranked by generic shell safety patterns, not by scenario-specific expected answers.",
        ],
    }


def classify_shell_command(command: str, *, context_profile: str = "project_context") -> dict[str, Any]:
    """Classify a single shell command without executing it."""

    original = (command or "").strip()
    if not original:
        return _command_deny("", "empty_command")
    raw = original if context_profile == _WORKSPACE_GENERATION_PROFILE else " ".join(original.split())
    if context_profile == _WORKSPACE_GENERATION_PROFILE:
        return _classify_workspace_generation_command(raw)
    if len(raw) > 4000:
        return _command_deny(raw, "command_too_long")

    lowered = raw.lower()
    denied = [marker for marker in _DENIED_TEXT_MARKERS if marker in raw]
    denied.extend(marker for marker in _DENIED_PATH_MARKERS if marker.lower() in lowered)
    if _REDIRECTION_PATTERN.search(raw):
        denied.append("redirection")
    if context_profile == "host_context_ro":
        denied.append("host_context_ro_requires_manual_approval")
    if denied:
        return _command_deny(raw, "denied_marker", denied)

    segments = [part.strip() for part in raw.split("|")]
    if not all(segments):
        return _command_deny(raw, "empty_pipeline_segment")

    tokens: list[str] = []
    risk = "low"
    reasons: list[str] = []
    for segment in segments:
        try:
            segment_tokens = shlex.split(segment)
        except ValueError as exc:
            return _command_deny(raw, f"parse_error:{exc}")
        if not segment_tokens:
            return _command_deny(raw, "empty_segment")
        tokens.extend(segment_tokens)
        segment_risk, reason = _classify_command_segment(segment_tokens)
        reasons.append(reason)
        risk = _max_command_risk(risk, segment_risk)
        if segment_risk == "deny":
            return _command_deny(raw, reason, tuple(segment_tokens))

    if risk == "medium":
        return _command_result(
            command=raw,
            action=_COMMAND_MEDIUM_ACTION,
            risk_level="medium",
            decision_hint="allow_with_audit",
            reason="; ".join(reasons),
            tokens=tokens,
        )
    return _command_result(
        command=raw,
        action=_COMMAND_LOW_ACTION,
        risk_level="low",
        decision_hint="allow",
        reason="read-only command within command sandbox allowlist",
        tokens=tokens,
    )


def _classify_workspace_generation_command(raw: str) -> dict[str, Any]:
    """Classify writes that stay inside a disposable workspace_execution copy."""

    if len(raw) > _WORKSPACE_GENERATION_MAX_COMMAND_LENGTH:
        return _command_deny(raw, "command_too_long")

    lowered = raw.lower()
    denied = [marker for marker in _DENIED_PATH_MARKERS if marker.lower() in lowered]
    denied.extend(
        executable
        for executable in _workspace_generation_executables(raw)
        if executable in _WORKSPACE_GENERATION_DENIED_EXECUTABLES
    )
    if denied:
        return _command_deny(raw, "workspace_generation_denied_marker", denied)

    tokens = _workspace_generation_executables(raw)[:200]
    return _command_result(
        command=raw,
        action=_COMMAND_MEDIUM_ACTION,
        risk_level="medium",
        decision_hint="allow_with_audit",
        reason="workspace_generation_session_write",
        tokens=tokens,
        metadata={
            "context_profile": _WORKSPACE_GENERATION_PROFILE,
            "disposable_workspace_only": True,
            "host_write_allowed": False,
        },
    )


def _workspace_generation_executables(raw: str) -> list[str]:
    """Return executable tokens from command lines while ignoring heredoc bodies."""

    executables: list[str] = []
    heredoc_delimiters: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if heredoc_delimiters:
            if stripped == heredoc_delimiters[-1]:
                heredoc_delimiters.pop()
            continue
        if not stripped:
            continue
        heredoc_delimiters.extend(_workspace_generation_heredoc_delimiters(stripped))
        executables.extend(_workspace_generation_line_executables(stripped))
    return executables


def _workspace_generation_heredoc_delimiters(line: str) -> list[str]:
    return [match.group(2) for match in _WORKSPACE_GENERATION_HEREDOC_RE.finditer(line)]


def _workspace_generation_line_executables(line: str) -> list[str]:
    command_line = _WORKSPACE_GENERATION_HEREDOC_RE.sub("", line)
    executables: list[str] = []
    for segment in _WORKSPACE_GENERATION_SEGMENT_RE.split(command_line):
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment, comments=True)
        except ValueError:
            tokens = segment.split()
        if not tokens:
            continue
        index = 0
        while index < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index]):
            index += 1
        if index >= len(tokens):
            continue
        executable = tokens[index].split("/")[-1].lower()
        if executable in {"command", "env", "nohup", "time"} and index + 1 < len(tokens):
            executable = tokens[index + 1].split("/")[-1].lower()
        executables.append(executable)
    return executables


def format_bash_safety_report(report: dict[str, Any], *, published_uri: str | None = None) -> str:
    lines = ["# Bash safety report", ""]
    append_storage_reference(lines, published_uri)
    policy = report.get("policy", {})
    summary = report.get("summary", {})
    append_key_value_section(
        lines,
        "Executive summary",
        [
            ("result", f"{summary.get('issues_seen', 0)} issue(s) across {summary.get('scripts_seen', 0)} script(s)"),
            ("critical", summary.get("critical_count", 0)),
            ("high", summary.get("high_count", 0)),
            ("next_safe_step", "review proposed mitigations and validate with bash -n or shellcheck without executing dangerous branches"),
        ],
    )
    lines.extend([
        f"- analysis mode: {report.get('analysis_mode')}",
        f"- scripts executed: {policy.get('scripts_executed')}",
        f"- writes performed: {policy.get('writes_performed')}",
        "",
        "## Summary",
        f"- scripts seen: {summary.get('scripts_seen', 0)}",
        f"- issues seen: {summary.get('issues_seen', 0)}",
        f"- critical: {summary.get('critical_count', 0)}",
        f"- high: {summary.get('high_count', 0)}",
        f"- medium: {summary.get('medium_count', 0)}",
        "",
        "## Findings",
    ])
    for script in report.get("scripts", []):
        if script.get("error"):
            lines.append(f"- `{script.get('path')}` error: {script['error']}")
            continue
        for issue in script.get("issues", []):
            lines.append(
                f"- **{issue['severity']} {issue['id']}** `{issue['path']}:{issue['line']}` "
                f"{issue['summary']} Evidence: `{issue['evidence']}` Mitigation: {issue['mitigation']}"
            )
    if not any(script.get("issues") for script in report.get("scripts", [])):
        lines.append("- No high-confidence shell safety findings were detected.")

    lines.extend(["", "## Safe validation commands"])
    for command in report.get("recommended_validation", []):
        lines.append(f"- `{command}`")
    lines.extend(["", "## Limitations"])
    for item in report.get("limitations", []):
        lines.append(f"- {item}")
    return "\n".join(lines).strip() + "\n"


def _classify_command_segment(tokens: list[str]) -> tuple[str, str]:
    executable = tokens[0].split("/")[-1]
    if executable in _DENIED_COMMANDS:
        return "deny", f"command_denied:{executable}"
    if executable in _WRITE_COMMANDS:
        return "deny", f"write_command_denied:{executable}"
    if executable not in _READ_ONLY_COMMANDS:
        return "deny", f"command_not_allowlisted:{executable}"

    if executable == "git":
        subcommand_index = next((index for index, token in enumerate(tokens[1:], 1) if not token.startswith("-")), 0)
        subcommand = tokens[subcommand_index] if subcommand_index else ""
        if subcommand == "branch":
            if _git_branch_args_are_read_only(tokens[subcommand_index + 1 :]):
                return "low", "read_only:git"
            return "deny", "git_branch_mutation_or_unknown_denied"
        if subcommand not in _SAFE_GIT_SUBCOMMANDS:
            return "deny", f"git_subcommand_denied:{subcommand or 'missing'}"
    if executable == "sed" and any(token == "-i" or token.startswith("-i") for token in tokens[1:]):  # nosec B105 - command flag, not a password
        return "deny", "sed_in_place_denied"
    if executable == "find" and "-delete" in tokens:
        return "deny", "find_delete_denied"
    if executable == "find" and "-maxdepth" not in tokens:
        return "medium", "find_without_maxdepth_is_heavy_scan"
    if executable == "du":
        return "medium", "du_can_be_heavy_scan"
    return "low", f"read_only:{executable}"


def _max_command_risk(left: str, right: str) -> str:
    order = {"low": 0, "medium": 1, "high": 2, "deny": 3}
    return left if order[left] >= order[right] else right


def _git_branch_args_are_read_only(args: list[str]) -> bool:
    return all(token in _SAFE_GIT_BRANCH_FLAGS for token in args)


def _command_deny(command: str, reason: str, markers: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
    return _command_result(
        command=command,
        action=_COMMAND_DENY_ACTION,
        risk_level="deny",
        decision_hint="deny",
        reason=reason,
        tokens=[],
        denied_markers=[str(item) for item in markers],
        dry_run_required=True,
    )


def _command_result(
    *,
    command: str,
    action: str,
    risk_level: str,
    decision_hint: str,
    reason: str,
    tokens: list[str],
    denied_markers: list[str] | None = None,
    dry_run_required: bool = False,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "command": command,
        "action": action,
        "risk_level": risk_level,
        "decision_hint": decision_hint,
        "reason": reason,
        "tokens": tokens,
        "denied_markers": denied_markers or [],
        "requires_approval": risk_level == "high",
        "dry_run_required": dry_run_required or risk_level in {"high", "deny"},
        "metadata": {
            "provider": "bash_safety",
            "analysis_mode": "single_command_static_risk",
            **(metadata or {}),
        },
    }


def _find_scripts(root: Path) -> list[Path]:
    scripts: list[Path] = []
    for path in sorted(root.rglob("*")):
        if len(scripts) >= 40:
            break
        if not path.is_file():
            continue
        if path.suffix.lower() in _SCRIPT_SUFFIXES:
            scripts.append(path)
            continue
        try:
            first = path.read_text(encoding="utf-8", errors="replace")[:128]
        except OSError:
            continue
        if first.startswith("#!") and any(shell in first.lower() for shell in ("sh", "bash", "zsh", "ksh")):
            scripts.append(path)
    return scripts


def _inspect_script(path: Path, root: Path) -> dict[str, Any]:
    rel = path.relative_to(root).as_posix()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:262144]
    except OSError as exc:
        return {"path": rel, "error": str(exc), "issues": []}
    lines = text.splitlines()
    issues: list[dict[str, Any]] = []
    code_lines = [line for line in lines if not line.strip().startswith("#")]
    has_dry_run = any(
        re.search(r"\b(DRY_RUN|dry-run|--dry-run)\b", line, re.IGNORECASE)
        for line in code_lines
    )
    dry_run_comment_line = next(
        (
            index
            for index, line in enumerate(lines, 1)
            if line.strip().startswith("#")
            and re.search(r"\b(DRY_RUN|dry-run|--dry-run)\b", line, re.IGNORECASE)
        ),
        0,
    )
    has_strict_mode = any("set -euo pipefail" in line or "set -o pipefail" in line for line in lines[:20])
    if any(re.search(r"\s", part) for part in Path(rel).parts):
        issues.append(_issue(
            "filename-contains-whitespace",
            "medium",
            rel,
            1,
            rel,
            "script path contains whitespace and can break unsafe shell loops or unquoted tooling",
            "Use null-delimited file enumeration (`find -print0`) and quote script paths in validation commands.",
        ))
    if dry_run_comment_line and not has_dry_run:
        issues.append(_issue(
            "reassuring-comment-without-code-gate",
            "medium",
            rel,
            dry_run_comment_line,
            lines[dry_run_comment_line - 1].strip(),
            "comment claims dry-run or safety behavior but no executable dry-run gate was found",
            "Treat comments as non-authoritative; add an explicit dry-run default and require `--apply` for writes.",
        ))
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for issue_id, pattern, severity in _DESTRUCTIVE_PATTERNS:
            if not re.search(pattern, stripped):
                continue
            issues.append(_issue(
                issue_id,
                severity,
                rel,
                number,
                stripped,
                _summary_for(issue_id),
                _mitigation_for(issue_id, has_dry_run=has_dry_run),
            ))
        if _looks_dangerous_target(stripped) and _UNQUOTED_VAR_RE.search(stripped):
            issues.append(_issue(
                "unquoted-variable-in-dangerous-command",
                "high",
                rel,
                number,
                stripped,
                "dangerous command contains an unquoted variable expansion",
                "Quote variables, validate non-empty paths, and require an allowlisted root before execution.",
            ))
        if _looks_path_test(stripped) and _UNQUOTED_VAR_RE.search(stripped):
            issues.append(_issue(
                "unquoted-variable-in-path-test",
                "medium",
                rel,
                number,
                stripped,
                "path existence test contains unquoted variables and can misparse empty or whitespace paths",
                "Quote variables in tests and validate required components before path construction.",
            ))
        if _looks_loop_glob(stripped) and _UNQUOTED_VAR_RE.search(stripped):
            issues.append(_issue(
                "unquoted-variable-in-loop-glob",
                "high",
                rel,
                number,
                stripped,
                "loop glob contains unquoted variables and can split paths or expand unexpectedly",
                "Use `find -print0` with `while IFS= read -r -d ''` or quote validated path prefixes.",
            ))
    if not has_strict_mode and issues:
        issues.append(_issue(
            "missing-strict-shell-mode",
            "medium",
            rel,
            1,
            lines[0].strip() if lines else "",
            "script with risky operations lacks strict shell mode near the top",
            "Use `set -euo pipefail` where compatible and handle expected failures explicitly.",
        ))
    return {
        "path": rel,
        "line_count": len(lines),
        "has_dry_run": has_dry_run,
        "has_strict_mode": has_strict_mode,
        "issues": issues,
    }


def _looks_dangerous_target(line: str) -> bool:
    lowered = line.lower()
    return any(term in lowered for term in ("rm ", "rsync ", "find ", "chmod ", "chown ", "tar "))


def _looks_path_test(line: str) -> bool:
    return bool(re.search(r"(^|\bif\s+)\[\s+-[defLrswx]\s+", line))


def _looks_loop_glob(line: str) -> bool:
    return bool(re.search(r"^for\s+[A-Za-z_][A-Za-z0-9_]*\s+in\s+.*[*?[]", line))


def _issue(
    issue_id: str,
    severity: str,
    path: str,
    line: int,
    evidence: str,
    summary: str,
    mitigation: str,
) -> dict[str, Any]:
    return {
        "id": issue_id,
        "severity": severity,
        "path": path,
        "line": line,
        "evidence": evidence[:240],
        "summary": summary,
        "mitigation": mitigation,
    }


def _summary_for(issue_id: str) -> str:
    return {
        "rm-recursive-force": "recursive force delete can erase broad path sets if variables expand unexpectedly",
        "find-delete": "find -delete can remove many files and needs explicit scoping/dry-run",
        "rsync-delete": "rsync --delete can remove destination files not present in source",
        "tar-unsafe-extract": "tar extraction can overwrite paths or allow traversal/symlink surprises without entry validation",
        "for-ls-word-splitting": "for loops over $(ls ...) split on whitespace/newlines and miss unusual paths",
        "gnu-date-d": "date -d is GNU-specific and can fail on BSD/macOS systems",
        "chmod-recursive": "recursive chmod can weaken permissions over a large tree",
        "chown-recursive": "recursive chown can change ownership over a large tree",
        "eval-execution": "eval executes constructed code and amplifies injection risk",
        "curl-pipe-shell": "piping network content into a shell executes unaudited code",
        "sudo-required": "sudo changes privilege boundary and requires explicit approval",
        "mktemp-u": "mktemp -u is race-prone because it returns a name without creating it safely",
    }.get(issue_id, "shell safety risk detected")


def _mitigation_for(issue_id: str, *, has_dry_run: bool) -> str:
    dry = "Keep and test the existing dry-run gate." if has_dry_run else "Add a dry-run mode and make it the default."
    return {
        "rm-recursive-force": f"{dry} Validate target is non-empty and under an allowlisted root before deletion.",
        "find-delete": f"{dry} First print matched files; then require explicit confirmation or sandbox.",
        "rsync-delete": f"{dry} Use `--dry-run --itemize-changes` before any real sync.",
        "tar-unsafe-extract": "List archive entries first and reject absolute paths, `..`, unsafe links, and overwrites before extraction.",
        "for-ls-word-splitting": "Use `find -print0` with a null-delimited read loop.",
        "gnu-date-d": "Use a portable date implementation or document the GNU dependency and fail clearly.",
        "chmod-recursive": "Limit scope, avoid world-writable modes, and print changed paths first.",
        "chown-recursive": "Limit scope, avoid following untrusted links, and print changed paths first.",
        "eval-execution": "Replace eval with arrays or explicit command dispatch.",
        "curl-pipe-shell": "Download to a file, verify checksum/signature, then review before execution.",
        "sudo-required": "Require explicit approval and document why elevated privileges are necessary.",
        "mktemp-u": "Use `mktemp` to create the file/directory atomically.",
    }.get(issue_id, dry)
