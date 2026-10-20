"""English prose refiner: draft → refine → check on the original manuscript.

Same three-stage pipeline as the authorial translation
(`backend/translation/authorial.py`), but the target language is the book's
own language (usually English): weak source prose is rewritten into stronger
prose instead of being translated.

Hard storage rules (this feature exists because chapter text was lost once):

- The original chapters under ``data/<book-id>/chapters/`` are NEVER written.
  The refiner opens them read-only.
- Finished chapters are committed as NEW version files under
  ``data/<book-id>/refined/chNNNN/vNNNN.json``. An existing version file is
  never rewritten — re-running a refinement appends ``v0002, v0003, …``.
  ``refined/index.json`` records which version is active per chapter.
- ``refined/book_refined.txt`` and ``refined/manuscript_refined.md`` are
  regenerable assemblies of the ACTIVE versions (rebuild_active_files).
- Batch/stage checkpoints live in ``data/<book-id>/refines/refine_report.json``
  with hash-based resume, mirroring the authorial translation report. Per
  chapter preview files are refreshed after every persist
  (``refines/chapters_refined/chNNNN.md``) so partial progress survives even a
  corrupted report.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ..llm.provider import LlmClient, LlmProfile
from ..translation.authorial import (
    CHECK_MODES,
    CHECK_MODE_ESSENTIAL,
    REFINE_TEMPERATURE,
    _first_line,
    _make_unload,
    _review_note,
    _MAX_REVIEW_LINES_EMITTED,
    build_word_batches,
    call_structured,
    char_ratio,
    check_messages,
    fidelity_flag,
    is_placeholder_prose,
    length_flag,
    parse_fidelity,
    similar_ratio,
    split_paragraphs,
    _role_usage,
)
from ..translation.ensemble import (
    TranslationStopped,
    _parse_judge_decision,
    codex_glossary,
    signal_call,
    style_context,
    unload_ollama_profile,
)

DRAFT_TEMPERATURE = 0.7
DEFAULT_REFINE_ROUNDS = 1
DEFAULT_SIMILARITY_STOP = 0.97
DEFAULT_RATIO_MIN = 0.75
DEFAULT_RATIO_MAX = 1.5
DEFAULT_MIN_WORDS = 250

STRUCTURED_TOOL_NAME = "translation_output"


def draft_messages(
    source_block: str,
    language: str,
    book: Dict[str, Any],
    previous_paragraph: Optional[str],
    style_probe: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Stage 1 prompt: free rewrite of the block in stronger prose (same language)."""
    system_parts = [
        "You are a literary author rewriting prose with a craftsman's eye.",
        f"Your goal: a text in {language} that keeps every fact but reads better than the original.",
    ]
    user_parts: List[str] = []
    glossary = codex_glossary(book)
    if glossary:
        user_parts.append(glossary)
    style = style_context(book)
    if style:
        user_parts.append(style)
    probe = (style_probe or "").strip()
    if probe:
        user_parts.append(f"Style probe (target tone, do not copy content):\n{probe}")
    if previous_paragraph:
        user_parts.append(
            f"Previous paragraph (keep tone and terminology):\n{previous_paragraph}"
        )
    user_parts.append(f"Original passage:\n{source_block}")
    user_parts.append(
        "Rules:\n"
        "- Rewrite the passage in stronger prose — same language, not a translation.\n"
        "- Preserve the full plot: every event, its order, facts, figures, object names. Omit nothing,\n"
        "  invent nothing, add nothing.\n"
        "- Fix weak sentences: pinched constructions break into two or three clean sentences;\n"
        "  participial chains become finite verbs; tangled clauses are untangled.\n"
        "- Unclear or awkward source passages are resolved to the most plausible reading and reported\n"
        "  under ANMERKUNGEN.\n"
        "- Characters' distinct voices stay distinct. Person and place names stay as they are.\n"
        "- Output exactly one paragraph per input paragraph, same order. Lines of the form\n"
        "  \"Chapter N: Title\" are chapter headings: render them as headings.\n"
        "- Abgabe des Ergebnisses: Rufe das Tool `translation_output` auf — {\"text\": <der komplette\n"
        "  umgeschriebene Text>, \"notes\": [<bewusste Änderungen, je eine kurze Zeile>]}.\n"
        "  notes bleibt leer, wenn nichts zu melden ist. Kein Denken im text-Feld. Unterstützt dein\n"
        "  Endpunkt keine Tool-Calls, antworte NUR mit einem JSON-Objekt {\"text\": \"...\", \"notes\": [...]}."
    )
    return [
        {"role": "system", "content": "\n".join(system_parts)},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def refine_messages(
    source_paragraph: str,
    previous_paragraph: Optional[str],
    current_text: str,
    language: str,
    book: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Stage 2 prompt: polish the current version with the source still visible."""
    user_parts: List[str] = []
    glossary = codex_glossary(book)
    if glossary:
        user_parts.append(glossary)
    if previous_paragraph:
        user_parts.append(f"Previous paragraph:\n{previous_paragraph}")
    user_parts.append(f"Original passage (for reference):\n{source_paragraph}")
    user_parts.append(f"Current version in {language}:\n{current_text}")
    user_parts.append(
        f"Aufgabe: Diese Fassung soll als Prosa in {language} besser klingen — Handlung, Reihenfolge,\n"
        "Figuren und Fakten bleiben exakt, nichts weglassen, nichts dazuerfinden:\n"
        "- Schwach gebaute Sätze zerlegst du oder straffst sie; Partizipialketten werden zu finiten\n"
        "  Verben; schief angeschlossene Sätze werden klar angeschlossen.\n"
        "- Klar und präzise schlägt geschwätzig. Wiederholte Aussagen fallen zusammen, wenn kein\n"
        "  neuer Inhalt bleibt.\n"
        "- Figurenstimmen bleiben unterscheidbar. Eigennamen unverändert.\n"
        "- Abgabe: Rufe das Tool `translation_output` auf — {\"text\": <neue Fassung>, \"notes\":\n"
        "  [<maximal eine kurze Änderungsnotiz>]}. Kein Denken im text-Feld; keine Tool-Calls möglich,\n"
        "  dann antworte NUR mit einem JSON-Objekt {\"text\": \"...\", \"notes\": [...]}. Keine Analyse,\n"
        "  keine Wiederholung der Fassung."
    )
    return [
        {
            "role": "system",
            "content": "You are an experienced literary editor with a fine sense of language.",
        },
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def refine_judge_messages(
    source_block: str,
    candidates: List[Dict[str, Any]],
    language: str,
    book: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Judge prompt for candidate rewrites of one block (wizard refine step)."""
    system_parts = [
        "You are a demanding literary editor.",
        f"Several candidate rewrites of one source passage were produced by different models. The rewrite language is {language}.",
        "Pick the candidate with the best combination of:",
        "1) faithful meaning — no omissions, no additions, nothing skipped",
        "2) strong, natural literary prose — reject stiff, machine-like or placeholder output",
        "3) narrative voice consistent with the style notes",
        "4) structure preserved (similar paragraph count and order)",
        'Respond ONLY with JSON exactly like {"winner": <candidate number>, "reason": "<one short sentence>"}.',
    ]
    style = style_context(book)
    if style:
        system_parts.append(style)
    glossary = codex_glossary(book)
    if glossary:
        system_parts.append(glossary)
    numbered = "\n\n".join(
        f"--- CANDIDATE {i + 1} ({c['name']}) ---\n{c['text']}" for i, c in enumerate(candidates)
    )
    return [
        {"role": "system", "content": "\n".join(system_parts)},
        {"role": "user", "content": f"--- SOURCE BLOCK ---\n{source_block}\n\n{numbered}"},
    ]


def _refine_check_messages(source_paragraph: str, refined: str) -> List[Dict[str, Any]]:
    """Strict-mode fidelity check: compare content, not style."""
    return [
        {
            "role": "system",
            "content": "You check content fidelity only, not style. Output ONLY JSON.",
        },
        {
            "role": "user",
            "content": (
                f"Original passage:\n{source_paragraph}\n\n"
                f"Refined passage:\n{refined}\n\n"
                "The refined version is a deliberate prose improvement — smoother sentences are\n"
                "intended, not a loss. Check only real content: is an event, fact or plot detail\n"
                "missing? Was anything invented? Did the plot or the central statement change?\n"
                'JSON: {"ok": true/false, "verloren": [], "erfunden": [], "verschoben": []}'
            ),
        },
    ]


def _version_dir(refined_dir: Path, chapter_id: int) -> Path:
    return refined_dir / f"ch{int(chapter_id):04d}"


def _version_path(refined_dir: Path, chapter_id: int, version: int) -> Path:
    return _version_dir(refined_dir, chapter_id) / f"v{int(version):04d}.json"


def existing_versions(refined_dir: Path, chapter_id: int) -> List[int]:
    directory = _version_dir(refined_dir, chapter_id)
    if not directory.exists():
        return []
    numbers = []
    for path in directory.glob("v*.json"):
        match = re.fullmatch(r"v(\d{4})\.json", path.name)
        if match:
            numbers.append(int(match.group(1)))
    return sorted(numbers)


def load_index(refined_dir: Path) -> Dict[str, Any]:
    path = refined_dir / "index.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"active": {}}
    return payload if isinstance(payload, dict) else {"active": {}}


def active_version(refined_dir: Path, chapter_id: int) -> Optional[int]:
    """Recorded active version, falling back to the newest existing file."""
    numbers = existing_versions(refined_dir, chapter_id)
    if not numbers:
        return None
    index = load_index(refined_dir)
    recorded = (index.get("active") or {}).get(str(int(chapter_id)))
    if recorded is not None and int(recorded) in numbers:
        return int(recorded)
    return numbers[-1]


def set_active_version(book_dir: Path, chapter_id: int, version: int) -> Dict[str, Any]:
    """Point the chapter's active pointer at an existing version and rebuild
    the derived book files. Never touches original chapters/."""
    refined_dir = book_dir / "refined"
    numbers = existing_versions(refined_dir, chapter_id)
    if int(version) not in numbers:
        raise ValueError(
            f"version v{int(version):04d} does not exist for chapter {int(chapter_id) + 1}"
        )
    index = load_index(refined_dir)
    index.setdefault("active", {})[str(int(chapter_id))] = int(version)
    index["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    index_path = refined_dir / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    rebuild_active_files(book_dir)
    return {"chapter_id": int(chapter_id), "version": int(version)}


def read_version(refined_dir: Path, chapter_id: int, version: Optional[int]) -> Optional[Dict[str, Any]]:
    if version is None:
        version = active_version(refined_dir, chapter_id)
    if version is None:
        return None
    path = _version_path(refined_dir, chapter_id, version)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def chapter_text_from_version(payload: Dict[str, Any]) -> str:
    return str(payload.get("content") or "")


def rebuild_active_files(book_dir: Path) -> Optional[str]:
    """Reassemble refined/book_refined.txt + manuscript_refined.md from the
    ACTIVE version of every committed chapter. Derived-only files (the version
    JSONs are the source of truth); returns the assembled txt path or None."""
    refined_dir = book_dir / "refined"
    if not refined_dir.exists():
        return None
    chapters: List[Tuple[int, Dict[str, Any]]] = []
    for directory in sorted(refined_dir.glob("ch*")):
        match = re.fullmatch(r"ch(\d+)", directory.name)
        if not directory.is_dir() or not match:
            continue
        payload = read_version(refined_dir, int(match.group(1)), None)
        if payload and str(payload.get("content") or "").strip():
            chapters.append((int(match.group(1)), payload))
    if not chapters:
        return None
    text_parts: List[str] = []
    md_parts: List[str] = []
    for _cid, payload in chapters:
        name = payload.get("chapter_name") or f"Chapter {int(payload.get('chapter_id') or 0) + 1}"
        heading = payload.get("heading") or f"Chapter {int(payload.get('chapter_id') or 0) + 1}: {name}"
        text_parts.extend([heading, "", str(payload.get("content") or "").strip(), ""])
        md_parts.extend([f"## {heading}", "", str(payload.get("content") or "").strip(), ""])
    refined_dir.mkdir(parents=True, exist_ok=True)
    (refined_dir / "book_refined.txt").write_text("\n".join(text_parts).rstrip() + "\n", encoding="utf-8")
    (refined_dir / "manuscript_refined.md").write_text("\n".join(md_parts).rstrip() + "\n", encoding="utf-8")
    return str(refined_dir / "book_refined.txt")


class RefineWriter:
    """Collects per-batch stage results; writes report, reviews, previews and
    versioned chapter commits. Mirrors AuthorialWriter, minus translation."""

    def __init__(self, outdir: Path, refined_dir: Path):
        self.outdir = outdir
        self.refined_dir = refined_dir
        self.current_path = outdir / "book_refined_current.txt"
        self.review_path = outdir / "review_list.md"
        self.manuscript_path = outdir / "manuscript_refined.md"
        self.report_path = outdir / "refine_report.json"
        self.finished: List[Dict[str, Any]] = []
        self._chapters: Dict[int, Dict[str, Any]] = {}
        self._committed: set = set()

    def set_chapters(self, chapters: List[Dict[str, Any]]) -> None:
        self._chapters = {
            int(c["id"]): {
                "chapter_id": int(c["id"]),
                "chapter_name": c.get("name") or "Untitled",
                "words_total": int(c.get("words") or 0),
            }
            for c in chapters
        }

    def chapter_progress(self) -> List[Dict[str, Any]]:
        progress: Dict[int, Dict[str, Any]] = {}
        for chapter_id, info in self._chapters.items():
            progress[chapter_id] = {
                "chapter_id": chapter_id,
                "chapter_name": info["chapter_name"],
                "words_total": info.get("words_total") or 0,
                "refined_words": 0,
                "batches_total": 0,
                "batches_done": 0,
                "paragraphs": 0,
                "review_flags": 0,
                "started": False,
                "done": False,
                "versions": existing_versions(self.refined_dir, chapter_id),
                "active_version": active_version(self.refined_dir, chapter_id),
            }
        for entry in self.finished:
            chapter_id = int(entry.get("chapter_id") or 0)
            row = progress.setdefault(
                chapter_id,
                {
                    "chapter_id": chapter_id,
                    "chapter_name": entry.get("chapter_name") or "Untitled",
                    "words_total": 0,
                    "refined_words": 0,
                    "batches_total": 0,
                    "batches_done": 0,
                    "paragraphs": 0,
                    "review_flags": 0,
                    "started": False,
                    "done": False,
                    "versions": existing_versions(self.refined_dir, chapter_id),
                    "active_version": active_version(self.refined_dir, chapter_id),
                },
            )
            row["batches_total"] += 1
            row["started"] = True
            if entry.get("stage") in ("done", "refined"):
                for item in entry.get("items") or []:
                    row["refined_words"] += len((item.get("text") or "").split())
                    row["paragraphs"] += 1 if item.get("text") else 0
                if entry.get("stage") == "done":
                    row["batches_done"] += 1
            row["review_flags"] += len(
                [f for f in entry.get("review") or [] if f.get("type") != "annotation" and not f.get("decision")]
            )
            if row["batches_total"] > 0:
                row["done"] = row["batches_done"] == row["batches_total"]
        return [progress[key] for key in sorted(progress)]

    def set_entry(self, index: int, entry: Dict[str, Any]) -> None:
        if index == len(self.finished):
            self.finished.append(entry)
        else:
            self.finished[index] = entry

    def last_paragraph(self) -> Optional[str]:
        for entry in reversed(self.finished):
            for item in reversed(entry.get("items") or []):
                text = (item.get("text") or "").strip()
                if text:
                    return text
        return None

    def source_for(self, batch: Dict[str, Any]) -> str:
        lines: List[str] = []
        if batch.get("header"):
            lines.append(batch["header"])
        lines.extend(batch["paragraphs"])
        return "\n".join(lines)

    def refined_text(self) -> str:
        blocks: List[str] = []
        for entry in self.finished:
            lines: List[str] = []
            texts = [(item.get("text") or "") for item in (entry.get("items") or [])]
            if texts and any(t.strip() for t in texts):
                if entry.get("heading"):
                    lines.append(entry["heading"])
                lines.extend(t for t in texts if t.strip())
            if lines:
                blocks.append("\n\n".join(lines))
        return "\n\n".join(blocks).rstrip() + ("\n" if blocks else "")

    def manuscript_text(self) -> str:
        blocks: List[str] = []
        for entry in self.finished:
            texts = [(item.get("text") or "") for item in (entry.get("items") or [])]
            if not any(t.strip() for t in texts):
                continue
            heading = entry.get("heading") or entry.get("chapter_name") or ""
            if heading:
                blocks.append(f"## {heading}")
            blocks.append("\n\n".join(t for t in texts if t.strip()))
        return "\n\n".join(blocks).rstrip() + ("\n" if blocks else "")

    def review_entries(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for entry in self.finished:
            for flag in entry.get("review") or []:
                rows.append({**flag, "chapter_id": entry.get("chapter_id"), "batch_index": entry.get("index")})
        return rows

    def review_text(self, book_title: str) -> str:
        rows = self.review_entries()
        lines = [
            f"# Review list — refined prose — {book_title}",
            "",
            f"Generated: {time.strftime('%Y-%m-%d %H:%M')} · {len(rows)} entries "
            f"(flags require a human decision; the system suggests, it does not know).",
            "",
        ]
        if not rows:
            lines.append("No flags — nothing to review.")
            return "\n".join(lines) + "\n"
        current_chapter: Optional[int] = None
        for row in rows:
            chapter_id = row.get("chapter_id")
            if chapter_id != current_chapter:
                current_chapter = chapter_id
                lines.append(f"## Chapter {int(chapter_id) + 1 if chapter_id is not None else '?'}")
            kind = row.get("type") or "flag"
            label = "note" if kind == "annotation" else kind
            paragraph = row.get("paragraph")
            where = f" ¶{paragraph + 1}" if isinstance(paragraph, int) else ""
            decision = row.get("decision") or {}
            status = f" — {decision.get('action', '')}" if decision.get("action") else ""
            lines.append(f"- [{label}{where}] {row.get('detail') or ''}{status}".rstrip())
        return "\n".join(lines) + "\n"

    def _chapter_entries(self) -> Dict[int, List[Dict[str, Any]]]:
        by_chapter: Dict[int, List[Dict[str, Any]]] = {}
        for entry in self.finished:
            cid = entry.get("chapter_id")
            if cid is None:
                continue
            by_chapter.setdefault(int(cid), []).append(entry)
        for entries in by_chapter.values():
            entries.sort(key=lambda e: int(e.get("index") or 0))
        return by_chapter

    def _chapter_state(self, chapter_id: int) -> Optional[Dict[str, Any]]:
        entries = self._chapter_entries().get(int(chapter_id))
        if not entries:
            return None
        heading = next((e.get("heading") for e in entries if e.get("heading")), None)
        parts: List[str] = []
        words = 0
        src_hashes: List[str] = []
        for entry in entries:
            if entry.get("source_hash"):
                src_hashes.append(str(entry["source_hash"]))
            if entry.get("heading"):
                heading = entry["heading"]
            for item in entry.get("items") or []:
                text = (item.get("text") or "").strip()
                if text:
                    parts.append(text)
                    words += len(text.split())
        if not parts:
            return None
        return {
            "heading": heading or f"Chapter {int(chapter_id) + 1}",
            "content": "\n\n".join(parts),
            "words": words,
            "source_hashes": src_hashes,
        }

    def commit_version(self, chapter_id: int, meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Append the refined chapter as a NEW version file. Existing version
        files are never rewritten; index.json's active pointer just moves.
        Returns the commit info or None when nothing to commit."""
        state = self._chapter_state(chapter_id)
        if not state:
            return None
        numbers = existing_versions(self.refined_dir, chapter_id)
        # idempotence: the identical content from the identical run must not
        # produce a duplicate version (resume runs call this again)
        content_hash = hash((state["content"], meta.get("started_at")))
        for number in numbers:
            payload = read_version(self.refined_dir, chapter_id, number)
            if payload is None:
                continue
            if payload.get("run_started_at") == meta.get("started_at") and payload.get("content") == state["content"]:
                return {"chapter_id": chapter_id, "version": number, "words": state["words"], "duplicate": True}
        version = (numbers[-1] + 1) if numbers else 1
        payload = {
            "version": version,
            "chapter_id": int(chapter_id),
            "chapter_name": (self._chapters.get(int(chapter_id)) or {}).get("chapter_name")
            or next((e.get("chapter_name") for e in self.finished if e.get("chapter_id") == int(chapter_id)), "Untitled"),
            "heading": state["heading"],
            "content": state["content"],
            "words": state["words"],
            "source_hashes": state["source_hashes"],
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run_started_at": meta.get("started_at"),
            "models": {
                "draft": (meta.get("draft") or {}).get("name"),
                "refiner": (meta.get("refiner") or {}).get("name"),
                "checker": (meta.get("checker") or {}).get("name") if meta.get("checker") else None,
            },
            "review_flags": sum(
                1
                for entry in self._chapter_entries().get(int(chapter_id), [])
                for flag in entry.get("review") or []
                if flag.get("type") != "annotation" and not flag.get("decision")
            ),
        }
        target = _version_path(self.refined_dir, chapter_id, version)
        if target.exists():  # safety net — must never happen
            raise RuntimeError(f"refusing to overwrite refined version: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        index = load_index(self.refined_dir)
        active = index.setdefault("active", {})
        active[str(int(chapter_id))] = version
        index["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        index_path = self.refined_dir / "index.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        rebuild_active_files(self.refined_dir.parent)
        self._committed.add((int(chapter_id), version))
        return {"chapter_id": int(chapter_id), "version": version, "words": state["words"]}

    def maybe_commit(self, batch_index: int, meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Commit the chapter right after its LAST batch reached stage done."""
        entry = self.finished[batch_index] if batch_index < len(self.finished) else None
        if not entry or entry.get("stage") != "done":
            return None
        chapter_id = int(entry.get("chapter_id") or 0)
        last_index = max(
            (int(e.get("index") or 0) for e in self.finished if e.get("chapter_id") == int(chapter_id)),
            default=-1,
        )
        if batch_index != last_index:
            return None
        for e in self.finished:
            if e.get("chapter_id") == int(chapter_id) and e.get("stage") != "done":
                return None
        return self.commit_version(chapter_id, meta)

    def write_chapter_files(self) -> None:
        """Per-chapter preview files, refreshed after EVERY persist:
        chapters_refined/chNNNN.md — finished (and partially refined) chapter
        text survives on disk regardless of what happens to the report."""
        base = self.report_path.parent
        out_dir = base / "chapters_refined"
        for cid, entries in self._chapter_entries().items():
            parts: List[str] = []
            for entry in entries:
                if entry.get("heading"):
                    parts.append(str(entry["heading"]))
                for item in entry.get("items") or []:
                    text = (item.get("text") or "").strip()
                    if text:
                        parts.append(text)
                        parts.append("")
            has_prose = any(p.strip() for p in parts)
            out_path = out_dir / f"ch{cid:04d}.md"
            if not has_prose:
                out_path.unlink(missing_ok=True)
                continue
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path.write_text("\n".join(parts).strip() + "\n", encoding="utf-8")

    def as_report(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        report = dict(meta)
        report["chapters"] = self.chapter_progress()
        report["batches"] = self.finished
        return report

    def persist(self, meta: Dict[str, Any], book_title: str = "") -> None:
        self.current_path.write_text(self.refined_text(), encoding="utf-8")
        self.review_path.write_text(self.review_text(book_title), encoding="utf-8")
        self.manuscript_path.write_text(self.manuscript_text(), encoding="utf-8")
        self.report_path.write_text(
            json.dumps(self.as_report(meta), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            self.write_chapter_files()
        except Exception:
            pass  # preview files must never break the refine run

    @staticmethod
    def load_report(path: Path) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None


async def run_refinement(
    *,
    chapters: List[Dict[str, Any]],
    language: str,
    draft_profile: Dict[str, Any],
    refiner_profile: Optional[Dict[str, Any]] = None,
    checker_profile: Optional[Dict[str, Any]] = None,
    unload_after_call: bool,
    refine_rounds: int,
    similarity_stop: float,
    ratio_min: float,
    ratio_max: float,
    min_words: int,
    max_words: Optional[int],
    draft_temperature: float = DRAFT_TEMPERATURE,
    style_probe: str = "",
    outdir: Path,
    refined_dir: Path,
    book: Dict[str, Any],
    resume: bool,
    structured: bool = True,
    check_mode: str = CHECK_MODE_ESSENTIAL,
    candidates: Optional[List[Dict[str, Any]]] = None,
    judge: Optional[Dict[str, Any]] = None,
    parallel_candidates: bool = False,
    parallel_blocks: int = 1,
    emit: Callable[[Dict[str, Any]], Awaitable[None]],
    should_stop: Callable[[], bool],
    client_factory: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> Dict[str, Any]:
    """Refine the given chapters; commit every finished chapter as a new
    version file under refined_dir. Chapters are consumed read-only.

    Optional candidate ensemble (wizard): every candidate profile produces the
    stage-1 rewrite of a block, a judge (if configured) picks the winner, and
    the polish rounds use the explicit refiner profile or — when absent — the
    winner's profile. Without candidates the call behaves exactly like before."""
    started = time.time()
    outdir.mkdir(parents=True, exist_ok=True)
    refined_dir.mkdir(parents=True, exist_ok=True)
    writer = RefineWriter(outdir, refined_dir)
    writer.set_chapters(
        [
            {
                "id": c["id"],
                "name": c.get("name") or "Untitled",
                "words": len((c.get("content") or "").split()),
            }
            for c in chapters
        ]
    )
    max_words = max(min_words, int(max_words or (min_words * 2)))
    batches = build_word_batches(chapters, min_words=min_words, max_words=max_words)
    refine_rounds = max(1, int(refine_rounds))
    similarity_stop = min(1.0, max(0.0, float(similarity_stop)))
    parallel_blocks = max(1, min(8, int(parallel_blocks or 1)))
    check_mode = check_mode if check_mode in CHECK_MODES else CHECK_MODE_ESSENTIAL

    draft_name = str(draft_profile.get("name") or draft_profile.get("model") or "draft")
    refiner_name = (
        str(refiner_profile.get("name") or refiner_profile.get("model") or "refiner")
        if refiner_profile
        else "refiner"
    )
    checker_name = (
        str(checker_profile.get("name") or checker_profile.get("model") or "checker")
        if checker_profile
        else None
    )
    candidates = [dict(c) for c in (candidates or []) if isinstance(c, dict) and c]
    ensemble_active = len(candidates) >= 2
    judge_name = (
        str((judge or {}).get("name") or (judge or {}).get("model") or "Judge")
        if judge
        else None
    )
    if not ensemble_active and refiner_profile is None and candidates:
        # single candidate behaves like the plain single-profile run
        draft_profile = dict(candidates[0])
        refiner_profile = dict(candidates[0])
        draft_name = str(draft_profile.get("name") or draft_profile.get("model") or "draft")
        refiner_name = draft_name
        candidates = []
        ensemble_active = False
    if not ensemble_active and refiner_profile is None:
        raise RuntimeError("refine needs a refiner profile (the strongest available model)")

    meta: Dict[str, Any] = {
        "mode": "refine",
        "language": language,
        "structured": structured,
        "check_mode": check_mode,
        "draft": {"name": draft_name, "model": draft_profile.get("model")},
        "refiner": {"name": refiner_name, "model": (refiner_profile or {}).get("model")},
        "checker": {"name": checker_name, "model": (checker_profile or {}).get("model")}
        if checker_profile
        else None,
        "settings": {
            "refine_rounds": refine_rounds,
            "similarity_stop": similarity_stop,
            "ratio_min": ratio_min,
            "ratio_max": ratio_max,
            "min_words": min_words,
            "max_words": max_words,
        },
        "batches_total": len(batches),
        "started_at": started,
        "totals": {"tokens": {}},
    }
    if ensemble_active:
        meta["candidates"] = [
            {"name": str(c.get("name") or c.get("model") or "?"), "model": c.get("model")}
            for c in candidates
        ]
        meta["judged_by"] = judge_name
        meta["parallel_candidates"] = bool(parallel_candidates)
    totals = meta["totals"]

    def _accumulate(name: str, usage: Dict[str, Any]) -> None:
        bucket = totals["tokens"].setdefault(
            name, {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        )
        bucket["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        bucket["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        bucket["cost"] += float(usage.get("cost") or 0.0)

    if resume:
        saved = RefineWriter.load_report(writer.report_path)
        reuse = 0
        if saved:
            by_index: Dict[int, Dict[str, Any]] = {}
            for saved_entry in saved.get("batches") or []:
                index_raw = saved_entry.get("index")
                if isinstance(index_raw, int) and 0 <= index_raw < len(batches):
                    by_index[index_raw] = saved_entry
            for index, batch in enumerate(batches):
                saved_entry = by_index.get(index)
                if saved_entry is not None and saved_entry.get("source_hash") == batch["source_hash"]:
                    saved_entry["source_block"] = writer.source_for(batch)
                    writer.set_entry(index, saved_entry)
                    reuse += 1
                else:
                    writer.set_entry(
                        index,
                        {
                            "index": index,
                            "chapter_id": batch["chapter_id"],
                            "chapter_name": batch["chapter_name"],
                            "source_hash": batch["source_hash"],
                            "source_block": writer.source_for(batch),
                            "source_paragraphs": batch["paragraphs"],
                            "stage": "draft",
                            "draft_error": None,
                            "draft_text": "",
                            "draft_notes": [],
                            "heading": None,
                            "segments": [],
                            "items": [],
                            "review": [],
                            "errors": [],
                        },
                    )
        if reuse:
            await emit(
                {
                    "type": "refine_batch",
                    "batch_index": reuse,
                    "batches_total": len(batches),
                    "note": f"resumed — {reuse} batches reused at their saved stage",
                }
            )
        if saved is None or not reuse:
            resume = False

    def _client(profile: Dict[str, Any]):
        if client_factory is not None:
            return client_factory(profile)
        return LlmClient(LlmProfile(**profile))

    draft_client = _client(draft_profile)
    refiner_client = _client(refiner_profile) if refiner_profile else None
    checker_client = _client(checker_profile) if checker_profile else None
    candidate_clients = [_client(c) for c in candidates] if ensemble_active else []
    judge_client = _client(judge) if (judge and ensemble_active) else None
    # polish stage role/profile — rebound per batch in candidates mode to the
    # winning candidate when no explicit refiner profile is configured
    active_refiner_profile = refiner_profile
    active_refiner_client = refiner_client
    active_refiner_name = refiner_name
    unload_between_candidates = not parallel_candidates
    # one worker per parallel block — a shared local model must not be evicted
    # mid-flight by a peer's unload, so per-call unloading only happens
    # sequentially; parallel runs unload every used profile once at the end
    _unload = _make_unload(unload_after_call and parallel_blocks <= 1)

    # tone anchor per block IN SOURCE space: the last paragraph of the
    # previous block of the same chapter. Sequential runs keep the refined
    # chain (writer.last_paragraph()); parallel runs cannot see a concurrent
    # neighbor's output, so the matching source paragraph carries the tone.
    _source_anchor: List[Optional[str]] = []
    _last_by_chapter: Dict[int, str] = {}
    for batch in batches:
        _source_anchor.append(_last_by_chapter.get(batch["chapter_id"]))
        paragraphs_ = batch["paragraphs"] or []
        if paragraphs_:
            text_ = paragraphs_[-1].strip()
            if text_:
                _last_by_chapter[batch["chapter_id"]] = text_

    async def _process_batch(index: int, previous_anchor: Optional[str], slot: int = 0) -> None:
        batch = batches[index]
        if should_stop():
            writer.persist({**meta, "interrupted": True}, book.get("title") or "")
            raise TranslationStopped()
        restored = writer.finished[index] if index < len(writer.finished) else None
        entry = restored
        stage = (entry or {}).get("stage")
        source_block = writer.source_for(batch)

        # ---- Stufe 1: Entwurf ------------------------------------------
        active_refiner_profile = refiner_profile
        active_refiner_client = refiner_client
        active_refiner_name = refiner_name
        if stage not in ("drafted", "refined", "done"):
            if ensemble_active:
                produced: List[Dict[str, Any]] = []
                cand_errors: List[str] = []

                async def _run_candidate(
                    cand_index: int, cand_profile: Dict[str, Any], cand_client: Any, do_unload: bool
                ) -> Optional[Dict[str, Any]]:
                    cand_name = str(
                        cand_profile.get("name") or cand_profile.get("model") or f"Kandidat {cand_index + 1}"
                    )
                    cand_detail = (
                        f"Kapitel {batch['chapter_id'] + 1} · Block {index + 1}/{len(batches)} "
                        f"(Entwurf Kandidat {cand_index + 1}/{len(candidates)})"
                    )
                    try:
                        cand_text, cand_notes, cand_usage = await signal_call(
                            emit,
                            cand_name,
                            cand_detail,
                            lambda: call_structured(
                                cand_client,
                                draft_messages(
                                    source_block, language, book, previous_anchor, style_probe
                                ),
                                draft_temperature,
                                structured=structured,
                            ),
                            worker=slot,
                        )
                        _accumulate(cand_name, _role_usage(cand_usage))
                        if do_unload:
                            await _unload(cand_profile)
                        return {
                            "name": cand_name,
                            "profile": cand_profile,
                            "client": cand_client,
                            "text": cand_text,
                            "notes": cand_notes,
                        }
                    except Exception as exc:
                        cand_errors.append(f"{cand_name}: {exc}")
                        if do_unload:
                            await _unload(cand_profile)
                        return None

                if parallel_candidates:
                    cand_results = await asyncio.gather(
                        *[
                            _run_candidate(c_index, cand, candidate_clients[c_index], False)
                            for c_index, cand in enumerate(candidates)
                        ],
                        return_exceptions=True,
                    )
                    for cand, result in zip(candidates, cand_results):
                        if isinstance(result, Exception):
                            cand_errors.append(f"{cand.get('name') or cand.get('model')}: {result}")
                        elif result is not None:
                            produced.append(result)
                    if unload_after_call:
                        for cand in candidates:
                            await _unload(cand)
                else:
                    for c_index, cand in enumerate(candidates):
                        if should_stop():
                            writer.persist({**meta, "interrupted": True}, book.get("title") or "")
                            raise TranslationStopped()
                        result = await _run_candidate(c_index, cand, candidate_clients[c_index], True)
                        if result is not None:
                            produced.append(result)

                if not produced:
                    entry = {
                        "index": index,
                        "chapter_id": batch["chapter_id"],
                        "chapter_name": batch["chapter_name"],
                        "source_hash": batch["source_hash"],
                        "source_block": source_block,
                        "source_paragraphs": batch["paragraphs"],
                        "stage": "draft",
                        "draft_error": "; ".join(cand_errors) or "all candidate drafts failed",
                        "draft_text": "",
                        "draft_notes": [],
                        "heading": None,
                        "items": [],
                        "review": [],
                        "errors": [
                            "draft: " + ("; ".join(cand_errors) or "all candidate drafts failed")
                        ],
                    }
                    writer.set_entry(index, entry)
                    writer.persist(meta, book.get("title") or "")
                    await emit(
                        {
                            "type": "refine_batch",
                            "batch_index": index + 1,
                            "batches_total": len(batches),
                            "chapter_id": batch["chapter_id"],
                            "stage": "draft",
                            "error": "; ".join(cand_errors) or "all candidate drafts failed",
                        }
                    )
                    return

                decision = {"winner": 1, "reason": ""}
                if judge_client is not None and len(produced) >= 2:
                    if should_stop():
                        writer.persist({**meta, "interrupted": True}, book.get("title") or "")
                        raise TranslationStopped()
                    try:
                        j_response = await signal_call(
                            emit,
                            judge_name or "Judge",
                            f"Kapitel {batch['chapter_id'] + 1} · Block {index + 1}/{len(batches)} · Rewrite-Judge",
                            lambda: judge_client.chat(
                                refine_judge_messages(source_block, produced, language, book),
                                temperature=0.2,
                                max_output_tokens=1200,
                            ),
                            worker=slot,
                        )
                        decision = _parse_judge_decision(j_response.content or "", len(produced))
                        _accumulate(judge_name or "Judge", _role_usage(j_response.usage))
                    except Exception as exc:
                        decision = _parse_judge_decision("", len(produced))
                        decision["reason"] = f"judge failed ({exc}); defaulted"
                    await _unload(judge or {})
                winner = produced[max(1, int(decision["winner"])) - 1]
                text = winner["text"]
                notes = winner["notes"]
                draft_winner_name = winner["name"]
                if refiner_profile is None:
                    active_refiner_profile = winner["profile"]
                    active_refiner_client = winner["client"]
                    active_refiner_name = winner["name"]
                await emit(
                    {
                        "type": "wizard_candidates",
                        "unit": f"block {index + 1}",
                        "chapter_id": batch["chapter_id"],
                        "candidates": [p["name"] for p in produced],
                        "winner": draft_winner_name,
                        "reason": str(decision.get("reason") or ""),
                    }
                )
            else:
                try:
                    text, notes, usage_struct = await signal_call(
                        emit,
                        draft_name,
                        f"Kapitel {batch['chapter_id'] + 1} · Block {index + 1}/{len(batches)}",
                        lambda: call_structured(
                            draft_client,
                            draft_messages(
                                source_block, language, book, previous_anchor, style_probe
                            ),
                            draft_temperature,
                            structured=structured,
                        ),
                        worker=slot,
                    )
                    _accumulate(draft_name, _role_usage(usage_struct))
                    await _unload(draft_profile)
                except Exception as exc:
                    entry = {
                        "index": index,
                        "chapter_id": batch["chapter_id"],
                        "chapter_name": batch["chapter_name"],
                        "source_hash": batch["source_hash"],
                        "source_block": source_block,
                        "source_paragraphs": batch["paragraphs"],
                        "stage": "draft",
                        "draft_error": f"{draft_name}: {exc}",
                        "draft_text": "",
                        "draft_notes": [],
                        "heading": None,
                        "items": [],
                        "review": [],
                        "errors": [f"draft: {exc}"],
                    }
                    writer.set_entry(index, entry)
                    await _unload(draft_profile)
                    writer.persist(meta, book.get("title") or "")
                    await emit(
                        {
                            "type": "refine_batch",
                            "batch_index": index + 1,
                            "batches_total": len(batches),
                            "chapter_id": batch["chapter_id"],
                            "stage": "draft",
                            "error": str(exc),
                        }
                    )
                    return

            segments = split_paragraphs(text)
            heading = None
            expected = len(batch["paragraphs"]) + (1 if batch.get("header") else 0)
            if batch.get("header") and len(segments) == expected:
                heading, segments = segments[0], segments[1:]
            entry = {
                "index": index,
                "chapter_id": batch["chapter_id"],
                "chapter_name": batch["chapter_name"],
                "source_hash": batch["source_hash"],
                "source_block": source_block,
                "source_paragraphs": batch["paragraphs"],
                "stage": "drafted",
                "draft_error": None,
                "draft_text": text,
                "draft_notes": notes,
                "heading": heading,
                "segments": segments,
                "items": [],
                "review": [
                    {"type": "annotation", "paragraph": None, "detail": note}
                    for note in notes
                ],
                "errors": [],
            }
            if ensemble_active:
                entry["draft_candidates"] = [p["name"] for p in produced]
                entry["draft_winner"] = draft_winner_name
                entry["draft_judge_reason"] = str(decision.get("reason") or "")
                entry["draft_candidate_errors"] = list(cand_errors)
            writer.set_entry(index, entry)
            writer.persist(meta, book.get("title") or "")
            stage = "drafted"
            await emit(
                {
                    "type": "refine_batch",
                    "batch_index": index + 1,
                    "batches_total": len(batches),
                    "chapter_id": batch["chapter_id"],
                    "stage": "draft",
                    "notes": len(notes),
                }
            )
            await emit(
                {
                    "type": "refine_output",
                    "lines": [f"[{draft_winner_name if ensemble_active else draft_name}] {_first_line(text)}"],
                }
            )

        # ---- Stufe 2: Verfeinern ---------------------------------------
        if entry["stage"] == "drafted":
            source_paragraphs: List[str] = entry.get("source_paragraphs") or []
            segments: List[str] = (
                entry.get("segments")
                or split_paragraphs(entry.get("draft_text") or "")
                if not entry.get("items")
                else [i.get("text") or "" for i in entry.get("items")]
            )
            aligned = len(segments) == len(source_paragraphs)
            if aligned:
                pairs = list(zip(source_paragraphs, segments))
            else:
                _review_note(
                    entry,
                    "structure",
                    None,
                    f"paragraph count deviates (source {len(source_paragraphs)}, "
                    f"draft {len(segments)}) — block refined as one text",
                )
                pairs = [(entry.get("source_block") or "", " ".join(segments))]
            items: List[Dict[str, Any]] = []
            previous = previous_anchor
            for p_index, (para_source, para_text) in enumerate(pairs):
                if should_stop():
                    # partial items must NOT become a "refined" stage — the
                    # batch stays draft-backed so a resume re-runs the whole
                    # block and no half-paragraph chapter can be committed
                    entry["items"] = []
                    entry["stage"] = "drafted"
                    writer.persist({**meta, "interrupted": True}, book.get("title") or "")
                    raise TranslationStopped()
                item: Dict[str, Any] = {
                    "index": p_index,
                    "source": para_source,
                    "text": para_text,
                    "rounds": 0,
                    "notes": [],
                    "error": None,
                    "kept_draft": False,
                    "stopped_early": False,
                    "length_ratio": None,
                    "fidelity": None,
                }
                # POLISH-GATE: the draft carried this paragraph over
                # unchanged (same-language ratio ≈ 1) and flagged nothing —
                # the draft model already treated it as final, so the
                # polish round would be a no-op. Skip it.
                gate_ratio = similar_ratio(para_text, para_source)
                if not entry.get("draft_notes") and gate_ratio >= similarity_stop:
                    item["kept_draft"] = True
                    item["stopped_early"] = True
                    item["length_ratio"] = gate_ratio
                    previous = item["text"]
                    items.append(item)
                    entry["items"] = items
                    writer.persist(meta, book.get("title") or "")
                    await emit(
                        {
                            "type": "refine_progress",
                            "batch_index": index + 1,
                            "batches_total": len(batches),
                            "chapter_id": batch["chapter_id"],
                            "paragraph": p_index + 1,
                            "paragraphs_total": len(pairs),
                            "rounds": 0,
                            "stopped_early": True,
                        }
                    )
                    continue
                for round_no in range(1, refine_rounds + 1):
                    try:
                        refined, note_lines, usage_struct = await signal_call(
                            emit,
                            active_refiner_name,
                            f"Kapitel {batch['chapter_id'] + 1} · ¶{p_index + 1}/{len(pairs)} · Runde {round_no}/{refine_rounds}",
                            lambda: call_structured(
                                active_refiner_client,
                                refine_messages(
                                    para_source, previous, item["text"], language, book
                                ),
                                REFINE_TEMPERATURE,
                                structured=structured,
                            ),
                            worker=slot,
                        )
                        _accumulate(active_refiner_name, _role_usage(usage_struct))
                        await _unload(active_refiner_profile)

                    except Exception as exc:
                        await _unload(active_refiner_profile)
                        item["error"] = str(exc)
                        item["kept_draft"] = True
                        _review_note(
                            entry,
                            "refiner",
                            p_index,
                            f"refinement failed at round {round_no} ({exc}) — draft kept",
                        )
                        break
                    ratio = similar_ratio(item["text"], refined)
                    item["text"] = refined
                    item["rounds"] = round_no
                    for note in note_lines:
                        item["notes"].append(note)
                        _review_note(entry, "annotation", p_index, note, echo_against=refined)
                    if ratio >= similarity_stop:
                        item["stopped_early"] = True
                        break
                previous = item["text"]
                items.append(item)
                entry["items"] = items
                writer.persist(meta, book.get("title") or "")
                await emit(
                    {
                        "type": "refine_progress",
                        "batch_index": index + 1,
                        "batches_total": len(batches),
                        "chapter_id": batch["chapter_id"],
                        "paragraph": p_index + 1,
                        "paragraphs_total": len(pairs),
                        "rounds": item["rounds"],
                        "stopped_early": bool(item["stopped_early"]),
                    }
                )
            entry["stage"] = "refined"
            writer.persist(meta, book.get("title") or "")
            await emit(
                {
                    "type": "refine_batch",
                    "batch_index": index + 1,
                    "batches_total": len(batches),
                    "chapter_id": batch["chapter_id"],
                    "stage": "refine",
                    "paragraphs": len(items),
                }
            )

            # ---- Stufe 3: Prüfen + Kapitel-Commit ---------------------------
            if entry["stage"] == "refined":
                for item in entry.get("items") or []:
                    src_text = item.get("source") or ""
                    refined_text = item.get("text") or ""
                    ratio = char_ratio(refined_text, src_text)
                    item["length_ratio"] = ratio
                    if check_mode == CHECK_MODE_ESSENTIAL:
                        if not refined_text.strip() or is_placeholder_prose(refined_text):
                            _review_note(
                                entry, "fidelity", item.get("index"),
                                "refined text missing (not written or placeholder)",
                            )
                        continue
                    flag = length_flag(ratio, ratio_min, ratio_max)
                    if flag:
                        _review_note(entry, "length", item.get("index"), flag)
                    if checker_profile:
                        verdict: Dict[str, Any]
                        if should_stop():
                            writer.persist({**meta, "interrupted": True}, book.get("title") or "")
                            raise TranslationStopped()
                        try:
                            response = await signal_call(
                                emit,
                                checker_name or "checker",
                                f"Kapitel {batch['chapter_id'] + 1} · ¶{(item.get('index') or 0) + 1}/{len(entry.get('items') or [])} · Fidelity-Check",
                                lambda: checker_client.chat(
                                    _refine_check_messages(src_text, refined_text),
                                    temperature=0.0,
                                ),
                                worker=slot,
                            )
                            verdict = parse_fidelity(response.content or "")
                            _accumulate(checker_name or "checker", _role_usage(response.usage))
                            await _unload(checker_profile)
                        except Exception as exc:
                            await _unload(checker_profile)
                            verdict = {"error": f"{checker_name or 'checker'}: {exc}"}
                            _review_note(
                                entry,
                                "check",
                                item.get("index"),
                                f"fidelity check not evaluable: {exc}",
                            )
                        item["fidelity"] = verdict
                        flag = fidelity_flag(verdict)
                        if flag:
                            _review_note(entry, "fidelity", item.get("index"), flag)
                entry["stage"] = "done"
                writer.persist(meta, book.get("title") or "")
                flag_rows = [
                    row for row in entry.get("review") or [] if row.get("type") != "annotation"
                ]
                await emit(
                    {
                        "type": "refine_batch",
                        "batch_index": index + 1,
                        "batches_total": len(batches),
                        "chapter_id": batch["chapter_id"],
                        "stage": "check",
                        "flags": len(flag_rows),
                        "review_total": len(entry.get("review") or []),
                    }
                )
                if flag_rows:
                    lines = [
                        f"[Prüfung] {row.get('type')}: {row.get('detail')}"
                        for row in flag_rows[:_MAX_REVIEW_LINES_EMITTED]
                    ]
                    await emit({"type": "refine_output", "lines": lines})
                # chapter commit works across workers: whichever parallel block
                # completes the chapter first sweeps it
                for committed in _commit_sweep():
                    if committed and not committed.get("duplicate"):
                        await emit(
                            {
                                "type": "refine_chapter",
                                "chapter_id": committed["chapter_id"],
                                "version": committed["version"],
                                "words": committed.get("words") or 0,
                                "note": f"chapter committed as version v{committed['version']:04d}",
                            }
                        )

    def _commit_sweep() -> List[Dict[str, Any]]:
        """Commit every chapter whose blocks are ALL done — re-checked per
        chapter against the known batch list (NOT just the entries currently
        in self.finished: a parallel neighbor's entry may not exist yet, and
        committing at that point produced half-finished versions). Commit
        itself is idempotent per run."""
        rows: List[Dict[str, Any]] = []
        chapter_indices: Dict[int, List[int]] = {}
        for batch_index, batch_ in enumerate(batches):
            chapter_indices.setdefault(batch_["chapter_id"], []).append(batch_index)
        for chapter_id, indices in sorted(chapter_indices.items()):
            all_done = all(
                int(index) < len(writer.finished)
                and (writer.finished[int(index)] or {}).get("stage") == "done"
                for index in indices
            )
            if not all_done:
                continue
            committed = writer.commit_version(chapter_id, meta)
            if committed:
                rows.append(committed)
        return rows

    try:
        if parallel_blocks > 1:
            # WORKERS TAKE WHOLE CHAPTERS: one worker runs all blocks of one
            # chapter sequentially (identical tone-chain quality to the serial
            # run), the other workers work other chapters in parallel.
            chapter_groups: Dict[int, List[int]] = {}
            for index, batch_ in enumerate(batches):
                chapter_groups.setdefault(batch_["chapter_id"], []).append(index)
            groups = [chapter_groups[cid] for cid in sorted(chapter_groups)]

            # refined tone tail per chapter: block N's anchor = block N-1's
            # refined last paragraph; first block falls back to the source
            # anchor, then the global refined tail (serial-equivalent)
            queue: asyncio.Queue = asyncio.Queue()
            for group_index in range(len(groups)):
                queue.put_nowait(group_index)
            worker_count = min(parallel_blocks, len(groups))
            for _ in range(worker_count):
                queue.put_nowait(None)  # clean shutdown signal
            stopped = {"flag": False}
            chapter_tails: Dict[int, Optional[str]] = {}

            async def _worker(worker_id: int) -> None:
                slot = worker_id - 1
                while True:
                    group_index = await queue.get()
                    if group_index is None:
                        queue.task_done()
                        return
                    try:
                        if stopped["flag"] or should_stop():
                            stopped["flag"] = True
                            return
                        for index in groups[group_index]:
                            cid = batches[index]["chapter_id"]
                            anchor = chapter_tails.get(cid) or _source_anchor[index]
                            await _process_batch(index, anchor, slot=slot)
                            entry = (
                                writer.finished[index]
                                if index < len(writer.finished)
                                else None
                            )
                            for item in reversed(entry.get("items") or []):
                                text = (item.get("text") or "").strip()
                                if text:
                                    chapter_tails[cid] = text
                                    break
                    except TranslationStopped:
                        stopped["flag"] = True
                    finally:
                        queue.task_done()

            block_workers = [
                asyncio.ensure_future(_worker(worker_id))
                for worker_id in range(1, worker_count + 1)
            ]
            try:
                await asyncio.gather(*block_workers)
            finally:
                for task in block_workers:
                    task.cancel()
                await asyncio.gather(*block_workers, return_exceptions=True)
            if stopped["flag"]:
                writer.persist({**meta, "interrupted": True}, book.get("title") or "")
                raise TranslationStopped()
        else:
            for index in range(len(batches)):
                if should_stop():
                    writer.persist({**meta, "interrupted": True}, book.get("title") or "")
                    raise TranslationStopped()
                await _process_batch(index, writer.last_paragraph())

        writer.persist(
            {
                **meta,
                "interrupted": False,
                "finished_at": time.time(),
                "duration_s": round(time.time() - started, 1),
            },
            book.get("title") or "",
        )
        gaps = [entry for entry in writer.finished if entry.get("stage") != "done"]
        review_rows = writer.review_entries()
        rounds_total = sum(
            int(item.get("rounds") or 0)
            for entry in writer.finished
            for item in entry.get("items") or []
        )
        return {
            "output_file": str(writer.current_path),
            "manuscript_file": str(writer.manuscript_path),
            "review_file": str(writer.review_path),
            "report_file": str(writer.report_path),
            "refined_dir": str(refined_dir),
            "batches_total": len(batches),
            "batches_done": len(writer.finished),
            "gaps": len(gaps),
            "totals": totals,
            "review_flags": len([row for row in review_rows if row.get("type") != "annotation"]),
            "annotations": len([row for row in review_rows if row.get("type") == "annotation"]),
            "refine_rounds_total": rounds_total,
        }
    finally:
        if parallel_blocks > 1 and unload_after_call:
            final_unload = _make_unload(True)
            for profile in [draft_profile, refiner_profile, checker_profile, judge, *candidates]:
                if profile:
                    try:
                        await final_unload(profile)
                    except Exception:
                        pass  # the model eviction must never mask a real error
        for client in (draft_client, refiner_client, checker_client, judge_client, *candidate_clients):
            if client is not None:
                close = getattr(client, "aclose", None)
                if close is not None:
                    await close()


def reset_refine_workdir(outdir: Path, book_dir: Path) -> List[str]:
    """Delete everything produced by the refiner. NEVER touches chapters/ —
    original chapters are not refiner state. Refines-workdir is removed;
    refined/ keeps version files unless wipe_versions is True."""
    removed: List[str] = []
    if outdir.is_dir():
        shutil.rmtree(outdir)
        removed.append(str(outdir.name))
    return removed


def reset_refine_all(book_dir: Path) -> List[str]:
    """Wipe ALL refiner state: working dir + versioned refined outputs."""
    removed: List[str] = []
    working = book_dir / "refines"
    refined = book_dir / "refined"
    for directory in (working, refined):
        if directory.is_dir():
            shutil.rmtree(directory)
            removed.append(str(directory.name))
    return removed