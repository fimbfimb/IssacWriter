from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..export.cleanup import cleanup_text
from ..store.project import BookStore
from ..nstagent.tools import count_words


def _alias_pattern(name: str) -> Optional[re.Pattern]:
    name = (name or "").strip()
    if not name:
        return None
    escaped = re.escape(name)
    pattern = r"(?<![A-Za-z0-9])" + escaped + r"(?![A-Za-z0-9])"
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None


def mention_counts(text: str, patterns: List[re.Pattern]) -> int:
    if not text:
        return 0
    return sum(len(pattern.findall(text)) for pattern in patterns)


def presence_spans(counts: Dict[int, int]) -> List[Tuple[int, int]]:
    chapters = sorted(ch for ch, n in counts.items() if n > 0)
    if not chapters:
        return []
    spans: List[Tuple[int, int]] = []
    start = prev = chapters[0]
    for ch in chapters[1:]:
        if ch == prev + 1:
            prev = ch
        else:
            spans.append((start, prev))
            start = prev = ch
    spans.append((start, prev))
    return spans


def build_stats(store: BookStore, book_id: str) -> Dict[str, Any]:
    book = store.load_book(book_id)
    codex = book.get("codex") or {}
    characters = codex.get("characters") or []
    settings = codex.get("settings") or []
    chapters = store.written_chapters(book_id)
    outline = book.get("outline") or {}
    chapters_plan = outline.get("chapters") or []
    metrics = store.load_metrics(book_id)

    char_patterns = {
        entry.get("name"): [
            p
            for alias in ([entry.get("name")] + (entry.get("aliases") or []))
            if (p := _alias_pattern(alias)) is not None
        ]
        for entry in characters
        if entry.get("name")
    }
    setting_patterns = {
        entry.get("name"): [
            p
            for alias in ([entry.get("name")] + (entry.get("aliases") or []))
            if (p := _alias_pattern(alias)) is not None
        ]
        for entry in settings
        if entry.get("name")
    }

    total_words = 0
    char_mentions: Dict[str, Dict[int, int]] = {name: {} for name in char_patterns}
    setting_mentions: Dict[str, Dict[int, int]] = {name: {} for name in setting_patterns}
    per_chapter: List[Dict[str, Any]] = []
    for chapter in chapters:
        ch_id = int(chapter["id"])
        words = int(chapter.get("written_words") or count_words(chapter.get("content", "")))
        total_words += words
        row: Dict[str, Any] = {
            "chapter_id": ch_id,
            "name": chapter.get("name"),
            "words": words,
            "target": chapter.get("target"),
            "chapters_total": len(chapters_plan),
        }
        char_chips = []
        for name, patterns in char_patterns.items():
            n = mention_counts(chapter.get("content", ""), patterns)
            if n:
                char_mentions[name][ch_id] = n
                char_chips.append({"name": name, "mentions": n})
        setting_chips = []
        for name, patterns in setting_patterns.items():
            n = mention_counts(chapter.get("content", ""), patterns)
            if n:
                setting_mentions[name][ch_id] = n
                setting_chips.append({"name": name, "mentions": n})
        row["characters"] = sorted(char_chips, key=lambda x: x["mentions"], reverse=True)
        row["settings"] = sorted(setting_chips, key=lambda x: x["mentions"], reverse=True)
        per_chapter.append(row)

    metrics_by_chapter: Dict[int, Dict[str, Any]] = {}
    for record in metrics:
        ch_id = int(record.get("chapter_id", -1))
        existing = metrics_by_chapter.get(ch_id)
        if existing is None or str(record.get("at", "")) >= str(existing.get("at", "")):
            metrics_by_chapter[ch_id] = record

    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
    tool_totals: Dict[str, int] = {}
    for record in metrics_by_chapter.values():
        usage = record.get("usage") or {}
        usage_totals["prompt_tokens"] += usage.get("prompt_tokens", 0)
        usage_totals["completion_tokens"] += usage.get("completion_tokens", 0)
        usage_totals["cost"] += usage.get("cost", 0.0)
        for tool, n in (record.get("tool_counts") or {}).items():
            tool_totals[tool] = tool_totals.get(tool, 0) + n

    threads_opened: Dict[str, Dict[int, int]] = {}
    threads_resolved: Dict[str, Dict[int, int]] = {}
    thread_timeline: List[Dict[str, Any]] = []
    for ch_id in sorted(metrics_by_chapter):
        info = metrics_by_chapter[ch_id].get("update_info") or {}
        for add in info.get("requirements_added") or []:
            threads_opened[add["key"]] = threads_opened.setdefault(add["key"], {})
            threads_opened[add["key"]][ch_id] = 1
        for res in info.get("requirements_resolved") or []:
            threads_resolved.setdefault(res["key"], {})[ch_id] = 1

    open_threads: List[Dict[str, Any]] = []
    for key in sorted(set(threads_opened) | set(threads_resolved)):
        opened_at = min(threads_opened.get(key, {})) if key in threads_opened else None
        resolved_at = min(threads_resolved.get(key, {})) if key in threads_resolved else None
        thread_timeline.append(
            {"key": key, "opened_at": opened_at, "resolved_at": resolved_at}
        )
        if resolved_at is None:
            open_threads.append({"key": key, "opened_at": opened_at})

    state_now = store.load_state(book_id).to_dict()
    snapshots: Dict[int, Dict[str, Any]] = {}
    history_dir = store.root / book_id / "state" / "history"
    if history_dir.exists():
        for path in sorted(history_dir.glob("ch*.json")):
            try:
                ch_id = int(path.stem[2:])
            except ValueError:
                continue
            snapshots[ch_id] = json.loads(path.read_text(encoding="utf-8"))

    tracked_timeline: Dict[str, Dict[int, int]] = {name: {} for name in char_patterns}
    for ch_id, snap in sorted(snapshots.items()):
        for entry in snap.get("character_states") or []:
            name = entry.get("name")
            if name in tracked_timeline:
                tracked_timeline[name][ch_id] = 1

    character_summary = []
    for entry in characters:
        name = entry.get("name")
        if not name:
            continue
        counts = char_mentions.get(name, {})
        spans = presence_spans(counts)
        first = min(counts) if counts else None
        last = max(counts) if counts else None
        character_summary.append(
            {
                "name": name,
                "role": entry.get("role") or "",
                "description": entry.get("description") or "",
                "color": entry.get("color") or "",
                "mentions": {str(ch): n for ch, n in sorted(counts.items())},
                "total_mentions": sum(counts.values()),
                "first_chapter": first,
                "last_chapter": last,
                "spans": [[a, b] for a, b in spans],
                "tracked_since": min(tracked_timeline.get(name, {}))
                if tracked_timeline.get(name)
                else None,
            }
        )
    character_summary.sort(key=lambda e: -e["total_mentions"])

    setting_summary = []
    for entry in settings:
        name = entry.get("name")
        if not name:
            continue
        counts = setting_mentions.get(name, {})
        spans = presence_spans(counts)
        first = min(counts) if counts else None
        last = max(counts) if counts else None
        setting_summary.append(
            {
                "name": name,
                "description": entry.get("description") or "",
                "color": entry.get("color") or "",
                "mentions": {str(ch): n for ch, n in sorted(counts.items())},
                "total_mentions": sum(counts.values()),
                "first_chapter": first,
                "last_chapter": last,
                "spans": [[a, b] for a, b in spans],
            }
        )
    setting_summary.sort(key=lambda e: -e["total_mentions"])

    target_total = int(book.get("target_words") or 0)
    cumulative = 0
    cost_running = 0.0
    progress_rows = []
    planned = {int(c.get("id")): c for c in chapters_plan}
    for row in per_chapter:
        cumulative += row["words"]
        plan = planned.get(row["chapter_id"], {})
        usage = (metrics_by_chapter.get(row["chapter_id"]) or {}).get("usage") or {}
        chapter_cost = float(usage.get("cost") or 0.0)
        cost_running += chapter_cost
        progress_rows.append(
            {
                "chapter_id": row["chapter_id"],
                "name": row["name"],
                "words": row["words"],
                "target": row.get("target", plan.get("word_count")),
                "cumulative": cumulative,
                "cost": round(chapter_cost, 6),
                "cumulative_cost": round(cost_running, 6),
                "prompt_tokens": usage.get("prompt_tokens") or 0,
                "completion_tokens": usage.get("completion_tokens") or 0,
                "tool_counts": (metrics_by_chapter.get(row["chapter_id"]) or {}).get("tool_counts") or {},
            }
        )

    return {
        "book_id": book_id,
        "title": book.get("title"),
        "target_words": target_total,
        "chapters_written": len(chapters),
        "chapters_planned": len(chapters_plan),
        "total_words": total_words,
        "coverage_words": round(total_words / target_total * 100, 1) if target_total else 0.0,
        "usage_totals": usage_totals,
        "tool_totals": tool_totals,
        "per_chapter": progress_rows,
        "characters": character_summary,
        "settings": setting_summary,
        "threads": thread_timeline,
        "open_threads": open_threads,
        "state_now": state_now,
        "snapshots": {str(ch): snapshots[ch] for ch in sorted(snapshots)},
    }


def export_markdown(store: BookStore, book_id: str) -> str:
    book = store.load_book(book_id)
    chapters = store.written_chapters(book_id)
    lines = [f"# {book.get('title', 'Untitled')}"]
    outline = book.get("outline") or {}
    if outline.get("premise"):
        lines.append("")
        lines.append(cleanup_text(outline["premise"]))
    for chapter in chapters:
        chapter_title = chapter.get("name") or f"Chapter {chapter['id']}"
        lines.append("")
        lines.append(f"## {chapter_title}")
        lines.append("")
        lines.append(cleanup_text(chapter.get("content", "")))
    return "\n".join(lines) + "\n"


def export_txt(store: BookStore, book_id: str) -> str:
    markdown = export_markdown(store, book_id)
    text = re.sub(r"^#", "", markdown, flags=re.MULTILINE)
    return text