from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from ..llm.provider import LlmClient, LlmError, LlmProfile
from ..store.project import BookStore
from .agent import ChapterIncomplete, ChapterWriter
from .planner import OutlineBuilder, outline_to_prompt_json
from .prompts import codex_context
from .search_index import ChapterSearchIndex
from .state import NarrativeState
from .tools import count_words

STATUS_IDLE = "idle"
STATUS_OUTLINE = "generating_outline"
STATUS_WRITING = "writing"
STATUS_PAUSED = "paused"
STATUS_REVIEW = "waiting_review"
STATUS_STOPPING = "stopping"
STATUS_STOPPED = "stopped"
STATUS_STEP = "stepped"
STATUS_DONE = "done"
STATUS_ERROR = "error"


def _now() -> float:
    return time.time()


class DeltaRelay:
    def __init__(self, emit, max_chars: int = 220, max_interval: float = 0.3):
        self.emit = emit
        self.max_chars = max_chars
        self.max_interval = max_interval
        self._buffer: Dict[str, str] = {}
        self._last_flush = 0.0
        self._chars_since_start = 0

    async def feed(self, kind: str, text: str) -> None:
        if not text:
            return
        self._buffer[kind] = self._buffer.get(kind, "") + text
        self._chars_since_start += len(text)
        now = time.monotonic()
        total_buffered = sum(len(v) for v in self._buffer.values())
        if total_buffered >= self.max_chars or (now - self._last_flush) >= self.max_interval:
            await self.flush()

    async def flush(self) -> None:
        if not self._buffer:
            self._last_flush = time.monotonic()
            return
        chunks = dict(self._buffer)
        self._buffer.clear()
        self._last_flush = time.monotonic()
        await self.emit(
            {
                "type": "llm_delta",
                "chunks": chunks,
                "chars_total": self._chars_since_start,
            }
        )


class BookRunner:
    def __init__(self, book_id: str, store: BookStore):
        self.book_id = book_id
        self.store = store
        self.status = STATUS_IDLE
        self.error: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._stop_requested = False
        self._review_queue: Optional[asyncio.Queue] = None
        self._abort_finalizer: Optional[asyncio.Task] = None
        self._subscribers: List[asyncio.Queue] = []
        self._current_chapter: Optional[int] = None
        self._last_event_at = 0.0

    def _meta(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "error": self.error,
            "current_chapter": self._current_chapter,
            "task_alive": bool(self._task is not None and not self._task.done()),
        }

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(
            {"type": "runner_status", **self._meta(), "sent_at": _now()}
        )
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    async def _emit(self, event: Dict[str, Any]) -> None:
        event["sent_at"] = _now()
        self._last_event_at = event["sent_at"]
        self._append_event_log(event)
        for queue in list(self._subscribers):
            queue.put_nowait(dict(event))

    def _append_event_log(self, event: Dict[str, Any]) -> None:
        """Persist every event so retry reasons survive browser disconnects and restarts."""
        try:
            log_path = self.store.root / self.book_id / "runs" / "events.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if log_path.exists() and log_path.stat().st_size > 8_000_000:
                previous = log_path.with_suffix(".prev.jsonl")
                previous.unlink(missing_ok=True)
                log_path.rename(previous)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _set_status(self, status: str, error: Optional[str] = None) -> None:
        self.status = status
        self.error = error
        self._persist_status(status, error)

    def _persist_status(self, status: str, error: Optional[str]) -> None:
        try:
            book = self.store.load_book(self.book_id)
            book["status"] = status if status != STATUS_STOPPING else STATUS_PAUSED
            if error:
                book["last_error"] = error
            self.store.save_book(book)
        except Exception:
            pass

    async def start(self, job: str, config: Dict[str, Any]) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError(f"runner busy in status {self.status}")
        self._stop_requested = False
        self._pause_event.set()
        self._review_queue = None
        self.error = None
        profile_payload = {}
        for key in ("planner_profile", "writer_profile"):
            if isinstance(config.get(key), dict):
                profile_payload[key] = LlmProfile(**config[key])
        planner = profile_payload.get("planner_profile")
        writer = profile_payload.get("writer_profile")
        if job == "outline":
            self._set_status(STATUS_OUTLINE)
        elif job == "story":
            book = self.store.load_book(self.book_id)
            outline = book.get("outline") or {}
            if not outline.get("frozen"):
                raise RuntimeError("outline is not frozen yet")
            if writer is None:
                raise RuntimeError("writer_profile required")
            self._set_status(STATUS_WRITING)
        else:
            raise RuntimeError(f"unknown job {job!r}")
        self._task = asyncio.create_task(
            self._run_job(job, planner, writer, config),
            name=f"book-{self.book_id}-{job}",
        )

    def request_stop(self) -> None:
        self._stop_requested = True

    def request_abort(self) -> str:
        """Abort immediately: cancel the running task and its in-flight LLM call.

        Chapters and narrative state are checkpointed only after a chapter is
        accepted, so aborting mid-chapter loses nothing that was already saved;
        the interrupted chapter simply restarts from scratch on resume.
        """
        task = self._task
        if task is None or task.done():
            return f"nothing to abort (status {self.status})"
        task.cancel()
        self._abort_finalizer = asyncio.create_task(self._finalize_abort(task))
        return f"abort requested for job '{task.get_name()}'"

    async def _finalize_abort(self, task: asyncio.Task) -> None:
        try:
            await task
        except BaseException:
            pass
        if self._task is None or self._task.done():
            if self.status in (STATUS_OUTLINE, STATUS_WRITING):
                self._set_status(STATUS_PAUSED)
                await self._emit({"type": "runner_aborted"})

    def request_pause(self) -> None:
        self._pause_event.clear()

    def request_resume(self) -> None:
        self._pause_event.set()

    async def submit_review(self, decision: Dict[str, Any]) -> None:
        if self.status != STATUS_REVIEW or self._review_queue is None:
            raise RuntimeError("runner is not waiting for review")
        await self._review_queue.put(decision)
        self._review_queue = None
        self._set_status(STATUS_WRITING)

    def submit_review_nowait(self, decision: Dict[str, Any]) -> None:
        if self._review_queue is None:
            raise RuntimeError("runner is not waiting for review")
        self._review_queue.put_nowait(decision)
        self._review_queue = None
        self._set_status(STATUS_WRITING)

    async def _wait_if_paused(self) -> bool:
        if self._pause_event.is_set():
            return True
        self._set_status(STATUS_PAUSED)
        await self._pause_event.wait()
        if self._stop_requested:
            return False
        self._set_status(STATUS_WRITING)
        return True

    async def _run_job(
        self,
        job: str,
        planner_profile: Optional[LlmProfile],
        writer_profile: Optional[LlmProfile],
        config: Dict[str, Any],
    ) -> None:
        try:
            if job == "outline":
                await self._job_outline(planner_profile, config)
                self._set_status(STATUS_IDLE)
            else:
                await self._job_story(writer_profile, config)
                if self.status == STATUS_WRITING:
                    self._set_status(STATUS_DONE)
        except asyncio.CancelledError:
            self._set_status(STATUS_PAUSED)
            await self._emit({"type": "runner_aborted"})
        except Exception as exc:
            self._set_status(STATUS_ERROR, str(exc))
            await self._emit({"type": "runner_error", "error": str(exc)})
        finally:
            self._task = None

    async def _job_outline(
        self, planner_profile: Optional[LlmProfile], config: Dict[str, Any]
    ) -> None:
        book = self.store.load_book(self.book_id)
        if planner_profile is None:
            raise RuntimeError("planner_profile required")
        lang = book.get("language", "en")
        llm = LlmClient(planner_profile)
        relay = DeltaRelay(self._emit)
        await self._emit({"type": "llm_stream", "phase": "start", "label": "Planner"})

        async def save_stage(stage: str, outline: Dict[str, Any]) -> None:
            await relay.flush()
            current = self.store.load_book(self.book_id)
            current["outline"] = outline
            self.store.save_book(current)

        builder = OutlineBuilder(
            llm,
            emit=self._emit,
            temperature=book.get("run_settings", {}).get("temperature", 0.7),
            delta_sink=relay.feed,
            on_stage_done=save_stage,
        )
        try:
            outline = await builder.run(
                story_prompt=self._story_contract(book),
                target_words=int(book["target_words"]),
                lang=lang,
                codex=book.get("codex") or {},
                existing=book.get("outline") or None,
            )
        finally:
            await relay.flush()
            await self._emit({"type": "llm_stream", "phase": "end", "label": "Planner"})
        book = self.store.load_book(self.book_id)
        book["outline"] = outline
        self.store.save_book(book)
        await self._emit({"type": "outline_done", "chapters": len(outline.get("chapters", []))})

    @staticmethod
    def _story_contract(book: Dict[str, Any]) -> str:
        parts = []
        prompt = book.get("story_prompt", "").strip()
        if prompt:
            parts.append(prompt)
        notes = book.get("genre_notes", "").strip()
        if notes:
            parts.append(f"Authorial style and constraints: {notes}")
        return "\n\n".join(parts)

    async def _wait_review(self, chapter_id: int) -> Dict[str, Any]:
        self._review_queue = asyncio.Queue(maxsize=1)
        self._set_status(STATUS_REVIEW)
        await self._emit({"type": "review_pending", "chapter_id": chapter_id})
        decision = await self._review_queue.get()
        return decision

    async def _job_story(
        self, writer_profile: Optional[LlmProfile], config: Dict[str, Any]
    ) -> None:
        book = self.store.load_book(self.book_id)
        if writer_profile is None:
            raise RuntimeError("writer_profile required")
        run_settings = book.get("run_settings", {})
        lang = book.get("language", "en")
        outline = book["outline"]
        chapters_plan = sorted(outline["chapters"], key=lambda c: c["id"])
        story_contract = self._story_contract(book)
        codex_text = codex_context(book.get("codex") or {})
        outline_json = outline_to_prompt_json(chapters_plan)
        state = self.store.load_state(self.book_id)
        written = self.store.written_chapters(self.book_id)
        written_map = {c["id"]: c for c in written}
        search_index = ChapterSearchIndex()
        llm = LlmClient(writer_profile)
        writer = ChapterWriter(
            llm,
            emit=self._emit,
            temperature=float(run_settings.get("temperature", 0.7)),
            max_turns=int(run_settings.get("max_turns", 50)),
            dynamic_tokens=bool(run_settings.get("dynamic_tokens", True)),
            hard_max_output_tokens=int(run_settings.get("hard_max_output_tokens", 32768)),
        )
        max_attempts = int(run_settings.get("max_attempts_per_chapter", 2))
        mode = run_settings.get("mode", "auto")
        relay = DeltaRelay(self._emit)
        try:
            idx = 0
            while idx < len(chapters_plan):
                chapter = chapters_plan[idx]
                ch_id = int(chapter["id"])
                self._current_chapter = ch_id
                record = written_map.get(ch_id)
                if record is not None and record.get("status") == "accepted":
                    idx += 1
                    continue
                if not await self._wait_if_paused():
                    self._set_status(STATUS_STOPPED if self._stop_requested else STATUS_PAUSED)
                    return
                if self._stop_requested:
                    self._set_status(STATUS_STOPPED)
                    await self._emit({"type": "runner_stopped"})
                    return
                await self._emit(
                    {
                        "type": "chapter_started",
                        "chapter_id": ch_id,
                        "name": chapter["name"],
                        "target": chapter["word_count"],
                    }
                )
                author_notes = self.store.pop_author_notes(self.book_id)
                result = None
                for attempt in range(1, max_attempts + 1):
                    await self._emit(
                        {
                            "type": "llm_stream",
                            "phase": "start",
                            "label": f"Chapter {ch_id} (attempt {attempt})",
                        }
                    )
                    try:
                        result = await writer.run(
                            chapter,
                            [c for c in written if c["id"] < ch_id],
                            state,
                            story_contract,
                            outline_json,
                            lang=lang,
                            codex=codex_text,
                            author_notes=author_notes,
                            visible_outline=None,
                            search_index=search_index,
                            delta_sink=relay.feed,
                        )
                        break
                    except (ChapterIncomplete, LlmError) as exc:
                        await self._emit(
                            {
                                "type": "chapter_retry",
                                "chapter_id": ch_id,
                                "attempt": attempt,
                                "error": str(exc),
                            }
                        )
                        continue
                    finally:
                        await relay.flush()
                        await self._emit(
                            {
                                "type": "llm_stream",
                                "phase": "end",
                                "label": f"Chapter {ch_id} (attempt {attempt})",
                            }
                        )
                if result is None:
                    raise RuntimeError(
                        f"chapter {ch_id} failed after {max_attempts} attempts"
                    )
                chapter_record = {
                    "id": ch_id,
                    "name": result.title,
                    "content": result.content,
                    "target": int(chapter["word_count"]),
                    "written_words": result.word_count,
                    "attempts": result.write_attempts,
                    "tool_counts": result.tool_counts,
                    "update_info": result.update_info,
                    "update_errors": result.update_errors,
                    "total_turns": result.total_turns,
                    "usage": result.usage_dict(),
                    "status": "accepted",
                    "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                self.store.save_chapter(self.book_id, chapter_record)
                self.store.save_state(self.book_id, state)
                self.store.save_state_snapshot(self.book_id, ch_id, state)
                self.store.append_metrics(
                    self.book_id,
                    {
                        "chapter_id": ch_id,
                        "word_count": result.word_count,
                        "target": int(chapter["word_count"]),
                        "usage": result.usage_dict(),
                        "tool_counts": result.tool_counts,
                        "update_info": result.update_info,
                        "attempts": result.write_attempts,
                        "at": chapter_record["accepted_at"],
                    },
                )
                written_map[ch_id] = chapter_record
                updated_written = [c for c in written if c["id"] != ch_id]
                updated_written.append(chapter_record)
                updated_written.sort(key=lambda c: c["id"])
                written = updated_written
                for cid in search_index._dirty_ids.copy():
                    dirty_record = written_map.get(cid)
                    if dirty_record is not None:
                        self.store.save_chapter(self.book_id, dirty_record)
                    search_index._dirty_ids.discard(cid)
                await self._emit(
                    {
                        "type": "chapter_done",
                        "chapter_id": ch_id,
                        "name": result.title,
                        "word_count": result.word_count,
                        "target": int(chapter["word_count"]),
                        "usage": result.usage_dict(),
                        "update_info": result.update_info,
                    }
                )
                if mode == "single":
                    self._set_status(STATUS_STEP)
                    await self._emit(
                        {"type": "chapter_step_pause", "chapter_id": ch_id}
                    )
                    self._current_chapter = None
                    return
                if mode == "review" and self._has_next_chapter(chapters_plan, ch_id):
                    decision = await self._wait_review(ch_id)
                    action = decision.get("action", "approve")
                    if action == "edit":
                        content = str(decision.get("content") or "")
                        if content.strip():
                            record = written_map[ch_id]
                            record["content"] = content
                            record["written_words"] = count_words(content)
                            self.store.save_chapter(self.book_id, record)
                            search_index.mark_dirty(ch_id)
                            await self._emit(
                                {"type": "chapter_edited", "chapter_id": ch_id}
                            )
                        idx += 1
                    elif action == "rewrite":
                        stale = written_map.pop(ch_id, None)
                        if stale is not None:
                            stale["status"] = "rewritten"
                            self.store.save_chapter(self.book_id, stale)
                        written = [c for c in written if c["id"] != ch_id]
                        state = self._revert_state(ch_id)
                        self.store.save_state(self.book_id, state)
                        search_index.mark_dirty(ch_id)
                        note = str(decision.get("note") or "")
                        if note.strip():
                            self.store.set_author_notes(self.book_id, note)
                        await self._emit(
                            {"type": "chapter_rewrite_requested", "chapter_id": ch_id}
                        )
                    else:
                        note = str(decision.get("note") or "")
                        if note.strip():
                            self.store.set_author_notes(self.book_id, note)
                            await self._emit(
                                {"type": "author_note_added", "chapter_id": ch_id}
                            )
                        idx += 1
                else:
                    idx += 1
            self._current_chapter = None
        finally:
            search_index.close()

    def _revert_state(self, ch_id: int) -> NarrativeState:
        history_dir = self.store.root / self.book_id / "state" / "history"
        prev = None
        for prev_id in range(ch_id - 1, -1, -1):
            candidate = history_dir / f"ch{prev_id:04d}.json"
            if candidate.exists():
                prev = candidate
                break
        if prev is None:
            return NarrativeState()
        return NarrativeState.from_dict(json.loads(prev.read_text(encoding="utf-8")))

    @staticmethod
    def _has_next_chapter(chapters_plan: List[Dict[str, Any]], ch_id: int) -> bool:
        return any(int(c["id"]) > ch_id for c in chapters_plan)


class RunnerRegistry:
    def __init__(self, store: BookStore):
        self.store = store
        self._runners: Dict[str, BookRunner] = {}

    def get(self, book_id: str) -> BookRunner:
        if book_id not in self._runners:
            self._runners[book_id] = BookRunner(book_id, self.store)
        return self._runners[book_id]

    def status(self, book_id: str) -> Dict[str, Any]:
        runner = self.get(book_id)
        return runner._meta()