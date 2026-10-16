"""Independent translation runner.

Translates the current manuscript with `bilingual_book_maker` (yihong0618, MIT)
as an external subprocess in its own venv. Strictly additive: it only reads
chapter files, never touches the BookRunner, checkpoints or narrative state,
so a book-writing run can continue while a translation is in flight.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..store.project import BookStore
from .authorial import (
    DEFAULT_DRAFT_TEMPERATURE,
    DEFAULT_MIN_WORDS,
    DEFAULT_RATIO_MAX,
    DEFAULT_RATIO_MIN,
    DEFAULT_REFINE_ROUNDS,
    DEFAULT_SIMILARITY_STOP,
    run_authorial_translation,
)
from .ensemble import TranslationStopped, run_ensemble_translation
from .exporter import build_translation_source

ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = ROOT / "tools"
BBM_ROOT = TOOLS_ROOT / "bilingual_book_maker"
BBM_VENV = TOOLS_ROOT / "bbm-venv"

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_STOPPING = "stopping"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_STOPPED = "stopped"

SAVED_MARKER = re.compile(r"^\s*Bilingual book saved:\s*(.+?)\s*$")


def tool_python() -> Optional[Path]:
    candidates = [
        BBM_VENV / "Scripts" / "python.exe",
        BBM_VENV / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def tool_status() -> Dict[str, Any]:
    python = tool_python()
    return {
        "tool_dir": str(BBM_ROOT),
        "tool_present": (BBM_ROOT / "make_book.py").exists(),
        "venv_python": str(python) if python else None,
        "ready": bool((BBM_ROOT / "make_book.py").exists() and python),
    }


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:32] or "target"


class TranslationRunner:
    """One concurrent translation run per book, decoupled from the writer."""

    def __init__(self, book_id: str, store: BookStore):
        self.book_id = book_id
        self.store = store
        self.status = STATUS_IDLE
        self.error: Optional[str] = None
        self.output_file: Optional[str] = None
        self.language: Optional[str] = None
        self.profile_name: Optional[str] = None
        self.model: Optional[str] = None
        self.batch_size: int = 10
        self.mode: str = "tool"
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.interrupted = False
        self._stop_requested = False
        self.console: deque = deque(maxlen=150)
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self._stderr_tail: deque = deque(maxlen=30)
        self._emit: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
        self.outdir: Optional[Path] = None

    def bind_emit(self, emit: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
        """Merge translation events into the shared book event stream (SSE/log)."""
        self._emit = emit

    async def _fire(self, event: Dict[str, Any]) -> None:
        if self._emit is not None:
            try:
                await self._emit(event)
            except Exception:
                pass
        self._append_log(event)

    def _append_log(self, event: Dict[str, Any]) -> None:
        """Append a human-readable line to translation_log.txt (live progress file)."""
        outdir = self.outdir
        kind = str(event.get("type") or "")
        if outdir is None or not kind.startswith("translation_"):
            return
        if kind == "translation_started":
            roles = ""
            if event.get("mode") == "authorial":
                roles = (
                    f" · draft {event.get('draft') or '?'}"
                    f" · refiner {event.get('refiner') or '?'}"
                    f" · checker {event.get('checker') or '— (length only)'}"
                )
            elif event.get("candidates"):
                roles = f" · candidates {', '.join(event['candidates'])} · judge {event.get('judge') or '—'}"
            line = f"start — {event.get('language') or '?'} · mode {event.get('mode') or 'tool'}"
            if event.get("profile_name"):
                line += f" · {event['profile_name']}"
            line += roles
        elif kind == "translation_batch":
            index = event.get("batch_index")
            total = event.get("batches_total")
            if event.get("error"):
                line = f"block {index}/{total} · {event.get('stage') or 'run'} FAILED — {event['error']}"
            elif event.get("stage"):
                extra = []
                if event.get("paragraphs"):
                    extra.append(f"{event['paragraphs']} paragraphs")
                if event.get("notes"):
                    extra.append(f"{event['notes']} note(s) from draft")
                if event.get("flags") is not None:
                    extra.append(f"{event['flags']} flag(s)")
                line = f"block {index}/{total} · chapter {event.get('chapter_id') or '?'} · {event['stage']} ok"
                if extra:
                    line += " · " + " · ".join(extra)
            else:
                line = f"block {index}/{total} · {event.get('winner') or ''}{(' — ' + event['reason']) if event.get('reason') else ''}"
                line = line.rstrip(" ·")
        elif kind == "translation_output":
            line = " · ".join(str(part) for part in event.get("lines") or [])
        elif kind == "translation_done":
            line = f"{'done' if event.get('status') == 'done' else 'stopped/interrupted'} — {event.get('output_file') or 'no output file'}"
            if event.get("review_flags") is not None:
                line += f" · {event['review_flags']} flag(s), {event.get('annotations') or 0} note(s) → review_list.md"
            if event.get("gaps"):
                line += f" · {event['gaps']} block(s) failed"
        elif kind == "translation_error":
            line = (
                "error — "
            ) + str(event.get("error") or "")
        else:
            line = kind
        stamp = time.strftime("%H:%M:%S")
        try:
            with (outdir / "translation_log.txt").open("a", encoding="utf-8") as handle:
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
            "batch_size": self.batch_size,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "interrupted": self.interrupted,
            "task_alive": bool(self._task is not None and not self._task.done()),
            "tool": tool_status(),
        }

    def _save_meta(self) -> None:
        if self.outdir is None:
            return
        try:
            payload = self._meta()
            payload.pop("tool", None)
            (self.outdir / "translation_meta.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    async def start(self, config: Dict[str, Any]) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("translation already in progress")
        mode = str(config.get("mode") or "tool").strip().lower() or "tool"
        if mode not in ("tool", "ensemble", "authorial"):
            raise RuntimeError("mode must be 'tool', 'ensemble' or 'authorial'")
        candidates = list(config.get("candidates") or [])
        judge = dict(config.get("judge") or {}) if config.get("judge") else None
        profile = dict(config["profile"]) if config.get("profile") else None
        draft = dict(config["draft"]) if config.get("draft") else None
        refiner = dict(config["refiner"]) if config.get("refiner") else None
        checker = dict(config["checker"]) if config.get("checker") else None
        if mode == "tool" and not profile:
            raise RuntimeError("translation LLM profile required")
        if mode == "tool" and profile and not profile.get("model"):
            raise RuntimeError("selected LLM profile has no model configured")
        if mode == "ensemble" and not candidates:
            raise RuntimeError("ensemble mode needs at least one candidate profile")
        if mode == "ensemble" and any(not c.get("model") for c in candidates):
            raise RuntimeError("a candidate profile has no model configured")
        if mode == "authorial":
            if not draft:
                raise RuntimeError("authorial mode needs a draft profile")
            if not draft.get("model"):
                raise RuntimeError("the draft profile has no model configured")
            if not refiner:
                raise RuntimeError("authorial mode needs a refiner profile (the strongest available model)")
            if not refiner.get("model"):
                raise RuntimeError("the refiner profile has no model configured")
            if checker and not checker.get("model"):
                raise RuntimeError("the checker profile has no model configured")
        self.mode = mode

        if mode == "tool":
            status = tool_status()
            if not status["tool_present"]:
                raise RuntimeError(
                    f"bilingual_book_maker not found in {BBM_ROOT}; clone it per README (Translation setup)"
                )
            if status["venv_python"] is None:
                raise RuntimeError("tool venv missing — run the setup commands from the README")

        self.status = STATUS_IDLE
        self.error = None
        self.output_file = None
        self.interrupted = False
        self.language = str(config.get("target_language") or "German").strip() or "German"
        try:
            self.batch_size = max(1, min(200, int(config.get("batch_size") or 10)))
        except Exception:
            self.batch_size = 10
        if mode == "tool":
            self.profile_name = (profile or {}).get("name")
            self.model = (profile or {}).get("model")
        elif mode == "ensemble":
            self.profile_name = "ensemble (" + " · ".join(
                str(c.get("name") or c.get("model")) for c in candidates
            ) + ")"
            self.model = ", ".join(str(c.get("model")) for c in candidates[:4])
        else:
            self.profile_name = "authorial (draft: {draft} · refiner: {refiner} · checker: {checker})".format(
                draft=str((draft or {}).get("name") or (draft or {}).get("model") or "?"),
                refiner=str((refiner or {}).get("name") or (refiner or {}).get("model") or "?"),
                checker=str((checker or {}).get("name") or (checker or {}).get("model") or "—"),
            )
            self.model = ", ".join(
                str(p.get("model")) for p in (draft, refiner, checker) if p and p.get("model")
            )

        outdir = self.store.root / self.book_id / "translations" / _slugify(self.language)
        outdir.mkdir(parents=True, exist_ok=True)
        self.outdir = outdir
        source = outdir / "book.txt"
        resume = bool(config.get("resume"))
        if not resume:
            try:
                (outdir / "translation_log.txt").unlink(missing_ok=True)
            except Exception:
                pass
        if resume and mode == "tool" and not (outdir / ".book.temp.bin").exists():
            raise RuntimeError("no resumable run for this language — start fresh instead")
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
        if mode == "tool" and not resume:
            build_translation_source(chapters, source)

        self.started_at = time.time()
        self.finished_at = None
        self.status = STATUS_RUNNING
        self._stop_requested = False
        self._save_meta()
        self._task = asyncio.create_task(
            self._run_job(mode, config, profile or {}, source, resume, chapters),
            name=f"translate-{self.book_id}",
        )

    def stop(self) -> None:
        self._stop_requested = True
        proc = self._proc
        if self._task is not None and not self._task.done():
            self.status = STATUS_STOPPING
            self._save_meta()
        if proc is None or proc.returncode is not None:
            return
        if sys.platform == "win32":
            # Deliver Ctrl+C so the tool saves its resume state file gracefully.
            try:
                proc.send_signal(signal.CTRL_C_EVENT)
                return
            except Exception:
                pass
        try:
            proc.terminate()
        except ProcessLookupError:
            pass

    async def _run_job(
        self,
        mode: str,
        config: Dict[str, Any],
        profile: Dict[str, Any],
        source: Path,
        resume: bool,
        chapters: List[Dict[str, Any]],
    ) -> None:
        test_num = int(config.get("test_num") or 0)
        judge = dict(config.get("judge") or {})
        started_event: Dict[str, Any] = {
            "type": "translation_started",
            "mode": mode,
            "language": self.language,
            "model": self.model,
            "profile_name": self.profile_name,
            "candidates": [c.get("name") or c.get("model") for c in (config.get("candidates") or [])],
            "judge": judge.get("name") or judge.get("model"),
            "test_num": test_num,
            "single_translate": bool(config.get("single_translate")),
            "batch_size": self.batch_size,
        }
        if mode == "authorial":
            started_event["draft"] = (config.get("draft") or {}).get("name") or (config.get("draft") or {}).get("model")
            started_event["refiner"] = (config.get("refiner") or {}).get("name") or (config.get("refiner") or {}).get("model")
            started_event["checker"] = None if not config.get("checker") else (
                (config.get("checker") or {}).get("name") or (config.get("checker") or {}).get("model")
            )
        await self._fire(started_event)
        if mode == "ensemble":
            await self._job_ensemble(config, judge, chapters, resume)
            return
        if mode == "authorial":
            await self._job_authorial(config, chapters, resume)
            return
        try:
            cmd: List[str] = [
                str(tool_python()),
                "make_book.py",
                "--book_name",
                str(source),
                "--model",
                str(profile["model"]),
                "--api_format",
                "openai",
                "--api_base",
                str(profile.get("base_url") or ""),
                "--language",
                str(self.language),
                "--temperature",
                str(float(profile.get("temperature", 0.7) or 0.7)),
                "--batch_size",
                str(self.batch_size),
                "--source_lang",
                "auto",
            ]
            test_num = int(config.get("test_num") or 0)
            if test_num > 0:
                cmd += ["--test", "--test_num", str(test_num)]
            if config.get("single_translate"):
                cmd += ["--single_translate"]
            if resume:
                cmd += ["--resume"]

            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            env["NO_COLOR"] = "1"
            env["BBM_API_KEY"] = profile.get("api_key") or "local"
            env["OPENAI_API_KEY"] = profile.get("api_key") or "local"
            env.pop("OPENAI_BASE_URL", None)

            create_flags = {}
            if sys.platform == "win32":
                create_flags["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(BBM_ROOT),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **create_flags,
            )
            pump_out = asyncio.create_task(self._pump_stdout())
            pump_err = asyncio.create_task(self._pump_stderr())
            return_code = await self._proc.wait()
            for task in (pump_out, pump_err):
                try:
                    await asyncio.wait_for(task, timeout=10)
                except asyncio.TimeoutError:
                    task.cancel()
        except asyncio.CancelledError:
            self.status = STATUS_STOPPED
            raise
        except Exception as exc:
            self.status = STATUS_ERROR
            self.error = str(exc)
            self.finished_at = time.time()
            self._save_meta()
            await self._fire({"type": "translation_error", "error": str(exc)})
            return

        self.finished_at = time.time()
        await self._finalize(return_code)
        self._save_meta()

    async def _job_ensemble(
        self,
        config: Dict[str, Any],
        judge: Dict[str, Any],
        chapters: List[Dict[str, Any]],
        resume: bool,
    ) -> None:
        try:
            summary = await run_ensemble_translation(
                chapters=chapters,
                language=self.language,
                candidate_profiles=list(config.get("candidates") or []),
                judge_profile=judge or None,
                unload_after_call=bool(config.get("unload_after_call", True)),
                batch_size=self.batch_size,
                outdir=self.outdir or (self.store.root / self.book_id / "translations"),
                book=self.store.load_book(self.book_id),
                resume=resume,
                emit=self._fire,
                should_stop=lambda: self._stop_requested,
            )
        except TranslationStopped:
            self.finished_at = time.time()
            self.status = STATUS_STOPPED
            self.interrupted = True
            self.error = None
            for candidate in (
                self.outdir / "book_bilingual.txt",
                self.outdir / "book_bilingual_temp.txt",
            ):
                if candidate.is_file():
                    self.output_file = str(candidate)
                    break
            await self._fire(
                {
                    "type": "translation_done",
                    "status": "interrupted",
                    "output_file": self.output_file,
                    "note": "Stopped by user — finished blocks kept in the report, resume continues.",
                }
            )
            self._save_meta()
            return
        except Exception as exc:
            self.finished_at = time.time()
            self.status = STATUS_ERROR
            self.error = str(exc)
            self._save_meta()
            await self._fire({"type": "translation_error", "error": str(exc)})
            return
        self.finished_at = time.time()
        self.output_file = summary.get("output_file")
        self.error = None
        if summary.get("gaps") and not summary.get("batches_done"):
            self.status = STATUS_ERROR
            self.error = "every block failed — see the ensemble report for model errors"
            await self._fire({"type": "translation_error", "error": self.error})
        else:
            self.status = STATUS_DONE
            await self._fire(
                {
                    "type": "translation_done",
                    "status": "done",
                    "output_file": self.output_file,
                    "gaps": summary.get("gaps") or 0,
                    "report_file": summary.get("report_file"),
                    "totals": summary.get("totals"),
                }
            )
        self._save_meta()

    def _clamp_float(self, value: Any, default: float, lo: float, hi: float) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return default
        return min(hi, max(lo, result))

    def _clamp_int(self, value: Any, default: int, lo: int, hi: int) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError):
            return default
        return min(hi, max(lo, result))

    async def _job_authorial(
        self,
        config: Dict[str, Any],
        chapters: List[Dict[str, Any]],
        resume: bool,
    ) -> None:
        min_words = self._clamp_int(config.get("batch_words") or DEFAULT_MIN_WORDS, DEFAULT_MIN_WORDS, 1, 5000)
        try:
            summary = await run_authorial_translation(
                chapters=chapters,
                language=self.language,
                draft_profile=dict(config.get("draft") or {}),
                refiner_profile=dict(config.get("refiner") or {}),
                checker_profile=dict(config["checker"]) if config.get("checker") else None,
                unload_after_call=bool(config.get("unload_after_call", True)),
                refine_rounds=self._clamp_int(
                    config.get("refine_rounds"), DEFAULT_REFINE_ROUNDS, 1, 6
                ),
                draft_temperature=self._clamp_float(
                    config.get("draft_temperature"), DEFAULT_DRAFT_TEMPERATURE, 0.0, 2.0
                ),
                similarity_stop=self._clamp_float(
                    config.get("similarity_stop"), DEFAULT_SIMILARITY_STOP, 0.5, 1.0
                ),
                ratio_min=self._clamp_float(config.get("ratio_min"), DEFAULT_RATIO_MIN, 0.1, 2.0),
                ratio_max=self._clamp_float(config.get("ratio_max"), DEFAULT_RATIO_MAX, 0.5, 5.0),
                min_words=min_words,
                max_words=None,
                style_probe=str(config.get("style_probe") or ""),
                outdir=self.outdir or (self.store.root / self.book_id / "translations"),
                book=self.store.load_book(self.book_id),
                resume=resume,
                emit=self._fire,
                should_stop=lambda: self._stop_requested,
            )
        except TranslationStopped:
            self.finished_at = time.time()
            self.status = STATUS_STOPPED
            self.interrupted = True
            self.error = None
            for candidate in (
                self.outdir / "book_bilingual.txt",
                self.outdir / "book_bilingual_temp.txt",
            ):
                if candidate.is_file():
                    self.output_file = str(candidate)
                    break
            await self._fire(
                {
                    "type": "translation_done",
                    "status": "interrupted",
                    "output_file": self.output_file,
                    "note": "Stopped by user — finished stages kept in the report, resume continues.",
                }
            )
            self._save_meta()
            return
        except Exception as exc:
            self.finished_at = time.time()
            self.status = STATUS_ERROR
            self.error = str(exc)
            self._save_meta()
            await self._fire({"type": "translation_error", "error": str(exc)})
            return
        self.finished_at = time.time()
        self.output_file = summary.get("output_file")
        self.error = None
        if summary.get("gaps") and not summary.get("batches_done"):
            self.status = STATUS_ERROR
            self.error = "every block failed — see the authorial report for model errors"
            await self._fire({"type": "translation_error", "error": self.error})
        else:
            self.status = STATUS_DONE
            await self._fire(
                {
                    "type": "translation_done",
                    "status": "done",
                    "output_file": self.output_file,
                    "german_file": summary.get("german_file"),
                    "review_file": summary.get("review_file"),
                    "gaps": summary.get("gaps") or 0,
                    "review_flags": summary.get("review_flags") or 0,
                    "annotations": summary.get("annotations") or 0,
                    "report_file": summary.get("report_file"),
                    "totals": summary.get("totals"),
                }
            )
        self._save_meta()

    async def _finalize(self, return_code: int) -> None:
        out_path = self._resolve_output()
        if self.status == STATUS_STOPPING:
            self.status = STATUS_STOPPED
            self.interrupted = True
            if out_path:
                self.output_file = out_path
        if out_path and Path(out_path).name != "book_bilingual_temp.txt":
            self.status = STATUS_DONE
            self.interrupted = False
            self.error = None
            self.output_file = out_path
            await self._fire({"type": "translation_done", "status": "done", "output_file": out_path})
        elif out_path:
            self.output_file = out_path
            self.error = None
            if self.status != STATUS_ERROR:
                self.status = STATUS_STOPPED
            self.interrupted = True
            await self._fire(
                {
                    "type": "translation_done",
                    "status": "interrupted",
                    "output_file": out_path,
                    "note": "Run ended early — partial bilingual output kept, resume possible.",
                }
            )
        else:
            self.status = STATUS_ERROR
            self.error = _last_line(self._stderr_tail) or "translation tool failed without output"
            await self._fire({"type": "translation_error", "error": self.error})

    async def _pump_stdout(self) -> None:
        proc = self._proc
        stream = proc.stdout
        if stream is None:
            return
        pending: List[str] = []
        last_flush = time.monotonic()
        try:
            while True:
                raw = await stream.readline()
                if not raw:
                    if pending:
                        await self._fire({"type": "translation_output", "lines": list(pending)})
                        pending.clear()
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line.strip():
                    continue
                match = SAVED_MARKER.match(line)
                if match:
                    self.output_file = match.group(1)
                self.console.append(line)
                pending.append(line)
                now = time.monotonic()
                if len(pending) >= 24 or (now - last_flush) >= 0.8:
                    await self._fire({"type": "translation_output", "lines": list(pending)})
                    pending.clear()
                    last_flush = now
        except asyncio.CancelledError:
            pass

    async def _pump_stderr(self) -> None:
        stream = self._proc.stderr
        if stream is None:
            return
        try:
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.strip():
                    self._stderr_tail.append(line)
                    self.console.append(f"[tool] {line}")
        except asyncio.CancelledError:
            pass

    def _resolve_output(self) -> Optional[str]:
        outdir = self.outdir
        if outdir is None:
            return None
        candidates: List[Path] = []
        if self.output_file:
            candidates.append(Path(self.output_file))
        candidates += [
            outdir / "book_bilingual.txt",
            outdir / "book_bilingual_temp.txt",
        ]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
        return None


def _last_line(values: deque) -> Optional[str]:
    for raw in reversed(list(values)):
        text = raw.strip()
        if "SystemExit" in text and ":" in text:
            text = text.split(":", 1)[-1].strip()
        if text:
            return text[-400:]
    return None


class TranslationRegistry:
    def __init__(self, store: BookStore):
        self.store = store
        self._runners: Dict[str, TranslationRunner] = {}

    def get(self, book_id: str) -> TranslationRunner:
        if book_id not in self._runners:
            self._runners[book_id] = TranslationRunner(book_id, self.store)
        return self._runners[book_id]