"""German refine (wizard step 6): a second prose pass over the finished
authorial translation.

Source of truth: ``translations/<slug>/authorial_report.json`` — every batch
with stage ``done`` contributes its item texts + German heading. Fallback when
the report is missing: the per-chapter files ``chapters_de/chNNNN.md``. If
neither exists, the runner refuses to start ("finish step 5 first").

The ORIGINAL English chapters and the German translation outputs are consumed
read-only. Versioned commits land in
``translations/<slug>/german_refined/chNNNN/vNNNN.json`` + ``index.json``
(never overwrite — same doctrine as refined/) plus the derived assembly
``translations/<slug>/book_german_refined.txt``.

The actual prose pipeline is the EXISTING refine engine (``run_refinement``)
with language "German"; this module only resolves the German source chapters,
remaps the event prefixes to ``german_refine_*`` and keeps the derived assembly
current during the run.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..store.project import BookStore
from ..translation.authorial import DEFAULT_MIN_WORDS
from ..translation.ensemble import TranslationStopped
from ..translation.runner import _slugify
from .engine import (
    DEFAULT_REFINE_ROUNDS,
    DEFAULT_SIMILARITY_STOP,
    existing_versions,
    load_index,
    read_version,
    run_refinement,
)

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_STOPPING = "stopping"
STATUS_STOPPED = "stopped"
STATUS_DONE = "done"
STATUS_ERROR = "error"

_HEADING_LINE = re.compile(r"^\s*(?:Kapitel|Chapter)\s+\d+\s*[:.]?\s*(.*)$", re.IGNORECASE)


def slug_dir(book_dir: Path, language: str) -> Path:
    return Path(book_dir) / "translations" / _slugify(language or "German")


def workdir(slug_dir: Path) -> Path:
    return slug_dir / "german_refines"


def refined_dir(slug_dir: Path) -> Path:
    return slug_dir / "german_refined"


def _split_heading(text: str) -> tuple:
    lines = [line.strip() for line in (text or "").splitlines()]
    heading = None
    body: List[str] = []
    for index, line in enumerate(lines):
        if not line:
            continue
        if index == 0 or heading is None:
            match = _HEADING_LINE.match(line)
            if match and heading is None:
                heading = line
                continue
            if heading is None and index == 0:
                heading = line  # first non-empty line is the heading by convention
                continue
        body.append(line)
    return heading, "\n".join(part for part in body).strip()


def german_chapters_from_report(
    report: Optional[Dict[str, Any]], chapters_dir: Path
) -> tuple[List[Dict[str, Any]], List[int]]:
    """German source chapters from the authorial report (finished batches) —
    chapters that are still incomplete fall back to their chapters_de file."""
    sources: Dict[int, Dict[str, Any]] = {}

    def _from_batches(entries: List[Dict[str, Any]]) -> None:
        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            cid = entry.get("chapter_id")
            if cid is None:
                continue
            grouped.setdefault(int(cid), []).append(entry)
        for cid, rows in grouped.items():
            rows.sort(key=lambda e: int(e.get("index") or 0))
            done_rows = [row for row in rows if row.get("stage") == "done"]
            if not done_rows or len(done_rows) < len(rows):
                continue
            parts: List[str] = []
            heading = next((row.get("heading") for row in rows if row.get("heading")), None)
            for row in done_rows:
                for item in row.get("items") or []:
                    text = (item.get("text") or "").strip()
                    if text:
                        parts.append(text)
            if not parts:
                continue
            sources[cid] = {
                "id": cid,
                "name": (rows[0].get("chapter_name") or f"Chapter {cid + 1}") if rows else f"Chapter {cid + 1}",
                "heading": heading,
                "content": "\n\n".join(parts),
                "source": "report",
            }

    if report and report.get("batches"):
        _from_batches(report.get("batches") or [])
    if chapters_dir.exists():
        for path in sorted(chapters_dir.glob("ch*.md")):
            match = re.fullmatch(r"ch(\d{4})\.md", path.name)
            if not match:
                continue
            cid = int(match.group(1))
            if cid in sources and sources[cid].get("source") == "report":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            heading, body = _split_heading(text)
            if not body.strip():
                continue
            sources[cid] = {
                "id": cid,
                "name": (heading or f"Kapitel {cid + 1}"),
                "heading": heading,
                "content": body,
                "source": "file",
            }
    for cid in sorted(sources):
        row = sources[cid]
        heading = row.get("heading") or ""
        match = _HEADING_LINE.match(heading) if heading else None
        if match and match.group(1).strip():
            row["name"] = match.group(1).strip()
        else:
            match = _HEADING_LINE.match(row["name"] or "")
            if match and match.group(1).strip():
                row["name"] = match.group(1).strip()
    chapters = [sources[cid] for cid in sorted(sources)]
    return chapters, sorted(sources)


def rebuild_german_files(slug_dir: Path) -> Optional[str]:
    """Assemble translations/<slug>/book_german_refined.txt from the ACTIVE
    german_refined versions (derived file only — versions stay the truth)."""
    directory = refined_dir(slug_dir)
    if not directory.exists():
        return None
    parts: List[str] = []
    for chapter_dir in sorted(directory.glob("ch*")):
        match = re.fullmatch(r"ch(\d+)", chapter_dir.name)
        if not chapter_dir.is_dir() or not match:
            continue
        payload = read_version(directory, int(match.group(1)), None)
        if payload and str(payload.get("content") or "").strip():
            heading = (
                payload.get("heading")
                or f"Kapitel {int(match.group(1)) + 1}: {payload.get('chapter_name') or 'Untitled'}"
            )
            parts.extend([heading, "", str(payload.get("content") or "").strip(), ""])
    if not parts:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    target = slug_dir / "book_german_refined.txt"
    target.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    return str(target)


def scrub_foreign_assemblies(slug_dir: Path) -> None:
    """run_refinement/rebuild_active_files drop book_refined.txt/
    manuscript_refined.md next to refined_dir — inside the translations slug
    dir those names are misleading, so the german runner keeps them out."""
    for name in ("book_refined.txt", "manuscript_refined.md"):
        try:
            (slug_dir / name).unlink(missing_ok=True)
        except Exception:
            pass


def set_active_german_version(slug_dir: Path, chapter_id: int, version: int) -> Dict[str, Any]:
    directory = refined_dir(slug_dir)
    if int(version) not in existing_versions(directory, chapter_id):
        raise ValueError(
            f"version v{int(version):04d} does not exist for german-refined chapter {int(chapter_id) + 1}"
        )
    index = load_index(directory)
    index.setdefault("active", {})[str(int(chapter_id))] = int(version)
    index["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (directory / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    rebuild_german_files(slug_dir)
    scrub_foreign_assemblies(slug_dir)
    return {"chapter_id": int(chapter_id), "version": int(version)}


def reset_german_refine_all(slug_dir: Path) -> List[str]:
    removed: List[str] = []
    for directory in (workdir(slug_dir), refined_dir(slug_dir)):
        if directory.is_dir():
            shutil.rmtree(directory)
            removed.append(directory.name)
    for name in ("book_german_refined.txt", "book_refined.txt", "manuscript_refined.md"):
        try:
            (slug_dir / name).unlink(missing_ok=True)
        except Exception:
            pass
    return removed


class GermanRefineRunner:
    """One german refine job per book (wizard step 6), mirroring RefineRunner."""

    def __init__(self, book_id: str, store: BookStore):
        self.book_id = book_id
        self.store = store
        self.status = STATUS_IDLE
        self.error: Optional[str] = None
        self.language: Optional[str] = None
        self.profile_name: Optional[str] = None
        self.model: Optional[str] = None
        self.batch_words: int = 250
        self.mode: str = "german_refine"
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.interrupted = False
        self._stop_requested = False
        self.console: deque = deque(maxlen=150)
        self._task: Optional[asyncio.Task] = None
        self._emit: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
        self.outdir: Optional[Path] = None
        self.refined: Optional[Path] = None
        self.slug_dir: Optional[Path] = None
        self.source_mode: str = ""
        self.source_chapters: List[int] = []
        self.activity: Optional[Dict[str, Any]] = None
        self.last_activity: Optional[float] = None

    def bind_emit(self, emit: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
        self._emit = emit

    def _rewrite(self, event: Dict[str, Any]) -> Dict[str, Any]:
        kind = str(event.get("type") or "")
        if kind.startswith("refine_"):
            event = {**event, "type": "german_" + kind}
        return event

    async def _fire(self, event: Dict[str, Any]) -> None:
        event = self._rewrite(event)
        self.last_activity = time.time()
        kind = str(event.get("type") or "")
        if kind == "llm_call":
            if event.get("phase") == "start":
                self.activity = {
                    "role": str(event.get("role") or "?"),
                    "detail": str(event.get("detail") or ""),
                    "since": time.time(),
                }
            else:
                self.activity = None
        self._mirror_current_file()
        if self._emit is not None:
            try:
                await self._emit(event)
            except Exception:
                pass
        self._append_log(event)

    def _mirror_current_file(self) -> None:
        """Keep translations/<slug>/book_german_refined_current.txt growing per
        block (the wizard live pane reads the top-level path)."""
        outdir = self.outdir
        if outdir is None:
            return
        current = outdir / "book_refined_current.txt"
        if not current.is_file():
            return
        try:
            (self.slug_dir / "book_german_refined_current.txt").write_text(
                current.read_text(encoding="utf-8", errors="replace"), encoding="utf-8"
            )
        except Exception:
            pass

    def _append_log(self, event: Dict[str, Any]) -> None:
        outdir = self.outdir
        kind = str(event.get("type") or "")
        if outdir is None or not (kind.startswith("german_refine_") or kind in ("llm_call", "wizard_candidates")):
            return
        if kind == "german_refine_started":
            line = (
                f"start — german refine · source {', '.join(str(c) for c in self.source_chapters) or '?'}"
                f" · rounds {event.get('refine_rounds') or 2}"
            )
        elif kind == "german_refine_batch":
            index = event.get("batch_index")
            total = event.get("batches_total")
            if event.get("error"):
                line = f"block {index}/{total} · {event.get('stage') or 'run'} FAILED — {event['error']}"
            elif event.get("note"):
                line = str(event["note"])
            else:
                line = f"block {index}/{total} · chapter {event.get('chapter_id') or '?'} · {event['stage']} ok"
        elif kind == "german_refine_chapter":
            line = (
                f"chapter {event.get('chapter_id')} committed → german_refined/ "
                f"v{int(event.get('version') or 0):04d} ({event.get('words') or 0} words)"
            )
        elif kind == "wizard_candidates":
            line = (
                f"candidates {', '.join(event.get('candidates') or [])} → winner "
                f"{event.get('winner') or '?'} ({event.get('unit') or ''})"
            )
        elif kind == "german_refine_output":
            line = " · ".join(str(part) for part in event.get("lines") or [])
        elif kind == "llm_call":
            if event.get("phase") == "start":
                line = f"… {event.get('role')} arbeitet: {event.get('detail')}"
            elif event.get("phase") == "done":
                line = f"✔ {event.get('role')} fertig ({event.get('elapsed_s')}s) — {event.get('detail')}"
            else:
                line = f"✘ {event.get('role')} Fehler nach {event.get('elapsed_s')}s — {event.get('detail')}"
        elif kind == "german_refine_done":
            line = f"{'done' if event.get('status') == 'done' else 'stopped/interrupted'} — versions kept in german_refined/"
            if event.get("gaps"):
                line += f" · {event['gaps']} block(s) failed"
        elif kind == "german_refine_error":
            line = "error — " + str(event.get("error") or "")
        else:
            line = kind
        stamp = time.strftime("%H:%M:%S")
        try:
            with (outdir / "german_refine_log.txt").open("a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {line}\n")
        except Exception:
            pass

    def _meta(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "error": self.error,
            "language": self.language,
            "mode": self.mode,
            "profile_name": self.profile_name,
            "model": self.model,
            "batch_words": self.batch_words,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "interrupted": self.interrupted,
            "task_alive": bool(self._task is not None and not self._task.done()),
            "source_chapters": list(self.source_chapters),
            "activity": self.activity,
            "last_activity": self.last_activity,
        }

    def _save_meta(self) -> None:
        if self.outdir is None:
            return
        try:
            (self.outdir / "german_refine_meta.json").write_text(
                json.dumps(self._meta(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _clamp_int(self, value: Any, default: float, lo: int, hi: int) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError):
            return int(default)
        return min(hi, max(lo, result))

    def _clamp_float(self, value: Any, default: float, lo: float, hi: float) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return default
        return min(hi, max(lo, result))

    async def start(self, config: Dict[str, Any]) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("german refine already in progress")
        candidates = [
            dict(c)
            for c in (config.get("candidates") or [])
            if isinstance(c, dict) and c
        ]
        judge = dict(config.get("judge") or {}) if config.get("judge") else None
        ensemble_active = len(candidates) >= 2
        if ensemble_active:
            if any(not c.get("model") for c in candidates):
                raise RuntimeError("a candidate profile has no model configured")
            if judge and not judge.get("model"):
                raise RuntimeError("the judge profile has no model configured")
            self.profile_name = "candidates (" + " · ".join(
                str(c.get("name") or c.get("model")) for c in candidates
            ) + (f" + judge {judge.get('name') or judge.get('model')}" if judge else " (no judge)") + ")"
            self.model = ", ".join(str(c.get("model")) for c in candidates)
        else:
            base = dict(config.get("profile") or (candidates[0] if candidates else {}))
            if not base.get("model"):
                raise RuntimeError("german refine needs a profile with a model configured")
            self.profile_name = str(base.get("name") or base.get("model"))
            self.model = base.get("model")
        self.language = str(config.get("language") or "German").strip() or "German"
        self.batch_words = self._clamp_int(
            config.get("batch_words") or DEFAULT_MIN_WORDS, DEFAULT_MIN_WORDS, 1, 5000
        )

        book = self.store.load_book(self.book_id)
        current = slug_dir(self.store.root / self.book_id, self.language)
        report_path = current / "authorial_report.json"
        report: Optional[Dict[str, Any]] = None
        if report_path.is_file():
            try:
                loaded = json.loads(report_path.read_text(encoding="utf-8"))
                report = loaded if isinstance(loaded, dict) else None
            except Exception:
                report = None
        chapters, _used = german_chapters_from_report(report, current / "chapters_de")
        if not chapters:
            raise RuntimeError(
                "no German translation found — finish translation step 5 first "
                "(authorial report or chapters_de files missing)"
            )
        self.source_mode = "report" if (report and (report.get("batches") or [])) else "files"
        self.slug_dir = current
        self.source_chapters = [int(c["id"]) for c in chapters]

        outdir = workdir(current)
        outdir.mkdir(parents=True, exist_ok=True)
        self.outdir = outdir
        self.refined = refined_dir(current)
        if not bool(config.get("resume")):
            try:
                (outdir / "german_refine_log.txt").unlink(missing_ok=True)
            except Exception:
                pass

        self.status = STATUS_IDLE
        self.error = None
        self.interrupted = False
        self.started_at = time.time()
        self.finished_at = None
        self.status = STATUS_RUNNING
        self._stop_requested = False
        self._save_meta()
        self._task = asyncio.create_task(
            self._run_job(
                book,
                config,
                chapters,
                candidates,
                judge,
                bool(config.get("resume")),
            ),
            name=f"german-refine-{self.book_id}",
        )

    def stop(self) -> None:
        self._stop_requested = True
        if self._task is not None and not self._task.done():
            self.status = STATUS_STOPPING
            self._save_meta()

    async def _run_job(
        self,
        book: Dict[str, Any],
        config: Dict[str, Any],
        chapters: List[Dict[str, Any]],
        candidates: List[Dict[str, Any]],
        judge: Optional[Dict[str, Any]],
        resume: bool,
    ) -> None:
        ensemble_active = len(candidates) >= 2
        fallback_slug = slug_dir(self.store.root / self.book_id, self.language or "German")
        if ensemble_active:
            draft_profile = dict(candidates[0])
            refiner_profile = None
        else:
            # MUST mirror start(): with a single candidate the wizard does not
            # hand a "profile" key — fall back to the candidate's own dict.
            # A missing profile here would build LlmProfile(**{}) whose empty
            # base_url makes the openai SDK default to http://127.0.0.1:8000/v1 —
            # every draft call then dies with
            # "Error code: 405 - {'detail': 'Method Not Allowed'}" against the
            # StoryForge server itself (Eitelkeit step 6, 10/10 blocks failed).
            base_profile = dict(
                config.get("profile")
                or (candidates[0] if candidates else {})
            )
            draft_profile = base_profile
            refiner_profile = base_profile
        await self._fire(
            {
                "type": "german_refine_started",
                "refine_rounds": int(config.get("refine_rounds") or 2),
                "chapters": [int(c["id"]) for c in chapters],
                "source": self.source_mode,
                "candidates": [c.get("name") or c.get("model") for c in candidates] if ensemble_active else None,
                "judge": judge.get("name") if (judge and ensemble_active) else None,
                "parallel_candidates": bool(config.get("parallel_candidates")) if ensemble_active else None,
            }
        )
        try:
            summary = await run_refinement(
                chapters=chapters,
                language=self.language or "German",
                draft_profile=draft_profile,
                refiner_profile=refiner_profile,
                checker_profile=None,
                unload_after_call=bool(config.get("unload_after_call", True))
                and not (ensemble_active and bool(config.get("parallel_candidates"))),
                refine_rounds=self._clamp_int(
                    config.get("refine_rounds"), DEFAULT_REFINE_ROUNDS, 1, 6
                ),
                similarity_stop=self._clamp_float(
                    config.get("similarity_stop"), DEFAULT_SIMILARITY_STOP, 0.5, 1.0
                ),
                ratio_min=0.75,
                ratio_max=1.5,
                min_words=self.batch_words,
                max_words=None,
                style_probe=str(config.get("style_probe") or ""),
                outdir=self.outdir or workdir(fallback_slug),
                refined_dir=self.refined or refined_dir(fallback_slug),
                book=book,
                resume=resume,
                structured=bool(config.get("structured", True)),
                check_mode="essential",
                candidates=candidates if ensemble_active else None,
                judge=judge if ensemble_active else None,
                parallel_candidates=bool(config.get("parallel_candidates")) and ensemble_active,
                emit=self._fire,
                should_stop=lambda: self._stop_requested,
            )
        except TranslationStopped:
            self.finished_at = time.time()
            self.status = STATUS_STOPPED
            self.interrupted = True
            self.error = None
            rebuild_german_files(self.slug_dir or self.store.root / self.book_id / "translations")
            scrub_foreign_assemblies(self.slug_dir)
            self._mirror_current_file()
            await self._fire(
                {
                    "type": "german_refine_done",
                    "status": "interrupted",
                    "note": "Stopped by user — finished blocks kept, resume continues.",
                }
            )
            self._save_meta()
            return
        except Exception as exc:
            self.finished_at = time.time()
            self.status = STATUS_ERROR
            self.error = str(exc)
            self._save_meta()
            await self._fire({"type": "german_refine_error", "error": str(exc)})
            return
        self.finished_at = time.time()
        self.error = None
        rebuild_german_files(self.slug_dir)
        scrub_foreign_assemblies(self.slug_dir)
        if summary.get("gaps") and not summary.get("batches_done"):
            self.status = STATUS_ERROR
            self.error = "every block failed — see the german refine report for model errors"
            await self._fire({"type": "german_refine_error", "error": self.error})
        else:
            self.status = STATUS_DONE
            await self._fire(
                {
                    "type": "german_refine_done",
                    "status": "done",
                    "batches_total": summary.get("batches_total"),
                    "batches_done": summary.get("batches_done"),
                    "gaps": summary.get("gaps") or 0,
                    "output_file": str(self.slug_dir / "book_german_refined.txt"),
                    "report_file": summary.get("report_file"),
                    "totals": summary.get("totals"),
                }
            )
        self._save_meta()


class GermanRefineRegistry:
    def __init__(self, store: BookStore):
        self.store = store
        self._runners: Dict[str, GermanRefineRunner] = {}

    def get(self, book_id: str) -> GermanRefineRunner:
        if book_id not in self._runners:
            self._runners[book_id] = GermanRefineRunner(book_id, self.store)
        return self._runners[book_id]