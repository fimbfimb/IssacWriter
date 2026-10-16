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
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.interrupted = False
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
        if not config.get("profile"):
            raise RuntimeError("translation LLM profile required")
        profile = dict(config["profile"])
        if not profile.get("model"):
            raise RuntimeError("selected LLM profile has no model configured")

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
        self.profile_name = profile.get("name")
        self.model = profile.get("model")
        try:
            self.batch_size = max(1, min(200, int(config.get("batch_size") or 10)))
        except Exception:
            self.batch_size = 10

        outdir = self.store.root / self.book_id / "translations" / _slugify(self.language)
        outdir.mkdir(parents=True, exist_ok=True)
        self.outdir = outdir
        source = outdir / "book.txt"
        resume = bool(config.get("resume"))
        if resume and not (outdir / ".book.temp.bin").exists():
            raise RuntimeError("no resumable run for this language — start fresh instead")
        if not resume:
            try:
                build_translation_source(self.store.written_chapters(self.book_id), source)
            except Exception:
                raise RuntimeError("no written chapters yet — write a chapter first")

        self.started_at = time.time()
        self.finished_at = None
        self.status = STATUS_RUNNING
        self._save_meta()
        self._task = asyncio.create_task(
            self._run_job(config, profile, source, resume), name=f"translate-{self.book_id}"
        )

    def stop(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            if self._task is not None and not self._task.done():
                self.status = STATUS_STOPPING
            return
        self.status = STATUS_STOPPING
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
        config: Dict[str, Any],
        profile: Dict[str, Any],
        source: Path,
        resume: bool,
    ) -> None:
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

            await self._fire(
                {
                    "type": "translation_started",
                    "language": self.language,
                    "model": self.model,
                    "profile_name": self.profile_name,
                    "test_num": test_num,
                    "single_translate": bool(config.get("single_translate")),
                    "batch_size": self.batch_size,
                }
            )
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