"""Build the plain-text source file the external translation tool consumes."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Any, List


def chapter_source_lines(chapters: List[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    for chapter in chapters:
        title = chapter.get("name") or "Untitled Chapter"
        lines.append(f"Chapter {int(chapter['id']) + 1}: {title}")
        content = chapter.get("content") or ""
        for paragraph in content.split("\n"):
            lines.append(paragraph.rstrip())
        lines.append("")
    return lines


def build_translation_source(chapters: List[Dict[str, Any]], target: Path) -> int:
    """Write the manuscript as line-based TXT and return the exported line count.

    The external tool consumes line batches (default 10 lines per request);
    blank separator lines and pure-number lines are dropped by the tool itself.
    """
    lines = chapter_source_lines(chapters)
    while lines and not lines[-1].strip():
        lines.pop()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)