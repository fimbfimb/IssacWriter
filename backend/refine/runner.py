"""Refine runner: one concurrent English-prose refinement run per book.

Mirrors TranslationRunner (translation/runner.py) minus the external tool
mode — the refiner is always the built-in engine. Events use the
``refine_*`` prefix and merge into the same per-book SSE stream.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..store.project import BookStore
from .engine import (
    DEFAULT_MIN_WORDS,
    DEFAULT_RATIO_MAX,
    DEFAULT_RATIO_MIN,
    DEFAULT_REFINE_ROUNDS,
    DEFAULT_SIMILARITY_STOP,
    rebuild_active_files,
    run_refinement,
)
from ..translation.ensemble import TranslationStopped

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_STOPPING = "stopping"
STATUS_STOPPED = "stopped"
STATUS_DONE = "done"
STATUS_ERROR = "error"


class RefineRunner:
    """One refine job per book, decoupled from the writer runner."""

    def __init__(self, book_id: str, store: BookStore):
        self.book_id = book_id
        self.store = store
        self.status = STATUS_IDLE
        self.error: Optional[str] = None
        self.output_file: Optional[str] = None
        self.language: Optional[str] = None
        self.profile_name: Optional[str] = None
        self.model: Optional[str] = None
        self.batch_words: int = 250
        self.mode: str = "refine"
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.interrupted = False
        self._stop_requested = False
        self.console: deque = deque(maxlen=150)
        self._task: Optional[asyncio.Task] = None
        self._emit: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
        self.outdir: Optional[Path] = None
        self.refined_dir: Optional[Path] = None
        self.activity: Optional[Dict[str, Any]] = None
        self.last_activity: Optional[float] = None
        self.workers: List[Optional[Dict[str, Any]]] = []  # per parallel slot: {role, detail, since}

    def bind_emit(self, emit: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
        """Merge refine events into the shared book event stream."""
        self._emit = emit

    async def _fire(self, event: Dict[str, Any]) -> None:
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
        if event.get("worker") is not None:
            # per-slot view for the parallel refine: which worker does WHAT
            slot = int(event["worker"])
            while len(self.workers) <= slot:
                self.workers.append(None)
            if kind == "llm_call" and event.get("phase") == "start":
                self.workers[slot] = {
                    "slot": slot + 1,
                    "role": str(event.get("role") or "?"),
                    "detail": str(event.get("detail") or ""),
                    "since": time.time(),
                }
            elif kind == "llm_call":
                self.workers[slot] = None
        if self._emit is not None:
            try:
                # the shared event fan-out must never hold the runner hostage —
                # a stalled subscriber froze the runner task (task_alive stayed
                # True after a stop and the wizard reported busy on everything)
                await asyncio.wait_for(self._emit(event), timeout=5.0)
            except Exception:
                pass
        self._append_log(event)

    def _append_log(self, event: Dict[str, Any]) -> None:
        outdir = self.outdir
        kind = str(event.get("type") or "")
        if outdir is None or not (kind.startswith("refine_") or kind in ("llm_call", "wizard_candidates")):
            return
        if kind == "wizard_candidates":
            line = (
                f"candidates {', '.join(event.get('candidates') or [])} → winner "
                f"{event.get('winner') or '?'} ({event.get('unit') or ''})"
            )
        elif kind == "refine_started":
            line = (
                f"start — refine · draft {event.get('draft') or '?'}"
                f" · refiner {event.get('refiner') or '?'}"
                f" · checker {event.get('checker') or '— (length only)'}"
            )
        elif kind == "refine_batch":
            index = event.get("batch_index")
            total = event.get("batches_total")
            if event.get("error"):
                line = f"block {index}/{total} · {event.get('stage') or 'run'} FAILED — {event['error']}"
            elif event.get("note"):
                line = str(event["note"])
            else:
                extra = []
                if event.get("paragraphs"):
                    extra.append(f"{event['paragraphs']} paragraphs")
                if event.get("notes"):
                    extra.append(f"{event['notes']} note(s)")
                if event.get("flags") is not None:
                    extra.append(f"{event['flags']} flag(s)")
                line = f"block {index}/{total} · chapter {event.get('chapter_id') or '?'} · {event['stage']} ok"
                if extra:
                    line += " · " + " · ".join(extra)
        elif kind == "refine_chapter":
            line = (
                f"chapter {event.get('chapter_id')} committed → refined/ v{int(event.get('version') or 0):04d}"
                f" ({event.get('words') or 0} words)"
            )
        elif kind == "refine_output":
            line = " · ".join(str(part) for part in event.get("lines") or [])
        elif kind == "refine_progress":
            line = (
                f"¶ {event.get('paragraph')}/{event.get('paragraphs_total')} verfeinert"
                f" · {event.get('rounds') or 0} Runde(n)"
                + (" · Frühstopp" if event.get("stopped_early") else "")
            )
        elif kind == "llm_call":
            if event.get("phase") == "start":
                line = f"… {event.get('role')} arbeitet: {event.get('detail')}"
            elif event.get("phase") == "done":
                line = f"✔ {event.get('role')} fertig ({event.get('elapsed_s')}s) — {event.get('detail')}"
            else:
                line = f"✘ {event.get('role')} Fehler nach {event.get('elapsed_s')}s — {event.get('detail')}"
        elif kind == "refine_done":
            line = f"{'done' if event.get('status') == 'done' else 'stopped/interrupted'} — versions kept in refined/"
            if event.get("review_flags") is not None:
                line += f" · {event['review_flags']} flag(s), {event.get('annotations') or 0} note(s)"
            if event.get("gaps"):
                line += f" · {event['gaps']} block(s) failed"
        elif kind == "refine_error":
            line = "error — " + str(event.get("error") or "")
        else:
            line = kind
        stamp = time.strftime("%H:%M:%S")
        try:
            with (outdir / "refine_log.txt").open("a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {line}\n")
        except Exception:
            pass

    def _meta(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "error": self.error,
            "output_file": self.output_file,
            "language": self.language,
            "profile_name": self.profile_name,
            "model": self.model,
            "batch_words": self.batch_words,
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "interrupted": self.interrupted,
            "task_alive": bool(self._task is not None and not self._task.done()),
            "activity": self.activity,
            "last_activity": self.last_activity,
            "workers": [w for w in self.workers],
        }

    def _save_meta(self) -> None:
        if self.outdir is None:
            return
        try:
            (self.outdir / "refine_meta.json").write_text(
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
            raise RuntimeError("refinement already in progress")
        candidates = [
            dict(c)
            for c in (config.get("candidates") or [])
            if isinstance(c, dict) and c
        ]
        judge = dict(config.get("judge") or {}) if config.get("judge") else None
        ensemble_active = len(candidates) >= 2
        draft = dict(config.get("draft") or (candidates[0] if candidates else {}))
        refiner = dict(config.get("refiner") or (candidates[0] if candidates else {}))
        checker = dict(config.get("checker") or {}) if config.get("checker") else None
        if ensemble_active:
            if any(not c.get("model") for c in candidates):
                raise RuntimeError("a candidate profile has no model configured")
            if judge and not judge.get("model"):
                raise RuntimeError("the judge profile has no model configured")
        else:
            if not draft:
                raise RuntimeError("refine needs a draft profile")
            if not draft.get("model"):
                raise RuntimeError("the draft profile has no model configured")
            if not refiner:
                raise RuntimeError("refine needs a refiner profile (the strongest available model)")
            if not refiner.get("model"):
                raise RuntimeError("the refiner profile has no model configured")
        if checker and not checker.get("model"):
            raise RuntimeError("the checker profile has no model configured")

        try:
            chapters = self.store.written_chapters(self.book_id)
        except Exception:
            chapters = []
        chapter_ids = config.get("chapter_ids")
        if chapter_ids not in (None, ""):
            try:
                wanted = {int(ch) for ch in chapter_ids}
            except Exception:
                raise RuntimeError("chapter_ids must be a list of chapter numbers")
            chapters = [c for c in chapters if int(c["id"]) in wanted]
        if not chapters:
            raise RuntimeError("no written chapters yet — write a chapter first")

        resume = bool(config.get("resume"))
        self.status = STATUS_IDLE
        self.error = None
        self.interrupted = False
        self.language = str(config.get("language") or "English").strip() or "English"
        self.batch_words = self._clamp_int(config.get("batch_words") or DEFAULT_MIN_WORDS, DEFAULT_MIN_WORDS, 1, 5000)
        if ensemble_active:
            self.profile_name = "candidates (" + " · ".join(
                str(c.get("name") or c.get("model")) for c in candidates
            )
            if judge:
                self.profile_name += f" + judge {judge.get('name') or judge.get('model')}"
            else:
                self.profile_name += " (no judge)"
            self.profile_name += ")"
            self.model = ", ".join(str(c.get("model")) for c in candidates)
        else:
            self.profile_name = "refine (draft: {draft} · refiner: {refiner} · checker: {checker})".format(
                draft=str(draft.get("name") or draft.get("model") or "?"),
                refiner=str(refiner.get("name") or refiner.get("model") or "?"),
                checker=str((checker or {}).get("name") or (checker or {}).get("model") or "—"),
            )
            self.model = ", ".join(
                str(p.get("model")) for p in (draft, refiner, checker) if p and p.get("model")
            )

        book_dir = self.store.root / self.book_id
        outdir = book_dir / "refines"
        outdir.mkdir(parents=True, exist_ok=True)
        self.outdir = outdir
        self.refined_dir = book_dir / "refined"
        if not resume:
            try:
                (outdir / "refine_log.txt").unlink(missing_ok=True)
            except Exception:
                pass

        self.started_at = time.time()
        self.finished_at = None
        self.status = STATUS_RUNNING
        self._stop_requested = False
        self._save_meta()
        self._task = asyncio.create_task(
            self._run_job(config, draft, refiner, checker, chapters, resume),
            name=f"refine-{self.book_id}",
        )

    def stop(self) -> None:
        self._stop_requested = True
        if self._task is not None and not self._task.done():
            self.status = STATUS_STOPPING
            self._save_meta()

    async def _run_job(
        self,
        config: Dict[str, Any],
        draft: Dict[str, Any],
        refiner: Dict[str, Any],
        checker: Optional[Dict[str, Any]],
        chapters: List[Dict[str, Any]],
        resume: bool,
    ) -> None:
        candidates = [dict(c) for c in (config.get("candidates") or []) if isinstance(c, dict) and c]
        ensemble_active = len(candidates) >= 2
        judge = dict(config.get("judge") or {}) if config.get("judge") else None
        started_event: Dict[str, Any] = {
            "type": "refine_started",
            "mode": "refine",
            "language": self.language,
            "model": self.model,
            "profile_name": self.profile_name,
            "draft": draft.get("name") or draft.get("model"),
            "refiner": refiner.get("name") or refiner.get("model"),
            "checker": None if not checker else (checker.get("name") or checker.get("model")),
            "batches_words": self.batch_words,
            "chapters": [int(c["id"]) for c in chapters],
        }
        if ensemble_active:
            started_event["candidates"] = [
                c.get("name") or c.get("model") for c in candidates
            ]
            started_event["judge"] = (judge or {}).get("name") or (judge or {}).get("model") if judge else None
            started_event["parallel_candidates"] = bool(config.get("parallel_candidates"))
        await self._fire(started_event)
        try:
            summary = await run_refinement(
                chapters=chapters,
                language=self.language,
                draft_profile=draft,
                refiner_profile=refiner,
                checker_profile=checker,
                unload_after_call=(
                    bool(config.get("unload_after_call", True))
                    and not (ensemble_active and bool(config.get("parallel_candidates")))
                ),
                candidates=candidates if ensemble_active else None,
                judge=judge if ensemble_active else None,
                parallel_candidates=bool(config.get("parallel_candidates")) and ensemble_active,
                refine_rounds=self._clamp_int(
                    config.get("refine_rounds"), DEFAULT_REFINE_ROUNDS, 1, 6
                ),
                draft_temperature=self._clamp_float(
                    config.get("draft_temperature"), 0.7, 0.0, 2.0
                ),
                similarity_stop=self._clamp_float(
                    config.get("similarity_stop"), DEFAULT_SIMILARITY_STOP, 0.5, 1.0
                ),
                ratio_min=self._clamp_float(config.get("ratio_min"), DEFAULT_RATIO_MIN, 0.1, 2.0),
                ratio_max=self._clamp_float(config.get("ratio_max"), DEFAULT_RATIO_MAX, 0.5, 5.0),
                min_words=self.batch_words,
                max_words=None,
                parallel_blocks=self._clamp_int(config.get("parallel_blocks"), 1, 1, 8),
                style_probe=str(config.get("style_probe") or ""),
                outdir=self.outdir or (self.store.root / self.book_id / "refines"),
                refined_dir=self.refined_dir
                or (self.store.root / self.book_id / "refined"),
                book=self.store.load_book(self.book_id),
                resume=resume,
                structured=bool(config.get("structured", True)),
                check_mode=(
                    str(config.get("check_mode") or "essential").lower()
                    if str(config.get("check_mode") or "essential").lower() in ("essential", "strict")
                    else "essential"
                ),
                emit=self._fire,
                should_stop=lambda: self._stop_requested,
            )
        except TranslationStopped:
            self.finished_at = time.time()
            self.status = STATUS_STOPPED
            self.interrupted = True
            self.error = None
            if rebuild_active_files(self.store.root / self.book_id):
                self.output_file = str(self.refined_dir / "book_refined.txt")
            await self._fire(
                {
                    "type": "refine_done",
                    "status": "interrupted",
                    "output_file": self.output_file,
                    "note": "Stopped by user — finished stages kept, resume continues.",
                }
            )
            self._save_meta()
            return
        except Exception as exc:
            self.finished_at = time.time()
            self.status = STATUS_ERROR
            self.error = str(exc)
            self._save_meta()
            await self._fire({"type": "refine_error", "error": str(exc)})
            return
        self.finished_at = time.time()
        self.output_file = summary.get("output_file")
        self.error = None
        if summary.get("gaps") and not summary.get("batches_done"):
            self.status = STATUS_ERROR
            self.error = "every block failed — see the refine report for model errors"
            await self._fire({"type": "refine_error", "error": self.error})
        else:
            self.status = STATUS_DONE
            await self._fire(
                {
                    "type": "refine_done",
                    "status": "done",
                    "output_file": summary.get("output_file"),
                    "manuscript_file": summary.get("manuscript_file"),
                    "review_file": summary.get("review_file"),
                    "gaps": summary.get("gaps") or 0,
                    "review_flags": summary.get("review_flags") or 0,
                    "annotations": summary.get("annotations") or 0,
                    "report_file": summary.get("report_file"),
                    "totals": summary.get("totals"),
                }
            )
        self._save_meta()


class RefineRegistry:
    def __init__(self, store: BookStore):
        self.store = store
        self._runners: Dict[str, RefineRunner] = {}

    def get(self, book_id: str) -> RefineRunner:
        if book_id not in self._runners:
            self._runners[book_id] = RefineRunner(book_id, self.store)
        return self._runners[book_id]