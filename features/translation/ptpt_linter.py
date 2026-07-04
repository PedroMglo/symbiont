"""European Portuguese linter."""

from __future__ import annotations

import re
import time
from pathlib import Path

from models import LintChange, LintPTPTResponse
from protected_spans import protect_text, restore_text


_DEFAULT_PTBR_BLOCKLIST = {
    "arquivo": "ficheiro",
    "tela": "ecrã",
    "usuário": "utilizador",
    "usuario": "utilizador",
    "celular": "telemóvel",
    "ônibus": "autocarro",
    "onibus": "autocarro",
    "aplicativo": "aplicação",
    "baixar": "descarregar",
    "deletar": "apagar",
    "time": "equipa",
    "cadastro": "registo",
}


def load_blocklist(path: str | Path) -> dict[str, str]:
    mapping = dict(_DEFAULT_PTBR_BLOCKLIST)
    p = Path(path)
    if not p.exists():
        return mapping
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().strip("'\"")
        value = value.strip().strip("'\"")
        if key and value:
            mapping[key] = value
    return mapping


class PTPTLinter:
    def __init__(self, blocklist_path: str | Path):
        self.blocklist = load_blocklist(blocklist_path)

    def lint(self, text: str, *, protect_spans_enabled: bool = True) -> LintPTPTResponse:
        start = time.perf_counter()
        working, spans = protect_text(text) if protect_spans_enabled else (text, [])
        changes: list[LintChange] = []
        for source, target in sorted(self.blocklist.items(), key=lambda item: len(item[0]), reverse=True):
            pattern = re.compile(rf"(?<![\w-]){re.escape(source)}(?![\w-])", re.IGNORECASE)

            def replace(match: re.Match[str]) -> str:
                matched = match.group(0)
                replacement = _match_case(matched, target)
                changes.append(LintChange(**{"from": matched, "to": replacement, "reason": "pt-BR to pt-PT"}))
                return replacement

            working = pattern.sub(replace, working)
        corrected = restore_text(working, spans) if spans else working
        return LintPTPTResponse(
            original=text,
            corrected=corrected,
            changes=changes,
            latency_ms=(time.perf_counter() - start) * 1000,
        )


def _match_case(source: str, target: str) -> str:
    if source.isupper():
        return target.upper()
    if source[:1].isupper():
        return target[:1].upper() + target[1:]
    return target
