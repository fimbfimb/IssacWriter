"""Wizard orchestration: a per-book state machine driving the 6 novel steps.

The wizard owns NO engine logic — every step is started through the existing
registries (RunnerRegistry, OutlineRefineRegistry, RefineRegistry,
TranslationRegistry, GermanRefineRegistry) and only waits for it to reach a
final state. All wizard state lives in ``wizard_state.json`` (state.py) and is
persisted after every change; step engines keep their own checkpoints, so a
crash loses at most the currently open chapter.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..refine.german_refine import GermanRefineRegistry
from ..refine.runner import RefineRegistry
from ..refine.outline_runner import OutlineRefineRegistry
from ..store.project import BookStore
from ..translation.runner import TranslationRegistry, _slugify
from . import state as wstate
from .state import (
    STATUS_DONE,
    STATUS_PAUSED,
    STATUS_PAUSING,
    STATUS_RUNNING,
    STATUS_STOPPED,
    STATUS_STOPPING,
    STEP_DONE,
    STEP_ERROR,
    STEP_PAUSED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_SKIPPED,
)

POLL_INTERVAL = 2.0
PROGRESS_WRITE_INTERVAL = 2.0


def _outline_language(book: Dict[str, Any]) -> str:
    code = str(book.get("language") or "en").lower()
    return "German" if code.startswith("de") else "English"


def _report(book_dir: Path, relpath: str) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads((Path(book_dir) / relpath).read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _report_gap_count(report: Optional[Dict[str, Any]]) -> Optional[int]:
    if not report:
        return None
    batches = report.get("batches") or []
    if not batches:
        return None
    return len([b for b in batches if isinstance(b, dict) and b.get("stage") != "done"])


class WizardRunner:
    def __init__(
        self,
        book_id: str,
        store: BookStore,
        profiles: Any,
        runners: Any,
        translations: Any,
        refines: Any,
        outline_refines: Any,
        german_refines: Any,
    ):
        self.book_id = book_id
        self.store = store
        self.profiles = profiles
        self.runners = runners
        self.translations = translations
        self.refines = refines
        self.outline_refines = outline_refines
        self.german_refines = german_refines
        self.state: Optional[Dict[str, Any]] = None
        self._task: Optional[asyncio.Task] = None
        self._stop_requested = False
        self._pause_requested = False
        self._last_progress_write = 0.0
        self._emit: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
        self.last_activity: Optional[float] = None

    # ------------------------------------------------------------------ #
    # state io + events
    # ------------------------------------------------------------------ #

    @property
    def book_dir(self) -> Path:
        return self.store.root / self.book_id

    @property
    def status(self) -> str:
        if self.state:
            return str(self.state.get("status") or wstate.STATUS_CONFIG)
        return wstate.STATUS_CONFIG

    def load_state(self, reload: bool = True) -> Optional[Dict[str, Any]]:
        if reload or self.state is None:
            self.state = wstate.load_state(self.book_dir)
        return self.state

    def _save(self) -> None:
        if self.state is not None:
            wstate.save_state(self.book_dir, self.state)

    def _save_progress_throttled(self) -> None:
        # progress persistence during a long-running step; the step engines
        # persist their own checkpoints, so this only refreshes the overlay
        now = time.time()
        if now - self._last_progress_write >= PROGRESS_WRITE_INTERVAL:
            self._last_progress_write = now
            self._collect_progress()
            self._save()

    def _history(self, event: str) -> None:
        if self.state is not None:
            wstate.history_add(self.state, event)

    def bind_emit(self, emit: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
        self._emit = emit

    async def _fire(self, event: Dict[str, Any]) -> None:
        self.last_activity = time.time()
        if self._emit is not None:
            try:
                await self._emit(event)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    async def attach(self, config: Dict[str, Any]) -> None:
        """Create the wizard for a freshly created book and start it."""
        if self._task is not None and not self._task.done():
            raise RuntimeError("wizard already exists and is running")
        self._stop_requested = False
        self._pause_requested = False
        self.state = wstate.new_state(self.book_id, config)
        self.state["status"] = STATUS_RUNNING
        self._save()
        await self._fire({"type": "wizard_started", "book": self.book_id})
        self._spawn()

    def _spawn(self) -> None:
        self._task = asyncio.create_task(
            self._orchestrate(), name=f"wizard-{self.book_id}"
        )

    def has_live_task(self) -> bool:
        return self._task is not None and not self._task.done()

    async def pause(self) -> Optional[str]:
        state_ = self.load_state()
        if not state_:
            return "no wizard state"
        if not self.has_live_task() or state_["status"] != STATUS_RUNNING:
            return f"wizard is not running (status {state_['status']})"
        self._pause_requested = True
        state_["status"] = STATUS_PAUSING
        self._save()
        cfg = wstate.config_step(state_, int(state_.get("current_step") or 0))
        if cfg and cfg.get("kind") == "write":
            # the writer pauses at the next chapter boundary (checkpoint!)
            self.runners.get(self.book_id).request_pause()
        return None

    async def resume(self) -> Optional[str]:
        state_ = self.load_state()
        if not state_:
            return "no wizard state"
        if self.has_live_task():
            return "wizard already running"
        if state_["status"] not in (
            STATUS_PAUSED, STATUS_STOPPED, wstate.STATUS_ERROR,
            wstate.STATUS_CONFIG, STATUS_DONE,
        ):
            return f"cannot resume from status {state_['status']}"
        self._stop_requested = False
        self._pause_requested = False
        state_["status"] = STATUS_RUNNING
        self._history("resumed")
        self._save()
        await self._fire({"type": "wizard_resumed"})
        self._spawn()
        return None

    def _stop_current_engine(self, state_: Dict[str, Any]) -> None:
        step_no = state_.get("current_step")
        cfg = wstate.config_step(state_, int(step_no or 0)) if step_no else None
        if not cfg:
            return
        kind = cfg.get("kind")
        if kind in ("outline", "write"):
            writer_runner = self.runners.get(self.book_id)
            if kind == "write":
                # clean stop: the writer finishes the current chapter, then stops
                writer_runner.request_stop()
                if writer_runner._task is not None and not writer_runner._task.done():
                    writer_runner.request_resume()
            else:
                # the outline job has no stage gate — abort keeps the
                # persisted stages (max loss: the current stage)
                writer_runner.request_abort()
        elif kind == "outline_refine":
            self.outline_refines.get(self.book_id).stop()
        elif kind == "text_refine":
            self.refines.get(self.book_id).stop()
        elif kind == "translate":
            self.translations.get(self.book_id).stop()
        elif kind == "translate_refine":
            self.german_refines.get(self.book_id).stop()

    async def stop(self) -> Optional[str]:
        state_ = self.load_state()
        if not state_:
            return "no wizard state"
        if not self.has_live_task() or state_["status"] not in (
            STATUS_RUNNING, STATUS_PAUSING, STATUS_PAUSED, wstate.STATUS_ERROR,
        ):
            if state_["status"] != STATUS_STOPPED:
                state_["status"] = STATUS_STOPPED
                self._history("stopped")
                self._save()
            return None
        self._stop_requested = True
        if self.has_live_task() and state_["status"] in (STATUS_RUNNING, STATUS_PAUSING):
            state_["status"] = STATUS_STOPPING
            self._save()
            self._stop_current_engine(state_)
        return None

    async def retry_step(self, step_no: int) -> Optional[str]:
        state_ = self.load_state()
        if not state_:
            return "no wizard state"
        if self.has_live_task():
            return "wizard is busy — pause or stop first"
        if state_["status"] not in (STATUS_PAUSED, wstate.STATUS_ERROR, STATUS_STOPPED):
            return f"cannot retry while status is {state_['status']}"
        cfg = wstate.config_step(state_, int(step_no))
        step_row = wstate.step_state(state_, int(step_no))
        if step_row is None or cfg is None:
            return f"unknown step {step_no}"
        if step_row.get("status") != STEP_ERROR:
            return f"step {step_no} is not in error state"
        if not cfg.get("enabled", True):
            return f"step {step_no} is disabled"
        step_row.update(status=STEP_PENDING, error=None, finished_at=None, started_at=None)
        self._history(f"retry step {step_no} ({cfg['kind']})")
        state_["status"] = STATUS_RUNNING
        self._save()
        self._spawn()
        return None
    def apply_config_patch(self, patch: Dict[str, Any]) -> Optional[str]:
        """Config changes take effect per STEP restart (never mid-step)."""
        state_ = self.load_state()
        if not state_:
            return "no wizard state"
        if self.has_live_task():
            return "wizard is busy — pause or stop first"
        if not isinstance(patch, dict):
            return "invalid patch"
        if self.state.get("status") == STATUS_PAUSING:
            self.state["status"] = STATUS_PAUSED
        if "pause_between_steps" in patch:
            self.state["config"]["pause_between_steps"] = bool(patch["pause_between_steps"])
        for row in patch.get("steps") or []:
            cfg = wstate.config_step(self.state, int(row.get("step") or 0))
            if cfg is None:
                continue
            for key, value in row.items():
                if key != "step":
                    cfg[key] = value
        self._history("config updated while paused")
        self._save()
        return None

    # ------------------------------------------------------------------ #
    # orchestration
    # ------------------------------------------------------------------ #

    async def _orchestrate(self) -> None:
        state_ = self.state or self.load_state()
        if not state_:
            return
        state_["status"] = STATUS_RUNNING
        self._save()
        try:
            for step_row in list(state_.get("steps_state") or []):
                if self._stop_requested:
                    return self._finalize_stop()
                cfg = wstate.config_step(state_, int(step_row["step"]))
                if cfg is None:
                    wstate.history_add(state_, f"step {step_row['step']} missing config — skipped")
                    step_row["status"] = STEP_SKIPPED
                    self._save()
                    continue
                if step_row.get("status") == STEP_DONE or step_row.get("status") == STEP_SKIPPED:
                    continue
                if not cfg.get("enabled", True):
                    step_row["status"] = STEP_SKIPPED
                    self._history(f"step {cfg['step']} ({cfg['kind']}) skipped — disabled")
                    self._save()
                    await self._fire(
                        {"type": "wizard_step_done", "step": cfg["step"], "kind": cfg["kind"], "status": STEP_SKIPPED}
                    )
                    continue
                state_["current_step"] = cfg["step"]
                step_row.update(
                    status=STEP_RUNNING,
                    started_at=step_row.get("started_at") or time.time(),
                    error=None,
                )
                self._collect_progress(state_)
                self._save()
                await self._fire(
                    {"type": "wizard_step_started", "step": cfg["step"], "kind": cfg["kind"]}
                )

                error = None
                try:
                    error = await self._execute_step(cfg, step_row)
                except Exception as exc:  # engine crash → step error, wizard stays
                    error = f"{type(exc).__name__}: {exc}"

                if step_row.get("status") == STEP_PAUSED and not error:
                    if self._stop_requested:
                        return self._finalize_stop()
                    state_["status"] = STATUS_PAUSED
                    self._history(f"step {cfg['step']} ({cfg['kind']}) paused — resume to continue")
                    self._save()
                    await self._fire({"type": "wizard_paused", "step": cfg["step"], "kind": cfg["kind"]})
                    return
                if error:
                    step_row.update(status=STEP_ERROR, error=str(error)[:500])
                    self._history(f"step {cfg['step']} ({cfg['kind']}) error — {error}")
                    self._save()
                    await self._fire(
                        {"type": "wizard_step_error", "step": cfg["step"], "kind": cfg["kind"], "error": str(error)[:300]}
                    )
                    if self._stop_requested:
                        return self._finalize_stop()
                    state_["status"] = STATUS_PAUSED
                    self._history("paused after step error — retry the step")
                    self._save()
                    await self._fire({"type": "wizard_paused", "step": cfg["step"], "reason": "step error"})
                    return

                step_row["status"] = STEP_DONE
                step_row["finished_at"] = time.time()
                self._collect_progress(state_)
                self._apply_chain(cfg)
                note = self._progress_summary(step_row)
                self._history(f"step {cfg['step']} ({cfg['kind']}) done · {note}")
                self._save()
                await self._fire(
                    {"type": "wizard_step_done", "step": cfg["step"], "kind": cfg["kind"], "note": note}
                )

                if wstate.state_done(state_):
                    break
                if self._stop_requested:
                    return self._finalize_stop()
                pause_after = bool(
                    (state_.get("config") or {}).get("pause_between_steps")
                ) or self._pause_requested
                if pause_after:
                    self._pause_requested = False
                    state_["status"] = STATUS_PAUSED
                    self._history("paused after step — resume to continue")
                    self._save()
                    await self._fire({"type": "wizard_paused"})
                    return

            state_["current_step"] = None
            if self._stop_requested:
                return self._finalize_stop()
            if wstate.state_done(state_):
                state_["status"] = STATUS_DONE
                summary = self._final_summary()
                self._history(f"wizard done · {summary}")
                self._save()
                await self._fire({"type": "wizard_done", "summary": summary})
            else:
                state_["status"] = STATUS_PAUSED
                self._save()
        finally:
            self._task = None

    def _finalize_stop(self) -> None:
        state_ = self.state
        if state_ is None:
            return
        self._stop_requested = False
        state_["status"] = STATUS_STOPPED
        self._history("stopped")
        self._save()
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._fire({"type": "wizard_stopped"}))
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # step execution
    # ------------------------------------------------------------------ #

    async def _execute_step(self, cfg: Dict[str, Any], step_row: Dict[str, Any]) -> Optional[str]:
        kind = cfg["kind"]
        if kind == "outline":
            return await self._step_outline(cfg, step_row)
        if kind == "outline_refine":
            return await self._step_outline_refine(cfg, step_row)
        if kind == "write":
            return await self._step_write(cfg, step_row)
        if kind == "text_refine":
            return await self._step_text_refine(cfg, step_row)
        if kind == "translate":
            return await self._step_translate(cfg, step_row)
        if kind == "translate_refine":
            return await self._step_translate_refine(cfg, step_row)
        return f"unknown step kind {kind}"

    def _resolve_profile(self, profile_id: Optional[str], role: str) -> Dict[str, Any]:
        """Full profile dict by id; raises when missing or model-less."""
        if not profile_id:
            raise RuntimeError(f"{role}: no profile selected in the wizard config")
        profile = self.profiles.get(str(profile_id))
        if profile is None:
            raise RuntimeError(f"{role}: profile {profile_id} not found in the profile store")
        if not profile.get("model"):
            raise RuntimeError(f"{role}: the selected profile '{profile.get('name')}' has no model configured")
        return dict(profile)

    def _resolve_candidates(self, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        ids = [str(pid) for pid in (cfg.get("candidate_profile_ids") or []) if pid]
        if not ids:
            raise RuntimeError("candidates: at least one candidate profile is required")
        if len(ids) > 6:
            raise RuntimeError("candidates: maximum 6 candidate profiles")
        resolved = []
        for index, pid in enumerate(ids):
            profile = self.profiles.get(pid)
            if profile is None:
                raise RuntimeError(f"candidate {index + 1}: profile {pid} not found")
            if not profile.get("model"):
                raise RuntimeError(f"candidate {index + 1}: profile '{profile.get('name')}' has no model configured")
            resolved.append(dict(profile))
        return resolved

    async def _step_outline(self, cfg: Dict[str, Any], step_row: Dict[str, Any]) -> Optional[str]:
        runner = self.runners.get(self.book_id)
        if runner._task is not None and not runner._task.done():
            return f"writer runner busy in status {runner.status}"
        planner = self._resolve_profile(cfg.get("planner_profile_id"), "outline")
        book = self.store.load_book(self.book_id)
        book["planner_profile_id"] = planner["id"]
        self.store.save_book(book)
        await runner.start("outline", {"planner_profile": planner, "auto_freeze": True})
        return await self._wait_write_runner(runner, step_row)

    async def _wait_write_runner(self, runner: Any, step_row: Dict[str, Any]) -> Optional[str]:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            meta = runner._meta()
            status = str(meta.get("status") or "")
            task_alive = bool(meta.get("task_alive"))
            if task_alive and status in ("generating_outline", "writing"):
                continue
            if task_alive and status == "waiting_review":
                # forced auto mode — this should not happen; surface it
                return "runner waits for review — the wizard runs mode 'auto'"
            if task_alive and status == "paused":
                # wizard pause (checkpoint at the chapter boundary) or an
                # external pause from the Write tab — both are resumable
                step_row["status"] = STEP_PAUSED
                return None
            if task_alive:
                continue
            if status == "done":
                return None
            if status == "error":
                return str(meta.get("error") or "writer runner error")
            if status in ("idle", "stopped", "paused"):
                # aborted outline / stopped-at-checkpoint write → resumable
                step_row["status"] = STEP_PAUSED
                return None
            step_row["status"] = STEP_PAUSED
            return None

    async def _step_outline_refine(self, cfg: Dict[str, Any], step_row: Dict[str, Any]) -> Optional[str]:
        runner = self.outline_refines.get(self.book_id)
        if runner._task is not None and not runner._task.done():
            return f"outline refiner busy in status {runner.status}"
        book = self.store.load_book(self.book_id)
        candidates = self._resolve_candidates(cfg)
        judge = None
        if cfg.get("judge_profile_id"):
            judge = self._resolve_profile(cfg.get("judge_profile_id"), "judge")
        config: Dict[str, Any] = {
            "candidates": candidates,
            "judge": judge,
            "parallel_candidates": bool(cfg.get("parallel")),
            "unload_after_call": not bool(cfg.get("parallel")),
            "resume": True,
            "refine_rounds": int(cfg.get("refine_rounds") or 1),
            "similarity_stop": float(cfg.get("similarity_stop") or 0.95),
            "context_prev": int(cfg.get("context_prev", 3)),
            "language": _outline_language(book),
            "structured": True,
        }
        try:
            await runner.start(config)
        except RuntimeError as exc:
            return str(exc)
        return await self._wait_refine_runner(
            runner, step_row, report_relpath="outline_refines/outline_refine_report.json"
        )

    async def _wait_refine_runner(
        self, runner: Any, step_row: Dict[str, Any], report_relpath: Optional[str] = None
    ) -> Optional[str]:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            meta = runner._meta()
            status = str(meta.get("status") or "")
            if meta.get("task_alive"):
                continue
            if status == "done":
                gaps = _report_gap_count(_report(self.book_dir, report_relpath)) if report_relpath else None
                if gaps:
                    step_row["status"] = STEP_ERROR  # retry keeps finished units
                    return f"{gaps} unit(s) failed — retry the step (finished units are kept)"
                return None
            if status == "stopped":
                step_row["status"] = STEP_PAUSED
                return None
            if status == "error":
                return str(meta.get("error") or "refine runner error")
            if status == "idle":
                # never started (e.g. resume on a fresh wizard) — restart path
                step_row["status"] = STEP_PAUSED
                return None
            step_row["status"] = STEP_PAUSED
            return None

    async def _step_write(self, cfg: Dict[str, Any], step_row: Dict[str, Any]) -> Optional[str]:
        runner = self.runners.get(self.book_id)
        alive = runner._task is not None and not runner._task.done()
        if not alive:
            writer = self._resolve_profile(cfg.get("writer_profile_id"), "write")
            book = self.store.load_book(self.book_id)
            book["writer_profile_id"] = writer["id"]
            book["run_settings"] = {**(book.get("run_settings") or {}), "mode": "auto"}
            book["status"] = "writing"
            self.store.save_book(book)
            try:
                await runner.start("story", {"writer_profile": writer})
            except RuntimeError as exc:
                return str(exc)
        else:
            meta = runner._meta()
            if str(meta.get("status")) == "paused":
                # resuming a wizard-paused write — clear the writer's pause gate
                runner.request_resume()
        return await self._wait_write_runner(runner, step_row)

    async def _step_text_refine(self, cfg: Dict[str, Any], step_row: Dict[str, Any]) -> Optional[str]:
        runner = self.refines.get(self.book_id)
        if runner._task is not None and not runner._task.done():
            return f"refiner busy in status {runner.status}"
        book = self.store.load_book(self.book_id)
        candidates = self._resolve_candidates(cfg)
        judge = None
        if cfg.get("judge_profile_id"):
            judge = self._resolve_profile(cfg.get("judge_profile_id"), "judge")
        rounds = int(cfg.get("refine_rounds") or 2)
        config: Dict[str, Any] = {
            "candidates": candidates,
            "judge": judge,
            "parallel_candidates": bool(cfg.get("parallel")),
            "unload_after_call": not bool(cfg.get("parallel")),
            "resume": True,
            "language": _outline_language(book),
            "refine_rounds": max(1, min(3, rounds)),
            "similarity_stop": float(cfg.get("similarity_stop") or 0.97),
            "batch_words": 250,
        }
        if cfg.get("draft_temperature"):
            config["draft_temperature"] = float(cfg.get("draft_temperature"))
        if cfg.get("style_probe"):
            config["style_probe"] = str(cfg.get("style_probe"))
        try:
            await runner.start(config)
        except RuntimeError as exc:
            return str(exc)
        return await self._wait_refine_runner(
            runner, step_row, report_relpath="refines/refine_report.json"
        )

    async def _step_translate(self, cfg: Dict[str, Any], step_row: Dict[str, Any]) -> Optional[str]:
        runner = self.translations.get(self.book_id)
        if runner._task is not None and not runner._task.done():
            return f"translation busy in status {runner.status}"
        book = self.store.load_book(self.book_id)
        draft = self._resolve_profile(cfg.get("draft_profile_id"), "translation draft")
        refiner = self._resolve_profile(cfg.get("refiner_profile_id"), "translation refiner")
        checker = (
            self._resolve_profile(cfg["checker_profile_id"], "translation checker")
            if cfg.get("checker_profile_id")
            else None
        )
        use_refined = self._chain_step4_done()
        target_language = str(cfg.get("target_language") or "German") or "German"
        config: Dict[str, Any] = {
            "mode": "authorial",
            "draft": draft,
            "refiner": refiner,
            "checker": checker,
            "target_language": target_language,
            "batch_words": int(cfg.get("batch_words") or 250),
            "style_probe": str(cfg.get("style_probe") or ""),
            "unload_after_call": True,
            "resume": True,
            "use_refined": use_refined,
        }
        try:
            await runner.start(config)
        except RuntimeError as exc:
            return str(exc)
        # wait until done/sim finished
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            meta = runner._meta()
            status = str(meta.get("status") or "")
            if meta.get("task_alive"):
                continue
            if status == "done":
                return None
            if status == "stopped":
                step_row["status"] = STEP_PAUSED
                return None
            if status == "error":
                return str(meta.get("error") or "translation error")
            step_row["status"] = STEP_PAUSED
            return None

    def _chain_step4_done(self) -> bool:
        """use_refined only when the text-refine step actually finished with
        committed versions (edge case 5 — no silent fallback)."""
        state_ = self.state
        if not state_:
            return False
        row = wstate.step_state(state_, 4)
        if row is None or row.get("status") != STEP_DONE:
            return False
        report = _report(self.book_dir, "refines/refine_report.json")
        if not report:
            return False
        chapters = report.get("chapters") or []
        return any(row.get("done") for row in chapters if isinstance(row, dict))

    async def _step_translate_refine(self, cfg: Dict[str, Any], step_row: Dict[str, Any]) -> Optional[str]:
        runner = self.german_refines.get(self.book_id)
        if runner._task is not None and not runner._task.done():
            return f"german refiner busy in status {runner.status}"
        state_ = self.state
        cfg5 = wstate.config_step(state_, 5) or {}
        target_language = str(cfg5.get("target_language") or cfg.get("target_language") or "German") or "German"
        candidates = self._resolve_candidates(cfg)
        judge = None
        if cfg.get("judge_profile_id"):
            judge = self._resolve_profile(cfg.get("judge_profile_id"), "judge")
        config: Dict[str, Any] = {
            "candidates": candidates,
            "judge": judge,
            "parallel_candidates": bool(cfg.get("parallel")),
            "unload_after_call": not bool(cfg.get("parallel")),
            "resume": True,
            "language": target_language,
            "refine_rounds": int(cfg.get("refine_rounds") or 1),
            "similarity_stop": 0.97,
            "batch_words": int(cfg5.get("batch_words") or 250),
            "style_probe": str(cfg5.get("style_probe") or ""),
        }
        try:
            await runner.start(config)
        except RuntimeError as exc:
            return str(exc)
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            meta = runner._meta()
            status = str(meta.get("status") or "")
            if meta.get("task_alive"):
                continue
            if status == "done":
                gaps = _report_gap_count(
                    _report(
                        self.book_dir,
                        f"translations/{_slugify(target_language)}/german_refines/refine_report.json",
                    )
                )
                if gaps:
                    step_row["status"] = STEP_ERROR
                    return f"{gaps} unit(s) failed — retry the step (finished units are kept)"
                return None
            if status == "stopped":
                step_row["status"] = STEP_PAUSED
                return None
            if status == "error":
                return str(meta.get("error") or "german refine error")
            step_row["status"] = STEP_PAUSED
            return None

    # ------------------------------------------------------------------ #
    # chain + progress
    # ------------------------------------------------------------------ #

    def _apply_chain(self, cfg: Dict[str, Any]) -> None:
        """CHAIN (WIZARD_PLAN §3.4): verbindliche Verkettung der Ausgaben."""
        kind = cfg["kind"]
        if kind == "outline_refine":
            book = self.store.load_book(self.book_id)
            book["refined_descriptions_source"] = True
            self.store.save_book(book)
        # step 4 → step 5: use_refained is resolved at translate start
        # step 5 → step 6: the german engine reads the translation report

    def _collect_progress(self, state_: Optional[Dict[str, Any]] = None) -> None:
        """Update steps_state[].progress from the existing engines' report files."""
        target = state_ if state_ is not None else self.state
        if not target:
            return
        try:
            book = self.store.load_book(self.book_id)
        except Exception:
            book = {}
        outline = book.get("outline") or {}
        planned = len(outline.get("chapters") or [])
        # the planner skips synopsis/acts for small books (target < 10000)
        detailed = int(book.get("target_words") or 0) >= 10000
        expected_stages = ["premise", "chapters"] + (["synopsis", "acts"] if detailed else [])
        outline_done = sum(1 for key in expected_stages if outline.get(key))
        try:
            _, written = self.store.book_progress(self.book_id) if planned else (0, 0)
        except Exception:
            written = 0
        for step_row in target.get("steps_state") or []:
            cfg = wstate.config_step(target, int(step_row["step"]))
            kind = (cfg or {}).get("kind")
            if kind == "outline":
                step_row["progress"] = {
                    "done": outline_done,
                    "total": len(expected_stages),
                    "frozen": bool(outline.get("frozen")),
                }
            elif kind == "write":
                step_row["progress"] = {"done": written, "total": planned or None}
            elif kind == "outline_refine":
                report = _report(self.book_dir, "outline_refines/outline_refine_report.json")
                step_row["progress"] = self._progress_from_report(report)
            elif kind == "text_refine":
                report = _report(self.book_dir, "refines/refine_report.json")
                step_row["progress"] = self._progress_from_report(report)
            elif kind == "translate":
                report = self._translation_report(book, target)
                step_row["progress"] = self._batches_progress(report)
            elif kind == "translate_refine":
                report = self._german_report(book, target)
                step_row["progress"] = self._progress_from_report(report)

    def _progress_from_report(self, report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        chapters = (report or {}).get("chapters") or []
        rows = [row for row in chapters if isinstance(row, dict)]
        done = len([row for row in rows if row.get("done")])
        total = None
        if rows:
            total = len(rows)
        elif (report or {}).get("batches_total"):
            total = 0
        return {"done": done, "total": total}

    def _batches_progress(self, report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        batches = (report or {}).get("batches") or []
        rows = [row for row in batches if isinstance(row, dict)]
        done = len([row for row in rows if row.get("stage") == "done" or row.get("winner_text")])
        total = (report or {}).get("batches_total")
        return {"done": done, "total": total}

    def _translation_report(self, book: Dict[str, Any], state_: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        cfg = wstate.config_step(state_, 5) or {}
        settings = book.get("translation_settings") or {}
        if not isinstance(settings, dict):
            settings = {}
        language = str(cfg.get("target_language") or settings.get("target_language") or "German")
        base = f"translations/{_slugify(language)}"
        report = _report(self.book_dir, f"{base}/authorial_report.json")
        return report or _report(self.book_dir, f"{base}/ensemble_report.json")

    def _german_report(self, book: Dict[str, Any], state_: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        cfg = wstate.config_step(state_, 5) or {}
        language = str(cfg.get("target_language") or "German")
        return _report(
            self.book_dir,
            f"translations/{_slugify(language)}/german_refines/refine_report.json",
        )

    def _progress_summary(self, step_row: Dict[str, Any]) -> str:
        progress = step_row.get("progress") or {}
        done = progress.get("done")
        total = progress.get("total")
        if total:
            return f"{done}/{total}"
        return str(done or 0)

    def _final_summary(self) -> str:
        progress = " · ".join(
            f"S{row['step']} {row['status']}" for row in (self.state.get("steps_state") or [])
        )
        return progress

    # ------------------------------------------------------------------ #
    # live view (GET state)
    # ------------------------------------------------------------------ #

    def live_view(self) -> Optional[Dict[str, Any]]:
        """Fresh state + progress overlay + per-step engine metadata for the UI.

        Reads the persistence file WITHOUT touching ``self.state`` — the
        orchestration task mutates that object; the GET poll must never swap
        the working dict under a running wizard."""
        payload = wstate.load_state(self.book_dir)
        if not payload:
            return None
        if payload.get("status") == STATUS_RUNNING and not self.has_live_task():
            # server restart mid-run → the orchestration task is gone
            payload["status"] = STATUS_PAUSED
            for row in payload.get("steps_state") or []:
                if row.get("status") == STEP_RUNNING:
                    row["status"] = STEP_PAUSED
            payload.setdefault("history", []).append(
                {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "event": "interrupted — server restarted mid-run; resume to continue"}
            )
            wstate.save_state(self.book_dir, payload)
        try:
            self._collect_progress(payload)
        except Exception:
            pass
        view = dict(payload)
        view["config"] = dict(payload.get("config") or {})
        view["steps_state"] = [dict(row) for row in payload.get("steps_state") or []]
        for row in view["steps_state"]:
            cfg = wstate.config_step(payload, int(row["step"]))
            row["engine"] = self._engine_live(cfg)
        return view

    def _engine_live(self, cfg: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not cfg:
            return None
        kind = cfg.get("kind")
        try:
            if kind in ("outline", "write"):
                meta = self.runners.get(self.book_id)._meta()
            elif kind == "outline_refine":
                meta = self.outline_refines.get(self.book_id)._meta()
            elif kind == "text_refine":
                meta = self.refines.get(self.book_id)._meta()
            elif kind == "translate":
                meta = self.translations.get(self.book_id)._meta()
            elif kind == "translate_refine":
                meta = self.german_refines.get(self.book_id)._meta()
            else:
                return None
        except Exception:
            return None
        return {
            "status": meta.get("status"),
            "error": meta.get("error"),
            "task_alive": bool(meta.get("task_alive")),
            "activity": meta.get("activity"),
            "last_activity": meta.get("last_activity"),
        }


class WizardRegistry:
    def __init__(
        self,
        store: BookStore,
        profiles: Any,
        runners: Any,
        translations: Any,
        refines: Any,
        outline_refines: Any,
        german_refines: Any,
    ):
        self.store = store
        self.profiles = profiles
        self.runners = runners
        self.translations = translations
        self.refines = refines
        self.outline_refines = outline_refines
        self.german_refines = german_refines
        self._wizards: Dict[str, WizardRunner] = {}

    def get(self, book_id: str) -> WizardRunner:
        if book_id not in self._wizards:
            self._wizards[book_id] = WizardRunner(
                book_id,
                self.store,
                self.profiles,
                self.runners,
                self.translations,
                self.refines,
                self.outline_refines,
                self.german_refines,
            )
        return self._wizards[book_id]

    def list_all(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if not self.store.root.exists():
            return rows
        for directory in sorted(self.store.root.iterdir()):
            if not directory.is_dir():
                continue
            payload = wstate.load_state(directory)
            if not payload:
                continue
            book = None
            try:
                book = self.store.load_book(directory.name)
            except Exception:
                book = {}
            rows.append(
                {
                    "book_id": directory.name,
                    "title": (book or {}).get("title") or "",
                    "status": payload.get("status"),
                    "current_step": payload.get("current_step"),
                    "created": payload.get("created"),
                    "updated": payload.get("updated"),
                }
            )
        return rows