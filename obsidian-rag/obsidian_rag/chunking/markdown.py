"""Chunking inteligente de notas Markdown para RAG.

Divide notas por headers (H1/H2/H3) com fallback por tamanho.
Preserva metadata: source_path, title, section_header, display_text.
Gera hash SHA256 por chunk para controlo incremental.
Prefixo contextual no texto para embedding (melhora relevância semântica).
"""

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from obsidian_rag.config import settings
from obsidian_rag.metadata import stable_source_id


@dataclass
class Chunk:
    id: str
    text: str  # Texto com prefixo contextual (para embedding)
    metadata: dict = field(default_factory=dict)


HEADER_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)
FRONTMATTER_CAPTURE_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
LINK_ONLY_RE = re.compile(r"^[\s\-\*]*\[\[.*?\]\][\s\-\*]*$")
WIKILINK_RE = re.compile(r"\[\[([^|\]#]+)(?:#[^|\]]+)?(?:\|[^\]]+)?\]\]")
TAG_RE = re.compile(r"(?<!\w)#([A-Za-z0-9_/-]+)")
TASK_RE = re.compile(r"^\s*- \[( |x|X)\] (.+)$", re.MULTILINE)
_DATE_FRONTMATTER_KEYS = ("date", "created", "modified", "updated", "published")
TASK_RE = re.compile(r"^\s*- \[( |x|X)\] (.+)$", re.MULTILINE)
_DATE_FRONTMATTER_KEYS = ("date", "created", "modified", "updated", "published")


def _compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter (---...---) do início do texto."""
    return FRONTMATTER_RE.sub("", text).strip()


def _parse_scalar(value: str) -> str | list[str]:
    value = value.strip().strip('"').strip("'")
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [part.strip().strip('"').strip("'") for part in inner.split(",") if part.strip()]
    return value


def _parse_frontmatter(raw: str) -> dict:
    """Parse a small useful subset of YAML frontmatter without adding PyYAML."""
    data: dict[str, str | list[str]] = {}
    current_key: str | None = None
    for raw_line in raw.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")) and current_key and line.strip().startswith("- "):
            current = data.setdefault(current_key, [])
            if isinstance(current, list):
                current.append(_parse_scalar(line.strip()[2:]))
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current_key = key.strip()
        value = value.strip()
        if value:
            data[current_key] = _parse_scalar(value)
        else:
            data[current_key] = []
    return data


def _extract_frontmatter(text: str) -> tuple[dict, str]:
    match = FRONTMATTER_CAPTURE_RE.match(text)
    if not match:
        return {}, text
    return _parse_frontmatter(match.group(1)), text[match.end():].strip()


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _extract_tasks(text: str) -> tuple[list[str], list[str]]:
    """Extract open and completed tasks from a note body.

    Returns (open_tasks, done_tasks).
    """
    open_tasks: list[str] = []
    done_tasks: list[str] = []
    for m in TASK_RE.finditer(text):
        marker, content = m.group(1), m.group(2).strip()
        if marker == " ":
            open_tasks.append(content)
        else:
            done_tasks.append(content)
    return open_tasks, done_tasks


def _extract_note_date(frontmatter: dict) -> str | None:
    """Return the first date-like value found in frontmatter, as a string."""
    for key in _DATE_FRONTMATTER_KEYS:
        val = frontmatter.get(key)
        if val:
            if isinstance(val, list):
                val = val[0] if val else None
            if val:
                return str(val).strip()
    return None


def _extract_wikilinks(text: str) -> list[str]:
    links = {match.group(1).strip() for match in WIKILINK_RE.finditer(text)}
    return sorted(link for link in links if link)


def _extract_tags(text: str, frontmatter: dict) -> list[str]:
    tags = set(_as_list(frontmatter.get("tags")) + _as_list(frontmatter.get("tag")))
    tags.update(match.group(1).strip() for match in TAG_RE.finditer(text))
    return sorted(tag for tag in tags if tag)


def _is_navigation_content(text: str) -> bool:
    """True if chunk is mostly wikilinks/navigation (low value for RAG)."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return True
    link_lines = sum(1 for ln in lines if LINK_ONLY_RE.match(ln))
    return link_lines / len(lines) > 0.7


def _split_by_headers(text: str) -> list[tuple[str, str]]:
    """Divide texto em secções baseadas em headers Markdown."""
    matches = list(HEADER_RE.finditer(text))
    if not matches:
        return [("", text)]

    sections = []
    if matches[0].start() > 0:
        preamble = text[: matches[0].start()].strip()
        if preamble:
            sections.append(("", preamble))

    for i, match in enumerate(matches):
        header_title = match.group(2).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections.append((header_title, body))

    return sections


def _split_long_text(text: str, max_chars: int, overlap: int) -> list[str]:
    """Divide texto longo em chunks com overlap."""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + max_chars
        if end < len(text):
            cut = text.rfind("\n\n", start, end)
            if cut == -1 or cut <= start:
                cut = text.rfind(". ", start, end)
            if cut > start:
                end = cut + 1
        chunks.append(text[start:end].strip())
        # Ensure start always advances: if end - overlap <= start the cut
        # was too close to start, which would cause an infinite loop.
        next_start = end - overlap if end < len(text) else end
        start = next_start if next_start > start else end

    return [c for c in chunks if c]


def chunk_note(path: Path, source_dir: Path | None = None) -> list[Chunk]:
    """Divide uma nota .md em chunks semânticos com prefixo contextual."""
    if source_dir is None:
        source_dir = settings.paths.vault_dir

    cfg = settings.chunking

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []

    frontmatter, body = _extract_frontmatter(text)
    if cfg.strip_frontmatter:
        text = body.strip()
        if not text:
            return []
    else:
        text = body.strip() if frontmatter else text

    tags = _extract_tags(text, frontmatter)
    wikilinks = _extract_wikilinks(text)
    aliases = _as_list(frontmatter.get("aliases") or frontmatter.get("alias"))
    open_tasks, done_tasks = _extract_tasks(text)
    note_date = _extract_note_date(frontmatter)

    # Derive belongs_to_topic from first hierarchical tag segment or parent folder
    belongs_to_topic: str | None = None
    if tags:
        belongs_to_topic = tags[0].split("/")[0]
    elif path.parent != source_dir:
        belongs_to_topic = path.parent.name

    rel_path = str(path.relative_to(source_dir))
    source_id = stable_source_id(source_dir.name, source_dir)
    title_match = re.match(r"^#\s+(.+)$", text, re.MULTILINE)
    note_title = title_match.group(1).strip() if title_match else path.stem

    sections = _split_by_headers(text)
    chunks = []

    for header, section_text in sections:
        if _is_navigation_content(section_text):
            continue

        sub_chunks = _split_long_text(section_text, cfg.max_chars, cfg.overlap_chars)
        for i, chunk_text in enumerate(sub_chunks):
            if len(chunk_text.strip()) < cfg.min_chars:
                continue

            # Build contextual prefix for better embedding
            if cfg.contextual_prefix:
                prefix_parts = [f"Nota: {note_title}"]
                if header:
                    prefix_parts.append(f"Secção: {header}")
                prefix = " | ".join(prefix_parts)
                embedding_text = f"{prefix}\n{chunk_text}"
            else:
                embedding_text = chunk_text

            chunk_id = _compute_hash(f"v2:{source_id}:{rel_path}:{header}:{i}:{chunk_text}")
            # Per-section tasks (for sections that contain them)
            section_open, section_done = _extract_tasks(chunk_text)
            metadata = {
                "source_id": source_id,
                "source_path": rel_path,
                "source_type": "markdown",
                "source_name": source_dir.name,
                "note_title": note_title,
                "section_header": header,
                "chunk_index": i,
                "display_text": chunk_text,
                # Content hash of the raw chunk text — drives incremental dedup
                # and embedding-cache reuse independent of position.
                "content_hash": _compute_hash(chunk_text),
                "frontmatter": frontmatter,
                "tags": tags,
                "aliases": aliases,
                "wikilinks": wikilinks,
                "outlinks": wikilinks,
                # --- relations ---
                "mentions": wikilinks,           # note names referenced via [[...]]
                "links_to": wikilinks,           # same — explicit outlinks
                "has_task": bool(open_tasks or done_tasks),
                "open_tasks": section_open or open_tasks,
                "done_tasks": section_done or done_tasks,
                "belongs_to_topic": belongs_to_topic,
                # --- temporal ---
                "note_date": note_date,
            }
            chunks.append(Chunk(id=chunk_id, text=embedding_text, metadata=metadata))

    return chunks


# Directories to skip when scanning notes
_EXCLUDED_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".cache", "dist", "build", ".obsidian",
})


def iter_note_files(source_dir: Path | None = None) -> Iterator[Path]:
    """Yield .md file paths from the notes directory, filtering excluded dirs.

    Used by the bounded ingest pipeline to process files one at a time.
    """
    if source_dir is None:
        source_dir = settings.paths.vault_dir

    if not source_dir.exists():
        return

    exclude_dirs = set(_EXCLUDED_DIRS)
    try:
        exclude_dirs.update(settings.sync.exclude_patterns)
    except Exception:
        pass

    for path in sorted(source_dir.rglob("*.md")):
        if not any(part in exclude_dirs for part in path.relative_to(source_dir).parts):
            yield path
