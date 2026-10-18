"""Outline-brief refiner runner: one concurrent run per book, refine_*-style
events with the outline_refine_ prefix, merged into the shared SSE stream."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from ..store.project import BookStore
from ..translation.ensemble import TranslationStopped
from .outline_engine import (
    DEFAULT_CONTEXT_PREV,
    DEFAULT_REFINE_ROUNDS,
    DEFAULT_SIMILARITY_STOP,
    run_outline_refinement,
)

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_STOPPING = "stopping"
STATUS_STOPPED = "stopped"
STATUS_DONE = "done"
STATUS_ERROR = "error"


class OutlineRefineRunner:
    def __init__(self, book_id: str, store: BookStore):
        self.book_id = book_id
        self.store = store
        self.status = STATUS_IDLE
        self.error: Optional[str] = None
        self.language: Optional[str] = None
        self.profile_name: Optional[str] = None
        self.model: Optional[str] = None
        self.batches_total: Optional[int] = None
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

    def bind_emit(self, emit: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
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
        if self._emit is not None:
            try:
                await self._emit(event)
            except Exception:
                pass
        self._append_log(event)

    def _append_log(self, event: Dict[str, Any]) -> None:
        outdir = self.outdir
        kind = str(event.get("type") or "")
        if outdir is None or not kind.startswith("outline_refine_") and kind != "llm_call":
            return
        if kind == "outline_refine_started":
            line = (
                f"start — briefs · profile {event.get('profile') or '?'}"
                f" · language {event.get('language') or '?'}"
                f" · rounds {event.get('refine_rounds') or 1}"
                f" · chapters {event.get('chapters') or []}"
            )
        elif kind == "outline_refine_batch":
            index = event.get("batch_index")
            total = event.get("batches_total")
            if event.get("error"):
                line = f"chapter {index}/{total} FAILED — {event['error']}"
            elif event.get("note"):
                line = str(event["note"])
            else:
                line = (
                    f"chapter {index}/{total} (id {event.get('chapter_id')}) · brief ok"
                    f" · {event.get('words') or 0} words · {event.get('rounds') or 1} round(s)"
                )
        elif kind == "outline_refine_chapter":
            line = f"chapter {event.get('chapter_id')} committed → outline_refined/ v{int(event.get('version') or 0):04d} ({event.get('words') or 0} words)"
        elif kind == "outline_refine_output":
            line = " · ".join(str(part) for part in event.get("lines") or [])
        elif kind == "llm_call":
            if event.get("phase") == "start":
                line = f"… {event.get('role')} arbeitet: {event.get('detail')}"
            elif event.get("phase") == "done":
                line = f"✔ {event.get('role')} fertig ({event.get('elapsed_s')}s) — {event.get('detail')}"
            else:
                line = f"✘ {event.get('role')} Fehler nach {event.get('elapsed_s')}s — {event.get('detail')}"
        elif kind == "outline_refine_done":
            line = f"{'done' if event.get('status') == 'done' else 'stopped/interrupted'} — versions kept in outline_refined/"
            if event.get("gaps"):
                line += f" · {event['gaps']} chapter(s) failed"
        elif kind == "outline_refine_error":
            line = "error — " + str(event.get("error") or "")
        else:
            line = kind
        stamp = time.strftime("%H:%M:%S")
        try:
            with (outdir / "outline_refine_log.txt").open("a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {line}\n")
        except Exception:
            pass

    def _meta(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "error": self.error,
            "language": self.language,
            "profile_name": self.profile_name,
            "model": self.model,
            "batches_total": self.batches_total,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "interrupted": self.interrupted,
            "task_alive": bool(self._task is not None and not self._task.done()),
            "activity": self.activity,
            "last_activity": self.last_activity,
        }

    def _save_meta(self) -> None:
        if self.outdir is None:
            return
        try:
            (self.outdir / "outline_refine_meta.json").write_text(
                json.dumps(self._meta(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    async def start(self, config: Dict[str, Any]) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("outline refinement already in progress")
        profile = dict(config.get("profile") or {})
        if not profile:
            raise RuntimeError("outline refine needs an LLM profile")
        if not profile.get("model"):
            raise RuntimeError("the outline refine profile has no model configured")

        book = self.store.load_book(self.book_id)
        outline = book.get("outline") or {}
        if not outline.get("chapters"):
            raise RuntimeError("no chapter outline yet — generate the outline first")

        chapters_plan = sorted(outline["chapters"], key=lambda c: int(c["id"]))
        chapter_ids = config.get("chapter_ids")
        if chapter_ids not in (None, ""):
            try:
                wanted = {int(ch) for ch in chapter_ids}
            except Exception:
                raise RuntimeError("chapter_ids must be a list of chapter numbers")
            chapters_plan = [c for c in chapters_plan if int(c["id"]) in wanted]
        chapters_plan = [c for c in chapters_plan if str(c.get("description") or "").strip()]
        if not chapters_plan:
            raise RuntimeError("no chapters with descriptions to refine")

        self.status = STATUS_IDLE
        self.error = None
        self.interrupted = False
        self.language = str(config.get("language") or "English").strip() or "English"
        self.profile_name = profile.get("name") or profile.get("model")
        self.model = profile.get("model")
        self.batches_total = len(chapters_plan)

        book_dir = self.store.root / self.book_id
        outdir = book_dir / "outline_refines"
        outdir.mkdir(parents=True, exist_ok=True)
        self.outdir = outdir
        self.refined_dir = book_dir / "outline_refined"
        if not bool(config.get("resume")):
            try:
                (outdir / "outline_refine_log.txt").unlink(missing_ok=True)
            except Exception:
                pass

        self.started_at = time.time()
        self.finished_at = None
        self.status = STATUS_RUNNING
        self._stop_requested = False
        self._save_meta()
        self._task = asyncio.create_task(
            self._run_job(book, profile, config, chapters_plan, bool(config.get("resume"))),
            name=f"outline-refine-{self.book_id}",
        )

    def stop(self) -> None:
        self._stop_requested = True
        if self._task is not None and not self._task.done():
            self.status = STATUS_STOPPING
            self._save_meta()

    async def _run_job(
        self,
        book: Dict[str, Any],
        profile: Dict[str, Any],
        config: Dict[str, Any],
        chapters_plan: list,
        resume: bool,
    ) -> None:
        await self._fire(
            {
                "type": "outline_refine_started",
                "profile": self.profile_name,
                "language": self.language,
                "refine_rounds": int(config.get("refine_rounds") or 1),
                "chapters": [int(c["id"]) for c in chapters_plan],
            }
        )
        def _clamp_int(value: Any, default: float, lo: int, hi: int) -> int:
            try:
                result = int(value)
            except (TypeError, ValueError):
                return int(default)
            return min(hi, max(lo, result))

        def _clamp_float(value: Any, default: float, lo: float, hi: float) -> float:
            try:
                result = float(value)
            except (TypeError, ValueError):
                return default
            return min(hi, max(lo, result))

        try:
            summary = await run_outline_refinement(
                book=book,
                profile=profile,
                language=self.language,
                unload_after_call=bool(config.get("unload_after_call", True)),
                refine_rounds=_clamp_int(config.get("refine_rounds"), DEFAULT_REFINE_ROUNDS, 1, 3),
                similarity_stop=_clamp_float(config.get("similarity_stop"), DEFAULT_SIMILARITY_STOP, 0.5, 1.0),
                context_prev=_clamp_int(config.get("context_prev"), DEFAULT_CONTEXT_PREV, 0, 50),
                style_probe=str(config.get("style_probe") or ""),
                outdir=self.outdir or (self.store.root / self.book_id / "outline_refines"),
                refined_dir=self.refined_dir or (self.store.root / self.book_id / "outline_refined"),
                resume=resume,
                structured=bool(config.get("structured", True)),
                emit=self._fire,
                should_stop=lambda: self._stop_requested,
            )
        except TranslationStopped:
            self.finished_at = time.time()
            self.status = STATUS_STOPPED
            self.interrupted = True
            self.error = None
            await self._fire(
                {
                    "type": "outline_refine_done",
                    "status": "interrupted",
                    "note": "Stopped by user — finished chapter briefs kept, resume continues.",
                }
            )
            self._save_meta()
            return
        except Exception as exc:
            self.finished_at = time.time()
            self.status = STATUS_ERROR
            self.error = str(exc)
            self._save_meta()
            await self._fire({"type": "outline_refine_error", "error": str(exc)})
            return
        self.finished_at = time.time()
        self.error = None
        if summary.get("gaps") and not summary.get("batches_done"):
            self.status = STATUS_ERROR
            self.error = "every chapter failed — see the outline refine report for model errors"
            await self._fire({"type": "outline_refine_error", "error": self.error})
        else:
            self.status = STATUS_DONE
            await self._fire(
                {
                    "type": "outline_refine_done",
                    "status": "done",
                    "batches_total": summary.get("batches_total"),
                    "batches_done": summary.get("batches_done"),
                    "gaps": summary.get("gaps") or 0,
                    "totals": summary.get("totals"),
                    "review_notes": summary.get("review_notes") or 0,
                }
            )
        self._save_meta()


class OutlineRefineRegistry:
    def __init__(self, store: BookStore):
        self.store = store
        self._runners: Dict[str, OutlineRefineRunner] = {}

    def get(self, book_id: str) -> OutlineRefineRunner:
        if book_id not in self._runners:
            self._runners[book_id] = OutlineRefineRunner(book_id, self.store)
        return self._runners[book_id]