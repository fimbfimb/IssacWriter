"""Chapter-brief refiner: turns thin outline descriptions into full structured
chapter briefs — one chapter at a time, in the context of the whole novel.

The brief follows a fixed template (Title / POV / Setting / Summary /
Scenes-Beats / Key Revelations / Emotional Core / Cliffhanger-Transition) so
the writing agent receives cinematic, director-ready chapter material instead
of a one-paragraph summary. Each chapter is refined IN CONTEXT: premise,
synopsis, acts, the full chapter list, the codex, the already-refined
preceding briefs and the next chapter's direction.

Hard storage rules (same doctrine as the manuscript refiner):

- The outline in book.json is NEVER written by this module.
- Every refined brief is committed as a NEW version file under
  ``data/<book-id>/outline_refined/chNNNN/vNNNN.json`` — re-runs append
  versions, nothing is overwritten. ``index.json`` records the active version.
- Batch checkpoints live in ``data/<book-id>/outline_refines/`` with
  hash-based resume (chapter brief + description source as hash material).
- The writer swaps descriptions in-memory ONLY when
  ``book["refined_descriptions_source"]`` is set.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ..llm.provider import LlmClient, LlmProfile
from ..translation.ensemble import (
    TranslationStopped,
    _parse_judge_decision,
    codex_glossary,
    signal_call,
    style_context,
)
from ..translation.authorial import (
    _first_line,
    _make_unload,
    _review_note,
    call_structured,
    similar_ratio,
    _role_usage,
)

BRIEF_TEMPERATURE = 0.6
POLISH_TEMPERATURE = 0.2
DEFAULT_REFINE_ROUNDS = 1
DEFAULT_SIMILARITY_STOP = 0.95
DEFAULT_CONTEXT_PREV = 3


def _briefs_dir(book_dir: Path) -> Path:
    return book_dir / "outline_refined"


def _briefs_workdir(book_dir: Path) -> Path:
    return book_dir / "outline_refines"


def existing_versions(refined_dir: Path, chapter_id: int) -> List[int]:
    directory = refined_dir / f"ch{int(chapter_id):04d}"
    if not directory.exists():
        return []
    numbers = []
    for path in directory.glob("v*.json"):
        match = re.fullmatch(r"v(\d{4})\.json", path.name)
        if match:
            numbers.append(int(match.group(1)))
    return sorted(numbers)


def load_index(refined_dir: Path) -> Dict[str, Any]:
    try:
        payload = json.loads((refined_dir / "index.json").read_text(encoding="utf-8"))
    except Exception:
        return {"active": {}}
    return payload if isinstance(payload, dict) else {"active": {}}


def active_version(refined_dir: Path, chapter_id: int) -> Optional[int]:
    numbers = existing_versions(refined_dir, chapter_id)
    if not numbers:
        return None
    index = load_index(refined_dir)
    recorded = (index.get("active") or {}).get(str(int(chapter_id)))
    if recorded is not None and int(recorded) in numbers:
        return int(recorded)
    return numbers[-1]


def read_version(refined_dir: Path, chapter_id: int, version: Optional[int] = None) -> Optional[Dict[str, Any]]:
    if version is None:
        version = active_version(refined_dir, chapter_id)
    if version is None:
        return None
    path = refined_dir / f"ch{int(chapter_id):04d}" / f"v{int(version):04d}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def set_active_version(book_dir: Path, chapter_id: int, version: int) -> Dict[str, Any]:
    refined_dir = _briefs_dir(book_dir)
    if int(version) not in existing_versions(refined_dir, chapter_id):
        raise ValueError(f"version v{int(version):04d} does not exist for outline chapter {int(chapter_id) + 1}")
    index = load_index(refined_dir)
    index.setdefault("active", {})[str(int(chapter_id))] = int(version)
    index["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (refined_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    rebuild_active_file(book_dir)
    return {"chapter_id": int(chapter_id), "version": int(version)}


def apply_refined_outline_descriptions(
    chapters: List[Dict[str, Any]], refined_dir: Path
) -> List[Dict[str, Any]]:
    """In-memory swap of chapter descriptions for the ACTIVE refined briefs;
    chapters without versions keep their original description (read-only —
    book.json is never written here)."""
    result: List[Dict[str, Any]] = []
    for chapter in chapters:
        cid = int(chapter["id"])
        version = active_version(refined_dir, cid)
        payload = read_version(refined_dir, cid, version) if version is not None else None
        text = str((payload or {}).get("description") or "")
        replaced = dict(chapter)
        if text.strip():
            replaced["description"] = text
            replaced["outline_refined_version"] = int(version or 0)
        result.append(replaced)
    return result


def rebuild_active_file(book_dir: Path) -> Optional[str]:
    """Reassemble outline_refined/book_outline_refined.md from ACTIVE versions."""
    refined_dir = _briefs_dir(book_dir)
    if not refined_dir.exists():
        return None
    parts: List[str] = []
    for directory in sorted(refined_dir.glob("ch*")):
        match = re.fullmatch(r"ch(\d+)", directory.name)
        if not directory.is_dir() or not match:
            continue
        payload = read_version(refined_dir, int(match.group(1)), None)
        if payload and str(payload.get("description") or "").strip():
            parts.append(
                f"# Kapitel {int(match.group(1)) + 1}: {payload.get('chapter_name') or 'Untitled'}\n\n"
                f"{str(payload['description']).strip()}"
            )
    if not parts:
        return None
    refined_dir.mkdir(parents=True, exist_ok=True)
    target = refined_dir / "book_outline_refined.md"
    target.write_text("\n\n\n".join(parts).rstrip() + "\n", encoding="utf-8")
    return str(target)


def reset_outline_refine_all(book_dir: Path) -> List[str]:
    removed: List[str] = []
    for directory in (_briefs_workdir(book_dir), _briefs_dir(book_dir)):
        if directory.is_dir():
            shutil.rmtree(directory)
            removed.append(str(directory.name))
    return removed


def _chapter_list_json(chapters: List[Dict[str, Any]]) -> str:
    compact = [
        {"id": ch.get("id"), "name": ch.get("name"), "word_count": ch.get("word_count"), "description": ch.get("description")}
        for ch in chapters
    ]
    return json.dumps(compact, ensure_ascii=False, indent=2)


def outline_judge_messages(
    source_description: str,
    candidates: List[Dict[str, Any]],
    language: str,
    book: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Judge prompt for candidate chapter briefs (ensemble of refiners)."""
    system_parts = [
        "You are a demanding story editor.",
        f"Several refined chapter briefs of ONE outline chapter were produced by different models. The brief language is {language}.",
        "Pick the candidate with the best combination of:",
        "1) fidelity — every fact of the original chapter description is present; nothing missing, nothing invented",
        "2) usefulness for a novelist — clear POV, concrete setting, actionable Scenes/Beats, a real cliffhanger",
        "3) prose quality — no filler, no placeholders, no meta commentary, consistent tone",
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
        {"role": "user", "content": f"--- ORIGINAL CHAPTER DESCRIPTION ---\n{source_description}\n\n{numbered}"},
    ]


def _beats_for(word_count: int) -> int:
    return max(5, min(14, round(int(word_count or 0) / 600)))


def brief_messages(
    book: Dict[str, Any],
    chapters: List[Dict[str, Any]],
    index: int,
    language: str,
    previous_briefs: List[str],
    current_text: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Refine/pass-1 + polish/pass-2+ prompt for ONE chapter brief."""
    chapter = chapters[index]
    cid = int(chapter["id"])
    beats = _beats_for(chapter.get("word_count") or 0)
    outline = book.get("outline") or {}
    context_parts: List[str] = []
    for key, label in (("premise", "Premise"), ("synopsis", "Detailed synopsis"), ("acts", "Acts")):
        value = (outline.get(key) or "").strip()
        if value:
            context_parts.append(f"## {label}\n{value}")
    context_parts.append(f"## Full chapter list of the novel\n{_chapter_list_json(chapters)}")

    user_parts: List[str] = []
    user_parts.append("\n\n".join(context_parts))
    if previous_briefs:
        user_parts.append(
            "## Already refined preceding chapters (tone + continuity, newest last)\n"
            + "\n\n---\n\n".join(previous_briefs)
        )
    next_chapter = chapters[index + 1] if index + 1 < len(chapters) else None
    if next_chapter is not None:
        user_parts.append(
            f"## Next chapter (the chapter's hook should point toward it)\n{json.dumps({'id': next_chapter.get('id'), 'name': next_chapter.get('name'), 'description': next_chapter.get('description')}, ensure_ascii=False)}"
        )
    glossary = codex_glossary(book)
    if glossary:
        user_parts.append(glossary)
    style = style_context(book)
    if style:
        user_parts.append(style)
    user_parts.append(
        "## Chapter to refine\n"
        + json.dumps(
            {"id": cid, "name": chapter.get("name"), "word_count": chapter.get("word_count"),
             "description": chapter.get("description")},
            ensure_ascii=False, indent=2,
        )
    )
    template = (
        "Gib den Kapitelbrief GENAU in dieser Struktur aus, jede Sektion als eigener Block:\n\n"
        "Title: <Kapiteltitel>\n"
        "POV: <Perspektivfigur> (third person limited, past tense)\n"
        "Setting: <3-6 Sätze: Ort, Atmosphäre, sensorische Details>\n"
        "Summary: <2-3 Absätze, der volle Kapitelbogen>\n"
        "Scenes/Beats:\n"
        f"1. <Beat-Name>: <2-5 Sätze>\n2. …  (etwa {beats} Beats für dieses Kapitel)\n"
        "Key Revelations:\n- <konkrete Offenbarung/Fakt aus diesem Kapitel>\n"
        "Emotional Core: <2-4 Sätze>\n"
        "Cliffhanger/Transition: <3-5 Sätze>\n"
    )
    if current_text is None:
        task = (
            "Aufgabe: Verfeinere DIESE eine Kapitelbeschreibung zum vollständigen Kapitelbrief\n"
            f"in {language} — im Kontext des ganzen Romans (oben) und der bereits verfeinerten\n"
            "Kapitel. Regeln:\n"
            f"{template}\n"
            "- Bewahre JEDE Faktaus der Originalbeschreibung: Ereignisse, Reihenfolge, Figurenaktionen,\n"
            "  Namen des Codex. Nichts darf wegfallen.\n"
            "- Erfinde KEINE neuen Wendungen, Offenbarungen oder Figuren. Konkrete sensorische Details\n"
            "  sind erwünscht, solange sie die Handlung nicht verändern.\n"
            "- POV: Die Perspektivfigur ergibt sich aus dem Kapitelinhalt (Codex/Outline); bei Zweifel\n"
            "  die Hauptfigur. Benenne die Figur, nicht 'the narrator'. Erzählduell des Romans ist\n"
            "  third person limited, past tense, sofern die Outline nichts anderes legt.\n"
            "- Der Cliffhanger zielt auf das nächste Kapitel (Name oben); gibt es kein nächstes,\n"
            "  zielt er auf die Auflösung.\n"
            "- Title: der VORGEGEBENE Kapitelname bleibt ('" + str(chapter.get("name") or "") + "').\n"
            "- Abgabe: Rufe das Tool `translation_output` auf — {\"text\": <Kapitelbrief>, \"notes\":\n"
            "  [<maximal 2 kurze Änderungsnotizen, DEUTSCH>]}. Kein Denken im text-Feld. Ohne Tool-\n"
            "  Support: NUR ein JSON-Objekt {\"text\": \"...\", \"notes\": []}. Keine Analyse außerhalb."
        )
    else:
        task = (
            "Aufgabe: Überarbeite diesen Kapitelbrief — Struktur und Fakten bleiben, schärke Beats,\n"
            "Straffe Sätze, mache Setting/Emotion konkreter. Der Originalbeschreibung und dem Codex\n"
            f"bleibst du treu. Alles in {language}.\n\n"
            f"{template}\n"
            "- Abgabe: Tool `translation_output` mit {\"text\": <überarbeiteter Brief>, \"notes\":\n"
            "  [<maximal 2 kurze DEUTSCHE Notiz>]}; JSON-/Prosa-Fallback wie oben."
        )
    user_parts.append("## Aufgabe\n" + task)
    if current_text is not None:
        user_parts.append(f"## Aktuelle Fassung des Kapitelbriefs\n{current_text}")
    return [
        {
            "role": "system",
            "content": "You are a story architect who turns plot summaries into cinematic, director-ready chapter briefs for novelists.",
        },
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


class OutlineBriefWriter:
    """Checkpoint writer for the outline-brief refinement."""

    def __init__(self, outdir: Path, refined_dir: Path):
        self.outdir = outdir
        self.refined_dir = refined_dir
        self.report_path = outdir / "outline_refine_report.json"
        self.finished: List[Dict[str, Any]] = []
        self._chapters: Dict[int, Dict[str, Any]] = {}

    def set_chapters(self, chapters: List[Dict[str, Any]]) -> None:
        self._chapters = {int(c["id"]): dict(c) for c in chapters}

    def chapter_progress(self) -> List[Dict[str, Any]]:
        progress: Dict[int, Dict[str, Any]] = {}
        for cid, chapter in self._chapters.items():
            versions = existing_versions(self.refined_dir, cid)
            progress[cid] = {
                "chapter_id": cid,
                "chapter_name": chapter.get("name") or "Untitled",
                "word_count": int(chapter.get("word_count") or 0),
                "brief_words": 0,
                "started": False,
                "done": False,
                "review_notes": 0,
                "versions": versions,
                "active_version": active_version(self.refined_dir, cid),
            }
        for entry in self.finished:
            cid = int(entry.get("chapter_id") or 0)
            row = progress.setdefault(
                cid,
                {
                    "chapter_id": cid,
                    "chapter_name": entry.get("chapter_name") or "Untitled",
                    "word_count": int(entry.get("word_count") or 0),
                    "brief_words": 0,
                    "started": True,
                    "done": False,
                    "review_notes": 0,
                    "versions": existing_versions(self.refined_dir, cid),
                    "active_version": active_version(self.refined_dir, cid),
                },
            )
            if entry.get("stage") == "done":
                row["brief_words"] = len((entry.get("brief") or "").split())
                row["done"] = True
            row["review_notes"] += len(
                [flag for flag in entry.get("review") or [] if flag.get("type") == "annotation"]
            )
        return [progress[key] for key in sorted(progress)]

    def set_entry(self, index: int, entry: Dict[str, Any]) -> None:
        if index == len(self.finished):
            self.finished.append(entry)
        else:
            self.finished[index] = entry

    def commit_version(self, entry: Dict[str, Any], meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        cid = int(entry["chapter_id"])
        text = (entry.get("brief") or "").strip()
        if not text:
            return None
        numbers = existing_versions(self.refined_dir, cid)
        for number in numbers:
            payload = read_version(self.refined_dir, cid, number)
            if payload is not None and payload.get("run_started_at") == meta.get("started_at") and payload.get("description") == text:
                return {"chapter_id": cid, "version": number, "duplicate": True}
        version = (numbers[-1] + 1) if numbers else 1
        payload = {
            "version": version,
            "chapter_id": cid,
            "chapter_name": entry.get("chapter_name") or "Untitled",
            "description": text,
            "words": len(text.split()),
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run_started_at": meta.get("started_at"),
            "source_hash": entry.get("source_hash"),
            "models": {
                "profile": (meta.get("profile") or {}).get("name"),
                "model": (meta.get("profile") or {}).get("model"),
                "winner": entry.get("winner"),
            },
        }
        target = self.refined_dir / f"ch{cid:04d}" / f"v{version:04d}.json"
        if target.exists():
            raise RuntimeError(f"refusing to overwrite outline version: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        index = load_index(self.refined_dir)
        index.setdefault("active", {})[str(cid)] = version
        index["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (self.refined_dir / "index.json").write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            rebuild_active_file(self.refined_dir.parent)
        except Exception:
            pass
        return {"chapter_id": cid, "version": version, "words": payload["words"]}

    def as_report(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        report = dict(meta)
        report["chapters"] = self.chapter_progress()
        report["batches"] = self.finished
        return report

    def persist(self, meta: Dict[str, Any]) -> None:
        self.report_path.write_text(
            json.dumps(self.as_report(meta), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def load_report(path: Path) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None


def _role_usage_any(usage: Any) -> Dict[str, Any]:
    return usage.to_dict() if usage else {}


async def run_outline_refinement(
    *,
    book: Dict[str, Any],
    profile: Dict[str, Any],
    language: str,
    unload_after_call: bool,
    refine_rounds: int,
    similarity_stop: float,
    context_prev: int,
    style_probe: str = "",
    outdir: Path,
    refined_dir: Path,
    resume: bool,
    structured: bool = True,
    candidates: Optional[List[Dict[str, Any]]] = None,
    judge: Optional[Dict[str, Any]] = None,
    parallel_candidates: bool = False,
    emit: Callable[[Dict[str, Any]], Awaitable[None]],
    should_stop: Callable[[], bool],
    client_factory: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> Dict[str, Any]:
    """Refine chapter briefs one chapter at a time; commit each as a new outline
    version. The outline in book.json is consumed read-only.

    Optional candidate ensemble (wizard): every candidate profile produces the
    brief, a judge (if configured) picks the winner, and the polish rounds run
    on the winner's profile. Without candidates the call behaves exactly like
    before (single profile)."""
    started = time.time()
    outdir.mkdir(parents=True, exist_ok=True)
    refined_dir.mkdir(parents=True, exist_ok=True)
    outline = book.get("outline") or {}
    chapters = sorted(outline.get("chapters") or [], key=lambda c: int(c["id"]))
    chapters = [
        {**c, "id": int(c["id"]), "word_count": int(c.get("word_count") or 0)}
        for c in chapters
        if str(c.get("description") or "").strip()
    ]
    profile_name = str(profile.get("name") or profile.get("model") or "refiner")
    refine_rounds = max(1, int(refine_rounds))
    similarity_stop = min(1.0, max(0.0, float(similarity_stop)))
    candidates = [dict(c) for c in (candidates or []) if isinstance(c, dict) and c]
    ensemble_active = len(candidates) >= 2
    judge_name = (
        str((judge or {}).get("name") or (judge or {}).get("model") or "Judge")
        if judge
        else None
    )

    meta: Dict[str, Any] = {
        "mode": "outline_refine",
        "language": language,
        "profile": {"name": profile_name, "model": profile.get("model")},
        "settings": {
            "refine_rounds": refine_rounds,
            "similarity_stop": similarity_stop,
            "context_prev": int(context_prev),
        },
        "batches_total": len(chapters),
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

    writer = OutlineBriefWriter(outdir, refined_dir)
    writer.set_chapters(chapters)

    if resume:
        saved = OutlineBriefWriter.load_report(writer.report_path)
        reuse = 0
        if saved:
            by_index: Dict[int, Dict[str, Any]] = {}
            for saved_entry in saved.get("batches") or []:
                index_raw = saved_entry.get("index")
                if isinstance(index_raw, int) and 0 <= index_raw < len(chapters):
                    by_index[index_raw] = saved_entry
            for index, chapter in enumerate(chapters):
                current_hash = hashlib.sha256(
                    (str(chapter.get("description") or "") + "|" + str(chapter.get("word_count"))).encode("utf-8")
                ).hexdigest()[:16]
                saved_entry = by_index.get(index)
                if saved_entry is not None and saved_entry.get("source_hash") == current_hash:
                    saved_entry["index"] = index
                    writer.set_entry(index, saved_entry)
                    reuse += 1
                else:
                    writer.set_entry(
                        index,
                        {
                            "index": index,
                            "chapter_id": int(chapter["id"]),
                            "chapter_name": chapter.get("name") or "Untitled",
                            "word_count": int(chapter.get("word_count") or 0),
                            "source_hash": current_hash,
                            "original_description": chapter.get("description"),
                            "stage": "brief",
                            "brief": "",
                            "notes": [],
                            "review": [],
                            "rounds": 0,
                            "stopped_early": False,
                            "errors": [],
                        },
                    )
        if reuse:
            await emit(
                {
                    "type": "outline_refine_batch",
                    "batch_index": reuse,
                    "batches_total": len(chapters),
                    "note": f"resumed — {reuse} chapter briefs reused",
                }
            )
        if saved is None or not reuse:
            resume = False

    def _client(p: Dict[str, Any]):
        if client_factory is not None:
            return client_factory(p)
        return LlmClient(LlmProfile(**p))

    client = _client(profile)
    candidate_clients = [_client(c) for c in candidates] if ensemble_active else []
    judge_client = _client(judge) if (judge and ensemble_active) else None
    _unload = _make_unload(unload_after_call)
    try:
        for index, chapter in enumerate(chapters):
            if should_stop():
                writer.persist({**meta, "interrupted": True})
                raise TranslationStopped()
            cid = int(chapter["id"])
            current_hash = hashlib.sha256(
                (str(chapter.get("description") or "") + "|" + str(chapter.get("word_count"))).encode("utf-8")
            ).hexdigest()[:16]
            entry = writer.finished[index] if index < len(writer.finished) else None
            if entry is None or entry.get("source_hash") != current_hash:
                entry = {
                    "index": index,
                    "chapter_id": cid,
                    "chapter_name": chapter.get("name") or "Untitled",
                    "word_count": int(chapter.get("word_count") or 0),
                    "source_hash": current_hash,
                    "original_description": chapter.get("description"),
                    "stage": "brief",
                    "brief": "",
                    "notes": [],
                    "review": [],
                    "rounds": 0,
                    "stopped_early": False,
                    "errors": [],
                }

            # already done in a previous run → skip
            if entry.get("stage") == "done":
                writer.set_entry(index, entry)
                continue

            # ---- Pass 1: Brief erstellen ---------------------------------
            polish_profile = profile
            polish_client = client
            polish_name = profile_name
            if entry.get("stage") == "brief" and not (entry.get("brief") or "").strip():
                detail = f"Kapitel {cid + 1} · Brief erstellen"
                previous_briefs = [
                    (writer.finished[i].get("brief") or "")
                    for i in range(0 if int(context_prev) <= 0 else max(0, index - int(context_prev)), index)
                    if i < len(writer.finished) and (writer.finished[i].get("brief") or "").strip()
                ]
                if ensemble_active:
                    produced: List[Dict[str, Any]] = []
                    cand_errors: List[str] = []

                    async def _run_candidate(
                        cand_index: int, cand_profile: Dict[str, Any], cand_client: Any, do_unload: bool
                    ) -> Optional[Dict[str, Any]]:
                        cand_name = str(
                            cand_profile.get("name") or cand_profile.get("model") or f"Kandidat {cand_index + 1}"
                        )
                        cand_detail = f"Kapitel {cid + 1} · Brief erstellen (Kandidat {cand_index + 1}/{len(candidates)})"
                        try:
                            cand_text, cand_notes, cand_usage = await signal_call(
                                emit,
                                cand_name,
                                cand_detail,
                                lambda: call_structured(
                                    cand_client,
                                    brief_messages(book, chapters, index, language, previous_briefs),
                                    BRIEF_TEMPERATURE,
                                    structured=structured,
                                ),
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
                        results = await asyncio.gather(
                            *[
                                _run_candidate(c_index, cand, candidate_clients[c_index], False)
                                for c_index, cand in enumerate(candidates)
                            ],
                            return_exceptions=True,
                        )
                        for cand, result in zip(candidates, results):
                            if isinstance(result, Exception):
                                cand_errors.append(f"{cand.get('name') or cand.get('model')}: {result}")
                            elif result is not None:
                                produced.append(result)
                        for cand in candidates:
                            await _unload(cand)
                    else:
                        for c_index, cand in enumerate(candidates):
                            if should_stop():
                                writer.persist({**meta, "interrupted": True})
                                raise TranslationStopped()
                            result = await _run_candidate(c_index, cand, candidate_clients[c_index], True)
                            if result is not None:
                                produced.append(result)

                    if not produced:
                        entry["errors"].append(
                            "brief: " + ("; ".join(cand_errors) or "all candidate briefs failed")
                        )
                        writer.set_entry(index, entry)
                        writer.persist(meta)
                        await emit(
                            {
                                "type": "outline_refine_batch",
                                "batch_index": index + 1,
                                "batches_total": len(chapters),
                                "chapter_id": cid,
                                "error": "; ".join(cand_errors) or "all candidate briefs failed",
                            }
                        )
                        continue

                    decision = {"winner": 1, "reason": ""}
                    if judge_client is not None and len(produced) >= 2:
                        if should_stop():
                            writer.persist({**meta, "interrupted": True})
                            raise TranslationStopped()
                        try:
                            j_response = await signal_call(
                                emit,
                                judge_name or "Judge",
                                f"Kapitel {cid + 1} · Brief-Judge",
                                lambda: judge_client.chat(
                                    outline_judge_messages(
                                        chapter.get("description") or "", produced, language, book
                                    ),
                                    temperature=0.2,
                                    max_output_tokens=1200,
                                ),
                            )
                            decision = _parse_judge_decision(j_response.content or "", len(produced))
                            _accumulate(judge_name or "Judge", _role_usage(j_response.usage))
                        except Exception as exc:
                            decision = _parse_judge_decision("", len(produced))
                            decision["reason"] = f"judge failed ({exc}); defaulted"
                        await _unload(judge or {})
                    winner = produced[max(1, int(decision["winner"])) - 1]
                    entry["brief"] = winner["text"]
                    entry["rounds"] = 1
                    for note in winner["notes"]:
                        entry["notes"].append(note)
                        _review_note(entry, "annotation", None, note)
                    entry["candidates"] = [p["name"] for p in produced]
                    entry["winner"] = winner["name"]
                    entry["judge_reason"] = str(decision.get("reason") or "")
                    entry["candidate_errors"] = list(cand_errors)
                    entry["stage"] = "briefed"
                    polish_profile = winner["profile"]
                    polish_client = winner["client"]
                    polish_name = winner["name"]
                    writer.set_entry(index, entry)
                    writer.persist(meta)
                    await emit(
                        {
                            "type": "wizard_candidates",
                            "unit": f"brief ch{cid + 1}",
                            "candidates": entry["candidates"],
                            "winner": winner["name"],
                            "reason": str(decision.get("reason") or ""),
                        }
                    )
                    await emit(
                        {
                            "type": "outline_refine_output",
                            "lines": [f"[{winner['name']}] {_first_line(winner['text'])}"],
                        }
                    )

                else:
                    try:
                        text, notes, usage = await signal_call(
                            emit,
                            profile_name,
                            detail,
                            lambda: call_structured(
                                client,
                                brief_messages(book, chapters, index, language, previous_briefs),
                                BRIEF_TEMPERATURE,
                                structured=structured,
                            ),
                        )
                        _accumulate(profile_name, _role_usage(usage))
                        await _unload(profile)
                    except Exception as exc:
                        await _unload(profile)
                        entry["errors"].append(f"brief: {exc}")
                        writer.set_entry(index, entry)
                        writer.persist(meta)
                        await emit(
                            {
                                "type": "outline_refine_batch",
                                "batch_index": index + 1,
                                "batches_total": len(chapters),
                                "chapter_id": cid,
                                "error": str(exc),
                            }
                        )
                        continue
                    entry["brief"] = text
                    entry["rounds"] = 1
                    for note in notes:
                        entry["notes"].append(note)
                        _review_note(entry, "annotation", None, note)
                    entry["stage"] = "briefed"
                    writer.set_entry(index, entry)
                    writer.persist(meta)
                    await emit(
                        {
                            "type": "outline_refine_output",
                            "lines": [f"[{profile_name}] {_first_line(text)}"],
                        }
                    )

            # ---- Pass 2..N: Brief verfeinern ------------------------------
            for round_no in range(2, refine_rounds + 1):
                if entry.get("stage") != "briefed":
                    break
                if should_stop():
                    writer.persist({**meta, "interrupted": True})
                    raise TranslationStopped()
                detail = f"Kapitel {cid + 1} · Brief verfeinern (Runde {round_no}/{refine_rounds})"
                previous_briefs = [
                    (writer.finished[i].get("brief") or "")
                    for i in range(0 if int(context_prev) <= 0 else max(0, index - int(context_prev)), index)
                    if i < len(writer.finished) and (writer.finished[i].get("brief") or "").strip()
                ]
                try:
                    text, notes, usage = await signal_call(
                        emit,
                        polish_name,
                        detail,
                        lambda: call_structured(
                            polish_client,
                            brief_messages(book, chapters, index, language, previous_briefs, current_text=entry["brief"]),
                            POLISH_TEMPERATURE,
                            structured=structured,
                        ),
                    )
                    _accumulate(polish_name, _role_usage(usage))
                    await _unload(polish_profile)
                except Exception as exc:
                    await _unload(polish_profile)
                    entry["errors"].append(f"polish round {round_no}: {exc}")
                    break
                ratio = similar_ratio(entry["brief"], text)
                entry["brief"] = text
                entry["rounds"] = round_no
                entry["stopped_early"] = ratio >= similarity_stop
                for note in notes:
                    entry["notes"].append(note)
                    _review_note(entry, "annotation", None, note)
                writer.set_entry(index, entry)
                writer.persist(meta)
                if ratio >= similarity_stop:
                    break

            entry["stage"] = "done"
            writer.set_entry(index, entry)
            writer.persist(meta)
            committed = writer.commit_version(entry, meta)
            await emit(
                {
                    "type": "outline_refine_batch",
                    "batch_index": index + 1,
                    "batches_total": len(chapters),
                    "chapter_id": cid,
                    "stage": "done",
                    "words": len((entry.get("brief") or "").split()),
                    "rounds": entry.get("rounds"),
                }
            )
            if committed and not committed.get("duplicate"):
                await emit(
                    {
                        "type": "outline_refine_chapter",
                        "chapter_id": cid,
                        "version": committed["version"],
                        "words": committed.get("words") or 0,
                        "note": f"chapter brief committed as version v{committed['version']:04d}",
                    }
                )

        writer.persist(
            {
                **meta,
                "interrupted": False,
                "finished_at": time.time(),
                "duration_s": round(time.time() - started, 1),
            }
        )
        gaps = [entry for entry in writer.finished if entry.get("stage") != "done"]
        return {
            "report_file": str(writer.report_path),
            "refined_dir": str(refined_dir),
            "batches_total": len(chapters),
            "batches_done": len([e for e in writer.finished if e.get("stage") == "done"]),
            "gaps": len(gaps),
            "totals": totals,
            "review_notes": sum(len(e.get("notes") or []) for e in writer.finished),
        }
    finally:
        for open_client in [client, *candidate_clients, judge_client]:
            close = getattr(open_client, "aclose", None)
            if close is not None:
                await close()