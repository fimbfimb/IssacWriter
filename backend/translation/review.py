"""Review GUI backend: decide translation flags in-app (authorial mode only).

The review list from the authorial report becomes an API surface:
each flag row carries the English source paragraph and the German text so the
GUI can render context + decision buttons without a file download. A decision
(accepted / fixed / dismissed) is written back into the report; a fix replaces
the item text, then the AuthorialWriter re-persists all outputs (one write
path — no second exporter). Ensemble reports are read-only.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..llm.provider import LlmClient, LlmError, LlmProfile
from .authorial import (
    AuthorialWriter,
    DEFAULT_RATIO_MAX,
    DEFAULT_RATIO_MIN,
    DEFAULT_REFINE_ROUNDS,
    DEFAULT_SIMILARITY_STOP,
    DRAFT_TEMPERATURE,
    REFINE_TEMPERATURE,
    char_ratio,
    check_messages,
    clean_model_prose,
    draft_messages,
    fidelity_flag,
    is_placeholder_prose,
    length_flag,
    parse_fidelity,
    refine_messages,
    split_paragraphs,
    similar_ratio,
    _is_junk_note,
    _review_note,
)
from .ensemble import unload_ollama_profile
from .runner import _slugify as slugify_language

FLAG_TYPES = ("length", "refiner", "fidelity", "check", "structure")
ACTIONS = ("accepted", "fixed", "dismissed", "understood")


class ReviewError(Exception):
    """Domain error the API maps to a HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def slugify(value: str) -> str:
    return slugify_language(value)


def report_dir(data_root: Path, book_id: str, language: str) -> Path:
    return Path(data_root) / str(book_id) / "translations" / slugify(language)


def report_files(data_root: Path, book_id: str, language: str) -> Tuple[Optional[Path], Optional[Path]]:
    """(authorial_report.json, ensemble_report.json) if readable."""
    base = report_dir(data_root, book_id, language)
    found: List[Path] = []
    for name in ("authorial_report.json", "ensemble_report.json"):
        path = base / name
        if path.is_file():
            try:
                if json.loads(path.read_text(encoding="utf-8")):
                    found.append(path)
            except Exception:
                continue
    authorial = next((p for p in found if p.name == "authorial_report.json"), None)
    ensemble = next((p for p in found if p.name == "ensemble_report.json"), None)
    return authorial, ensemble


def _row_texts(entry: Dict[str, Any], paragraph: Optional[int]) -> Tuple[str, str]:
    """(english source, german text) for one flag."""
    items = entry.get("items") or []
    source_paragraphs = entry.get("source_paragraphs") or []
    if isinstance(paragraph, int) and 0 <= paragraph < len(items):
        item = items[paragraph]
        source = item.get("source") or ""
        if not source and paragraph < len(source_paragraphs):
            source = source_paragraphs[paragraph]
        return source, (item.get("text") or "")
    if isinstance(paragraph, int) and paragraph < len(source_paragraphs):
        source = source_paragraphs[paragraph]
        segments = entry.get("german_segments") or []
        text = segments[0] if paragraph < len(segments) else ""
        return source, text
    # structural flags / whole batch
    source = entry.get("source_block") or ""
    texts = [item.get("text") or "" for item in items]
    batch_text = "\n\n".join(t for t in texts if t.strip())
    if not batch_text:
        batch_text = "\n\n".join(s for s in (entry.get("german_segments") or []) if s.strip())
    return source, batch_text


def _flag_id(row: Dict[str, Any]) -> str:
    import hashlib

    key = f"{row.get('batch_index')}|{row.get('type')}|{row.get('paragraph')}|{row.get('detail')}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def collect_rows(report: Dict[str, Any], editable: bool) -> Dict[str, Any]:
    """Group flag rows by chapter; annotate each with texts + decision state."""
    batches = report.get("batches") or []
    grouped: Dict[int, Dict[str, Any]] = {}
    open_flags = 0
    total_flags = 0
    for entry in batches:
        if not isinstance(entry, dict):
            continue
        chapter_id = entry.get("chapter_id")
        chapter = grouped.setdefault(
            int(chapter_id or 0),
            {"chapter_id": int(chapter_id or 0), "chapter_name": entry.get("chapter_name") or "", "rows": []},
        )
        stage = entry.get("stage")
        for flag in entry.get("review") or []:
            if not isinstance(flag, dict):
                continue
            paragraph = flag.get("paragraph")
            paragraph = int(paragraph) if isinstance(paragraph, int) else None
            # annotation rows that are model self-talk or paragraph echoes
            # (older runs wrote those into reports) are hidden at read time;
            # decided rows stay visible
            if flag.get("type") == "annotation" and not flag.get("decision"):
                detail = str(flag.get("detail") or "")
                if _is_junk_note(detail):
                    continue
                _, text_preview = _row_texts(entry, paragraph)
                if text_preview and text_preview.strip() and similar_ratio(detail, text_preview) > 0.88:
                    continue
            source, text = _row_texts(entry, paragraph)
            decision = flag.get("decision")
            row = {
                "id": _flag_id({**flag, "batch_index": entry.get("index")}),
                "batch_index": entry.get("index"),
                "chapter_id": int(chapter_id or 0),
                "chapter_name": entry.get("chapter_name") or "",
                "type": flag.get("type") or "flag",
                "paragraph": paragraph,
                "detail": flag.get("detail") or "",
                "source_en": source,
                "text_de": text,
                "stage": stage,
                "decision": decision,
                "history_count": len(flag.get("decision_history") or []) + (1 if decision else 0),
                "editable": editable,
            }
            chapter["rows"].append(row)
            is_annotation = row["type"] == "annotation"
            if not is_annotation:
                total_flags += 1
                if not decision:
                    open_flags += 1
    chapters = [grouped[key] for key in sorted(grouped)]
    return {
        "mode": report.get("mode") or "authorial",
        "language": report.get("language") or "",
        "report_file": report.get("report_file"),
        "open_flags": open_flags,
        "total_flags": total_flags,
        "chapters": chapters,
    }


def build_review_response(data_root: Path, book_id: str, language: str) -> Dict[str, Any]:
    """GET payload: flags grouped by chapter, ensemble reports read-only."""
    authorial_path, ensemble_path = report_files(data_root, book_id, language)
    if authorial_path is not None:
        try:
            report = json.loads(authorial_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ReviewError(500, f"report unreadable ({exc}) — use the .bak copy in the same folder")
        payload = collect_rows(report, editable=True)
        payload["report_path"] = str(authorial_path)
        return payload
    if ensemble_path is not None:
        try:
            report = json.loads(ensemble_path.read_text(encoding="utf-8"))
        except Exception:
            report = {"mode": "ensemble"}
        payload = collect_rows({"mode": "ensemble", "batches": report.get("batches") or []}, editable=False)
        payload["report_path"] = str(ensemble_path)
        return payload
    return {
        "mode": None,
        "language": language,
        "open_flags": 0,
        "total_flags": 0,
        "chapters": [],
        "report_path": None,
        "hint": "No report for this target language yet — run an authorial translation first.",
    }


def _find_flag(
    entry: Dict[str, Any], type_: str, paragraph: Optional[int], detail: str
) -> Optional[Dict[str, Any]]:
    matches = []
    for flag in entry.get("review") or []:
        if flag.get("type") != type_ or flag.get("detail") != detail:
            continue
        row_par = flag.get("paragraph")
        row_par = int(row_par) if isinstance(row_par, int) else None
        if row_par == paragraph:
            matches.append(flag)
    return matches[0] if matches else None


def _restore_writer(
    writer: AuthorialWriter, report: Dict[str, Any]
) -> None:
    chapters = report.get("chapters") or []
    writer.set_chapters(
        [
            {
                "id": row.get("chapter_id"),
                "name": row.get("chapter_name") or "Untitled",
                "words": row.get("words_total"),
            }
            for row in chapters
            if isinstance(row, dict) and row.get("chapter_id") is not None
        ]
    )
    writer.finished = [entry for entry in report.get("batches") or [] if isinstance(entry, dict)]


def previous_german_text(report: Dict[str, Any], batch_index: int) -> Optional[str]:
    """Last accepted German paragraph of the batches before `batch_index`."""
    entries = [
        e
        for e in report.get("batches") or []
        if isinstance(e, dict) and isinstance(e.get("index"), int) and e["index"] < batch_index
    ]
    for entry in sorted(entries, key=lambda e: e["index"], reverse=True):
        for item in reversed(entry.get("items") or []):
            text = (item.get("text") or "").strip()
            if text:
                return text
    return None


async def retranslate_batch(
    entry: Dict[str, Any],
    language: str,
    book: Dict[str, Any],
    draft_profile: Dict[str, Any],
    refiner_profile: Dict[str, Any],
    checker_profile: Optional[Dict[str, Any]],
    unload: bool,
    previous_text: Optional[str],
    refine_rounds: int,
    similarity_stop: float,
    ratio_min: float,
    ratio_max: float,
) -> Dict[str, Any]:
    """Re-run draft → refine → check for ONE corrupted batch (same chain).

    Mirrors the authorial main loop for a single batch; the report entry is
    rebuilt fresh, so old flags vanish and fresh ones are computed here.
    """
    async def _unload(profile: Dict[str, Any]) -> None:
        if unload:
            try:
                await unload_ollama_profile(profile)
            except Exception:
                pass

    source_block = entry.get("source_block") or ""
    source_paragraphs = entry.get("source_paragraphs") or []
    text = ""
    notes: List[str] = []
    reviewed: Dict[str, Any] = {"review": []}  # _review_note adds here, filtered
    review = reviewed["review"]

    # ---- draft -------------------------------------------------------------
    draft_client = LlmClient(LlmProfile(**draft_profile))
    try:
        response = await draft_client.chat(
            draft_messages(source_block, language, book, previous_text),
            temperature=DRAFT_TEMPERATURE,
        )
        raw = (response.content or "").strip()
        if not raw:
            raise LlmError("empty draft")
        text, notes, _marker = clean_model_prose(raw)
        if not text.strip():
            raise LlmError("draft produced no prose before ANMERKUNGEN")
        if is_placeholder_prose(text):
            raise LlmError("draft answered with a placeholder instead of prose")
    finally:
        try:
            await draft_client.aclose()
        except Exception:
            pass
        await _unload(draft_profile)

    segments = split_paragraphs(text)
    heading = entry.get("heading")
    expected = len(source_paragraphs) + (1 if heading else 0)
    if heading and len(segments) == expected:
        heading, segments = segments[0], segments[1:]
    else:
        heading = None
    for note in notes:
        _review_note(reviewed, "annotation", None, note)
    if len(segments) == len(source_paragraphs):
        pairs = list(zip(source_paragraphs, segments))
    else:
        _review_note(
            reviewed,
            "structure",
            None,
            f"paragraph count deviates (source {len(source_paragraphs)}, draft {len(segments)}) — block refined as one text",
        )
        pairs = [(source_block, " ".join(segments))]

    # ---- refine ------------------------------------------------------------
    refiner_client = LlmClient(LlmProfile(**refiner_profile))
    items: List[Dict[str, Any]] = []
    try:
        for p_index, (para_source, para_text) in enumerate(pairs):
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
            for round_no in range(1, refine_rounds + 1):
                try:
                    response = await refiner_client.chat(
                        refine_messages(para_source, previous_text, item["text"], language, book),
                        temperature=REFINE_TEMPERATURE,
                    )
                    raw = (response.content or "").strip()
                    if not raw:
                        raise LlmError("empty refinement")
                    refined, note_lines, _marker = clean_model_prose(raw)
                    if not refined.strip():
                        raise LlmError("refinement produced no prose")
                    if is_placeholder_prose(refined):
                        raise LlmError("refinement answered with a placeholder instead of prose")
                except Exception as exc:
                    await _unload(refiner_profile)
                    item["error"] = str(exc)
                    item["kept_draft"] = True
                    _review_note(
                        reviewed,
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
                    _review_note(reviewed, "annotation", p_index, note, echo_against=refined)
                if ratio >= similarity_stop:
                    item["stopped_early"] = True
                    break
            previous_text = item["text"]
            items.append(item)
    finally:
        try:
            await refiner_client.aclose()
        except Exception:
            pass
        await _unload(refiner_profile)

    # ---- check -------------------------------------------------------------
    for item in items:
        en_text = item["source"] or ""
        de_text = item["text"] or ""
        ratio = char_ratio(de_text, en_text)
        item["length_ratio"] = ratio
        flag = length_flag(ratio, ratio_min, ratio_max)
        if flag:
            _review_note(reviewed, "length", item["index"], flag)
        if checker_profile:
            checker_client = LlmClient(LlmProfile(**checker_profile))
            try:
                response = await checker_client.chat(
                    check_messages(en_text, de_text, language), temperature=0.0
                )
                verdict = parse_fidelity(response.content or "")
            finally:
                try:
                    await checker_client.aclose()
                except Exception:
                    pass
                await _unload(checker_profile)
            item["fidelity"] = verdict
            flag = fidelity_flag(verdict)
            if flag:
                _review_note(reviewed, "fidelity", item["index"], flag)

    return {
        "index": entry.get("index"),
        "chapter_id": entry.get("chapter_id"),
        "chapter_name": entry.get("chapter_name"),
        "source_hash": entry.get("source_hash"),
        "source_block": source_block,
        "source_paragraphs": source_paragraphs,
        "stage": "done",
        "draft_error": None,
        "draft_text": text,
        "draft_notes": notes,
        "heading": heading,
        "german_segments": segments,
        "items": items,
        "review": review,
        "errors": [],
    }


def apply_decision(
    data_root: Path,
    book_id: str,
    language: str,
    book_title: str,
    batch_index: int,
    type_: str,
    paragraph: Optional[int],
    detail: str,
    action: str,
    edited_text: Optional[str],
) -> Dict[str, Any]:
    """Write one decision into the report, fixed also into every output file."""
    if action not in ACTIONS:
        raise ReviewError(400, f"action must be one of {ACTIONS}")
    authorial_path, ensemble_path = report_files(data_root, book_id, language)
    if authorial_path is None:
        raise ReviewError(404, f"no authorial report for language {language}")
    if ensemble_path is not None:
        raise ReviewError(
            400,
            "the latest report for this language is an ensemble report — decisions are authorial-only",
        )
    outdir = authorial_path.parent
    try:
        report = json.loads(authorial_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ReviewError(
            500,
            f"report unreadable ({exc}) — a decision-safe .bak exists next to it; restore it first",
        )
    if report.get("mode") != "authorial":
        raise ReviewError(400, "decisions are only available for authorial runs")
    entries = report.get("batches") or []
    entry = next((e for e in entries if isinstance(e, dict) and e.get("index") == batch_index), None)
    if entry is None:
        raise ReviewError(404, f"batch index {batch_index} not in report")
    if action == "fixed" and entry.get("stage") != "done":
        raise ReviewError(409, f"batch {batch_index} is at stage '{entry.get('stage')}' — fixing needs stage 'done'")
    flag = _find_flag(entry, type_, paragraph, detail)
    if flag is None:
        raise ReviewError(404, "flag not found in report (detail/page mismatch)")

    if action == "fixed":
        items = entry.get("items") or []
        if paragraph is None:
            if len(items) == 1:
                paragraph = 0
            else:
                raise ReviewError(400, "batch has several paragraphs — decide per paragraph")
        if paragraph >= len(items):
            raise ReviewError(400, f"paragraph {paragraph} outside the batch ({len(items)} items)")
        new_text = (edited_text or "").strip()
        if not new_text:
            raise ReviewError(400, "edited_text required for a fix")
        items[paragraph]["text"] = new_text
        item_notes = items[paragraph].setdefault("notes", [])
        item_notes.append("manuell korrigiert (Review-GUI)")

    decision = {
        "action": action,
        "decided_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        **({"edited_text": edited_text} if action == "fixed" and edited_text is not None else {}),
    }
    history = flag.setdefault("decision_history", [])
    if flag.get("decision"):
        history.append(flag["decision"])
    flag["decision"] = decision

    # corruption guard: copy the report before the first write (cheap, always)
    shutil.copyfile(authorial_path, authorial_path.with_suffix(".json.bak"))

    writer = AuthorialWriter(outdir)
    meta = {key: value for key, value in report.items() if key not in ("chapters", "batches")}
    _restore_writer(writer, report)
    writer.persist(meta, book_title or "")
    payload = collect_rows(writer.as_report(meta), editable=True)
    payload["decided"] = decision
    return payload