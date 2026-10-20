"""Chapter collector for the finished TRANSLATED book (EPUB export).

Priority of sources, first match wins:
1. ``german_refined/`` — ACTIVE prose versions of the German refine
   (wizard step 6 / manual German refine). The actual translated book.
2. ``authorial_report.json`` + ``chapters_de/`` — the plain finished
   translation (authorial mode).
3. ``ensemble_report.json`` — winner texts per block (ensemble mode).

Every resolver returns chapter rows ``{"id", "name", "content"}`` with the
heading OUT of the content, so the EPUB builder applies its own heading.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..refine import german_refine
from ..refine.engine import read_version
from ..export.cleanup import cleanup_paragraphs

_ISO_CODES = {
    "german": "de", "deutsch": "de",
    "english": "en",
    "spanish": "es", "espanol": "es",
    "french": "fr", "francais": "fr",
    "italian": "it", "italiano": "it",
    "portuguese": "pt", "portugues": "pt",
    "dutch": "nl",
    "russian": "ru",
    "japanese": "ja",
    "chinese": "zh",
}

# Kapitel/Chapter heading, also as Markdown line ("# ", "## …") — the
# chapters_de files carry their heading sometimes with marker prefixes.
# Ordinal forms ("Erstes Kapitel: …") leak from the translator prose too, and
# refiners wrap them in markdown bold ("**Erstes Kapitel: …**").
_HEADING = re.compile(
    r"^\s*(?:\*{1,2}\s*)?(?:#{1,6}\s*)?"
    r"(?:(?:Kapitel|Chapter)\s+\d+|(?:Erst|Zweit|Dritt|Viert|F[üu]nft|Sechs|Siebt|Acht|Neunt|Zehnt)e[rsn]?\s+Kapitel)\s*[:.]?\s*"
    r"(?P<title>.*?)\s*\*{0,2}\s*$",
    re.IGNORECASE,
)


def _strip_heading(line: str) -> str:
    match = _HEADING.match((line or "").strip())
    return match.group("title").strip() if match else ""


def _heading_match(line: str):
    """Heading-shaped line (any form above) — used to purge heading rows from
    the body even when they carry no recoverable title."""
    return _HEADING.match((line or "").strip())


def language_code(language: str) -> str:
    lowered = (language or "").strip().lower()
    if lowered in _ISO_CODES:
        return _ISO_CODES[lowered]
    return lowered[:2] or "en"


def collect_german_chapters(book_dir: Path, language: str) -> Tuple[List[Dict[str, Any]], str]:
    """(chapters, source) for translation EPUB export — empty list = nothing."""
    slug = german_refine.slug_dir(book_dir, language)
    refined = refined_chapters(slug)
    if refined:
        return _clean_headings(refined), "german_refined"
    authorial = authorial_chapters(slug)
    if authorial:
        return _clean_headings(authorial), "authorial"
    ensemble = ensemble_chapters(slug)
    if ensemble:
        return _clean_headings(ensemble), "ensemble"
    return [], ""


# ------------------------------------------------------------------ #
# source 1: german_refined active versions
# ------------------------------------------------------------------ #


def refined_chapters(slug_dir: Path) -> List[Dict[str, Any]]:
    directory = german_refine.refined_dir(slug_dir)
    if not directory.exists():
        return []
    chapters: List[Dict[str, Any]] = []
    for chapter_dir in sorted(directory.glob("ch*")):
        match = re.fullmatch(r"ch(\d+)", chapter_dir.name)
        if not chapter_dir.is_dir() or not match:
            continue
        cid = int(match.group(1))
        payload = read_version(directory, cid, None) or {}
        content = str(payload.get("content") or "").strip()
        if not content:
            continue
        heading = str(payload.get("heading") or "").strip()
        name = str(payload.get("chapter_name") or "").strip()
        if not name:
            stripped = _strip_heading(heading) or _strip_heading(content.splitlines()[0] if content else "")
            name = stripped or f"Kapitel {cid + 1}"
        chapters.append({"id": cid, "name": name, "content": content})
    return chapters


# ------------------------------------------------------------------ #
# source 2: authorial report / chapters_de files
# ------------------------------------------------------------------ #


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def authorial_chapters(slug_dir: Path) -> List[Dict[str, Any]]:
    report = _read_json(slug_dir / "authorial_report.json")
    chapters, _ids = german_refine.german_chapters_from_report(report, slug_dir / "chapters_de")
    return [
        {"id": int(row["id"]), "name": str(row.get("name") or ""), "content": str(row.get("content") or "")}
        for row in chapters
        if str(row.get("content") or "").strip()
    ]


# ------------------------------------------------------------------ #
# source 3: ensemble winner texts
# ------------------------------------------------------------------ #


def ensemble_chapters(slug_dir: Path) -> List[Dict[str, Any]]:
    report = _read_json(slug_dir / "ensemble_report.json")
    batches = (report or {}).get("batches") or []
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for entry in batches:
        if not isinstance(entry, dict) or entry.get("chapter_id") is None:
            continue
        grouped.setdefault(int(entry["chapter_id"]), []).append(entry)
    chapters: List[Dict[str, Any]] = []
    for cid, rows in sorted(grouped.items()):
        rows.sort(key=lambda e: int(e.get("index") or 0))
        texts = [str(row.get("winner_text") or "").strip() for row in rows]
        body = "\n\n".join(text for text in texts if text)
        if not body.strip():
            continue
        heading = next((str(row.get("heading") or "").strip() for row in rows if row.get("heading")), "")
        name = _strip_heading(heading) or _strip_heading(body) or ""
        if not name:
            name = str(rows[0].get("chapter_name") or f"Kapitel {cid + 1}")
        chapters.append({"id": cid, "name": name, "content": _without_leading_heading(body)})
    return chapters


def _strip_heading(line: str) -> str:
    match = _HEADING.match((line or "").strip())
    return match.group(1).strip() if match else ""


def _without_leading_heading(body: str) -> str:
    lines = body.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and _strip_heading(lines[0]):
        lines.pop(0)
        return "\n".join(lines).strip()
    return body.strip()


def _clean_headings(chapters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Final normalization for ANY source: a translated (Kapitel-)heading that
    leaked INTO the content (e.g. `# Kapitel 1: …` from a chapters_de file) is
    moved out of the body — and it REPLACES the recorded chapter name, which is
    often the source-language name (the report carries the English outline
    name). The EPUB builder prefixes `Kapitel N: ` itself."""
    out: List[Dict[str, Any]] = []
    for row in chapters:
        content = str(row.get("content") or "")
        lines = [line for line in content.splitlines()]
        while lines and not lines[0].strip():
            lines.pop(0)
        # translation batches repeat the chapter heading INSIDE their prose
        # (sometimes 2-3x, ordinal and markdown-bold forms included: "**Erstes
        # Kapitel: …**") — every heading-shaped line dissolves; the first one
        # names the h2
        translated = ""
        body_lines: List[str] = []
        for line in lines:
            match = _heading_match(line)
            if match:
                title = match.group("title").strip()
                if title and not translated:
                    translated = title
                continue
            body_lines.append(line)
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        content = "\n".join(body_lines)
        raw = str(row.get("name") or "").strip()
        plain = _strip_heading(raw)
        if plain:
            name = plain  # "Kapitel 7: …"/"Erstes Kapitel: …"/"# …" — stripped
        elif raw.startswith("#"):
            name = re.sub(r"^\s*#{1,6}\s*", "", raw)
        else:
            name = translated or raw
        out.append({"id": int(row["id"]), "name": name, "content": content})
    return out


def align_illustrations_for_translation(layout: Dict[str, Any], chapters: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Illustration anchors sit on ENGLISH paragraph indices; a translation can
    have a different paragraph count. Clamp the anchor onto the translated
    chapter's paragraph range (best effort) and only keep entries whose chapter
    exists in the translation — build_epub still warns about anything else."""
    counts = {
        int(chapter["id"]): len(cleanup_paragraphs(str(chapter.get("content") or "")))
        for chapter in chapters
    }
    kept: List[Dict[str, Any]] = []
    for entry in (layout or {}).get("illustrations") or []:
        cid = entry.get("chapter_id")
        if not isinstance(cid, int) or cid not in counts:
            continue
        index = entry.get("paragraph_index")
        index = index if isinstance(index, int) and index >= 0 else 0
        limit = max(0, counts[cid] - 1)
        kept.append({**entry, "paragraph_index": min(index, limit)})
    return {**layout, "illustrations": kept}