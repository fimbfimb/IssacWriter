from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ..llm.provider import LlmClient, ToolCall, Usage, sanitize_tool_call_arguments
from .state import NarrativeState
from .search_index import ChapterSearchIndex
from .prompts import (
    MAX_CORRECT_PER_CHAPTER,
    MAX_READ,
    MAX_SEARCH,
    MAX_TURNS_PER_CHAPTER,
    SEARCH_MAX_HITS,
    SYSTEM_CHAPTER,
    chapter_prompt,
)
from .tools import TOOLS, count_words, fuzzy_locate, length_limits, parse_tool_arguments

EmitFn = Callable[[Dict[str, Any]], Awaitable[None]]

ADAPTIVE_LENGTH_MIN_IMPROVEMENT = 0.10


class ChapterIncomplete(Exception):
    pass


def is_done_signal(text: str) -> bool:
    return text.strip().casefold().strip("\"'`") == "done"


@dataclass
class ChapterResult:
    chapter_id: int
    title: str
    content: str
    word_count: int
    usage: Usage
    tool_counts: Dict[str, int] = field(default_factory=dict)
    update_info: Dict[str, Any] = field(default_factory=dict)
    update_errors: List[str] = field(default_factory=list)
    total_turns: int = 0
    write_attempts: int = 1

    def usage_dict(self) -> Dict[str, Any]:
        return self.usage.to_dict()


class ChapterWriter:
    def __init__(
        self,
        llm: LlmClient,
        emit: Optional[EmitFn] = None,
        temperature: float = 0.7,
        max_turns: int = MAX_TURNS_PER_CHAPTER,
        dynamic_tokens: bool = True,
        hard_max_output_tokens: int = 32768,
    ):
        self.llm = llm
        self.emit = emit
        self.temperature = temperature
        self.max_turns = max_turns
        self.dynamic_tokens = dynamic_tokens
        self.hard_max_output_tokens = hard_max_output_tokens

    async def _emit(self, event: Dict[str, Any]) -> None:
        if self.emit is not None:
            await self.emit(event)

    async def _relay_delta(self, kind: str, text: str, delta_sink: Any) -> None:
        if delta_sink is not None:
            await delta_sink(kind, text)
    def _per_call_tokens(self, target: int) -> int:
        if not self.dynamic_tokens:
            return self.hard_max_output_tokens
        return min(self.hard_max_output_tokens, max(1024, math.ceil(target * 1.38) + 1536))

    async def run(
        self,
        chapter: Dict[str, Any],
        written_chapters: List[Dict[str, Any]],
        state: NarrativeState,
        story_prompt: str,
        outline_json: str,
        lang: str = "en",
        codex: str = "",
        author_notes: str = "",
        visible_outline: Optional[str] = None,
        search_index: Optional[ChapterSearchIndex] = None,
        delta_sink: Optional[Any] = None,
    ) -> ChapterResult:
        ch_id = int(chapter["id"])
        target = int(chapter["word_count"])
        min_words, max_words = length_limits(target)
        draft: Optional[Dict[str, Any]] = None
        write_accepted = False
        current_corrected = False
        correct_used = 0
        read_used = 0
        search_used = 0
        update_done = False
        update_info: Dict[str, Any] = {}
        update_errors: List[str] = []
        usage = Usage(price_in_per_m=self.llm.profile.price_in_per_m, price_out_per_m=self.llm.profile.price_out_per_m)
        tool_counts: Dict[str, int] = {}
        completed = len(written_chapters)
        full_rejection: Optional[Dict[str, Any]] = None

        user_prompt = chapter_prompt(
            lang,
            outline_json,
            state.to_prompt_json(),
            ch_id,
            chapter["name"],
            chapter["description"],
            target,
            completed,
            story_prompt,
            codex,
            author_notes,
            visible_outline,
        )
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_CHAPTER[lang]},
            {"role": "user", "content": user_prompt},
        ]
        per_call_max_tokens = self._per_call_tokens(target)
        relay = (
            (lambda kind, text: self._relay_delta(kind, text, delta_sink))
            if delta_sink is not None
            else None
        )

        total_turns = 0
        while total_turns < self.max_turns:
            total_turns += 1
            response = await self.llm.chat(
                messages, tools=TOOLS,
                temperature=self.temperature,
                max_output_tokens=per_call_max_tokens,
                on_delta=relay,
            )
            usage.prompt_tokens += response.usage.prompt_tokens
            usage.completion_tokens += response.usage.completion_tokens
            if response.thinking_ignored:
                await self._emit({"type": "thinking_ignored", "chapter_id": ch_id})
            await self._emit(
                {
                    "type": "llm_usage",
                    "chapter_id": ch_id,
                    "call": total_turns,
                    "usage": response.usage.to_dict(),
                }
            )

            if not response.tool_calls:
                text = response.content
                if is_done_signal(text):
                    if not write_accepted:
                        messages.append({"role": "assistant", "content": text})
                        messages.append({
                            "role": "user",
                            "content": (
                                "You cannot finish before the chapter is written. Continue: "
                                "use write to submit the chapter, then update, then output DONE."
                            ),
                        })
                        continue
                    if not update_done:
                        messages.append({"role": "assistant", "content": text})
                        messages.append({
                            "role": "user",
                            "content": (
                                "You must maintain the narrative state before finishing: make "
                                "exactly one update call with the four arrays now, then output DONE."
                            ),
                        })
                        continue
                    break
                messages.append({"role": "assistant", "content": text or ""})
                if response.finish_reason == "length":
                    note = (
                        "Your previous response hit the output token limit and was not accepted. "
                        "Continue the tool protocol now; make tool calls instead of narrating."
                    )
                else:
                    note = (
                        "Any text other than a tool call or DONE does not finish the chapter. "
                        "Continue with the execution order: read/search if needed, write, "
                        "correct if needed, exactly one update, then output DONE."
                    )
                messages.append({"role": "user", "content": note})
                continue

            assistant_index = len(messages)
            tool_call_meta: List[Dict[str, Any]] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": sanitize_tool_call_arguments(call.arguments),
                    },
                }
                for call in response.tool_calls
            ]
            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": tool_call_meta,
                    "content": response.content or "",
                }
            )
            for call, sanitized_meta in zip(response.tool_calls, tool_call_meta):
                name = sanitized_meta["function"]["name"]
                tool_counts[name] = tool_counts.get(name, 0) + 1
                try:
                    args = parse_tool_arguments(sanitized_meta["function"]["arguments"])
                except Exception:
                    args = {}
                payload, extras = await self._execute_tool(
                    name,
                    args,
                    ch_id=ch_id,
                    target=target,
                    min_words=min_words,
                    max_words=max_words,
                    written_chapters=written_chapters,
                    draft=draft,
                    state=state,
                    flags={
                        "write_accepted": write_accepted,
                        "current_corrected": current_corrected,
                        "correct_used": correct_used,
                        "read_used": read_used,
                        "search_used": search_used,
                        "update_done": update_done,
                    },
                    search_index=search_index,
                )

                if name == "write":
                    write_accepted = extras.get("write_accepted", write_accepted)
                    if extras.get("accepted"):
                        draft = extras["draft"]
                        write_accepted = True
                    else:
                        word_count = int(extras.get("word_count", 0) or 0)
                        if word_count > 0:
                            full_rejection = self._handle_rejection(
                                messages,
                                assistant_index,
                                word_count,
                                target,
                                min_words,
                                max_words,
                                full_rejection,
                            )
                elif name == "correct":
                    correct_used = extras.get("correct_used", correct_used)
                    if extras.get("current_corrected"):
                        current_corrected = True
                    for cid in extras.get("edited_ids", []) or []:
                        if search_index is not None:
                            search_index.mark_dirty(int(cid))
                elif name == "read" and extras.get("charged"):
                    read_used = extras.get("read_used", read_used)
                elif name == "search" and extras.get("charged"):
                    search_used = extras.get("search_used", search_used)
                elif name == "update" and extras.get("done"):
                    update_done = True
                    update_info = extras.get("info", {})
                    update_errors = extras.get("errors", [])

                await self._emit(
                    {
                        "type": "tool_event",
                        "chapter_id": ch_id,
                        "tool": name,
                        "ok": bool(payload.get("ok")),
                        "summary": _tool_summary(name, payload, extras),
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": json.dumps(payload, ensure_ascii=False),
                    }
                )

        if draft is None:
            raise ChapterIncomplete(f"chapter {ch_id} was not written after {total_turns} turns")
        if not update_done:
            raise ChapterIncomplete(f"chapter {ch_id} finished without a successful update call")
        return ChapterResult(
            chapter_id=ch_id,
            title=draft.get("title") or chapter["name"],
            content=draft["content"],
            word_count=count_words(draft["content"]),
            usage=usage,
            tool_counts=tool_counts,
            update_info=update_info,
            update_errors=update_errors,
            total_turns=total_turns,
            write_attempts=tool_counts.get("write", 1),
        )

    def _handle_rejection(
        self,
        messages: List[Dict[str, Any]],
        assistant_index: int,
        word_count: int,
        target: int,
        min_words: int,
        max_words: int,
        full_rejection: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        distance = min(abs(word_count - min_words), abs(word_count - max_words)) / max(1, target)
        if full_rejection is None:
            return {
                "assistant_index": assistant_index,
                "count": word_count,
                "distance": distance,
            }
        improvement = (full_rejection["distance"] - distance) / max(0.000001, full_rejection["distance"])
        if improvement >= ADAPTIVE_LENGTH_MIN_IMPROVEMENT:
            self._compact_rejection(messages, full_rejection["assistant_index"])
            return {"assistant_index": assistant_index, "count": word_count, "distance": distance}
        self._compact_rejection(messages, assistant_index)
        return full_rejection

    @staticmethod
    def _compact_rejection(messages: List[Dict[str, Any]], assistant_index: int) -> None:
        if assistant_index >= len(messages):
            return
        assistant_msg = messages[assistant_index]
        tool_calls = assistant_msg.get("tool_calls") or []

        def _call_name(tc: Dict[str, Any]) -> str:
            function = tc.get("function")
            return tc.get("name") or (function or {}).get("name") or ""

        write_call = next((tc for tc in tool_calls if _call_name(tc) == "write"), None)
        if write_call is None:
            return
        args = parse_tool_arguments(write_call.get("arguments", "{}"))
        content = str(args.get("content", ""))
        compact_args = {
            "chapter_id": args.get("chapter_id"),
            "title": args.get("title"),
            "word_count": count_words(content),
            "note": "rejected draft; its text was removed from history, word count retained",
        }
        compact_json = json.dumps(compact_args, ensure_ascii=False)
        if write_call.get("function") is not None:
            write_call["function"]["arguments"] = compact_json
        else:
            write_call["arguments"] = compact_json
        for j in range(assistant_index + 1, min(assistant_index + 4, len(messages))):
            msg = messages[j]
            if msg.get("role") == "tool":
                try:
                    tool_content = json.loads(msg["content"])
                except Exception:
                    tool_content = {}
                tool_content["compacted"] = True
                tool_content["note"] = "rejected draft text removed from history; word count retained"
                msg["content"] = json.dumps(tool_content, ensure_ascii=False)
                break

    async def _execute_tool(
        self,
        name: str,
        args: Dict[str, Any],
        *,
        ch_id: int,
        target: int,
        min_words: int,
        max_words: int,
        written_chapters: List[Dict[str, Any]],
        draft: Optional[Dict[str, Any]],
        state: NarrativeState,
        flags: Dict[str, Any],
        search_index: Optional[ChapterSearchIndex],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if name == "read":
            return self._tool_read(args, ch_id, written_chapters, draft, flags["read_used"])
        if name == "search":
            return self._tool_search(args, written_chapters, flags["search_used"], search_index)
        if name == "correct":
            return self._tool_correct(
                args, ch_id, written_chapters, draft, flags["correct_used"],
                flags["write_accepted"], search_index is not None,
            )
        if name == "write":
            return self._tool_write(
                args, ch_id, target, min_words, max_words,
                flags.get("write_accepted", False), flags.get("current_corrected", False),
            )
        if name == "update":
            return self._tool_update(args, state, flags.get("write_accepted", False), flags.get("update_done", False))
        return {"ok": False, "error": f"unknown tool {name!r}"}, {}

    @staticmethod
    def _tool_read(
        args: Dict[str, Any], ch_id: int, written_chapters: List[Dict[str, Any]],
        draft: Optional[Dict[str, Any]], read_used: int,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        requested = args.get("chapter_id")
        if not isinstance(requested, int):
            return {"ok": False, "error": "chapter_id must be an integer"}, {}
        if requested > ch_id:
            return {"ok": False, "error": "chapter_id must not be a future chapter"}, {}
        if requested == ch_id:
            chapter: Optional[Dict[str, Any]] = draft
            missing = "current chapter is not available until write succeeds"
        else:
            chapter = next((c for c in written_chapters if c["id"] == requested), None)
            missing = f"chapter {requested} not written"
        if chapter is None or not chapter.get("content"):
            return {"ok": False, "error": missing}, {}
        if read_used >= MAX_READ:
            return {
                "ok": False,
                "error": f"read budget exhausted ({MAX_READ} reads); proceed without further reads",
            }, {}
        return (
            {"ok": True, "chapter_id": requested, "name": chapter.get("name", ""), "content": chapter["content"]},
            {"charged": True, "read_used": read_used + 1},
        )

    @staticmethod
    def _tool_search(
        args: Dict[str, Any], written_chapters: List[Dict[str, Any]], search_used: int,
        search_index: Optional[ChapterSearchIndex],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return {"ok": False, "error": "query must be a non-empty string"}, {}
        if search_used >= MAX_SEARCH:
            return {
                "ok": False,
                "error": f"search budget exhausted ({MAX_SEARCH} searches); proceed from the state",
            }, {}
        if search_index is None:
            return {"ok": False, "error": "search index unavailable"}, {}
        hits = search_index.search(written_chapters, query, SEARCH_MAX_HITS)
        return (
            {
                "ok": True,
                "query": query,
                "hit_count": len(hits),
                "hits": hits,
                "note": "" if hits else "no matches; refine wording or rely on the narrative state",
            },
            {"charged": True, "search_used": search_used + 1},
        )

    @staticmethod
    def _tool_correct(
        args: Dict[str, Any], ch_id: int, written_chapters: List[Dict[str, Any]],
        draft: Optional[Dict[str, Any]], correct_used: int, write_accepted: bool,
        has_index: bool,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if correct_used >= MAX_CORRECT_PER_CHAPTER:
            return {
                "ok": False,
                "error": f"correct budget exhausted ({MAX_CORRECT_PER_CHAPTER}); proceed",
            }, {}
        requested = args.get("chapter_id")
        if not isinstance(requested, int) or requested > ch_id:
            return {"ok": False, "error": "chapter_id must reference a written chapter"}, {}
        old_text = str(args.get("old_text") or "")
        new_text = str(args.get("new_text") or "")
        if not old_text:
            return {"ok": False, "error": "old_text must be non-empty"}, {}
        if requested == ch_id:
            if not write_accepted:
                return {"ok": False, "error": "current chapter is not written yet"}, {}
            if draft is None:
                return {"ok": False, "error": "internal: no draft"}, {}
            span = fuzzy_locate(draft["content"], old_text)
            if span is None:
                return {
                    "ok": False,
                    "error": "old_text not found in the current chapter; copy the exact text",
                }, {}
            draft["content"] = (
                draft["content"][: span[0]] + new_text + draft["content"][span[1] :]
            )
            return (
                {"ok": True, "chapter_id": requested, "applied": True},
                {"correct_used": correct_used + 1, "current_corrected": True, "edited_ids": [requested]},
            )
        chapter = next((c for c in written_chapters if c["id"] == requested), None)
        if chapter is None or not chapter.get("content"):
            return {"ok": False, "error": f"chapter {requested} not written"}, {}
        span = fuzzy_locate(chapter["content"], old_text)
        if span is None:
            return {
                "ok": False,
                "error": f"old_text not found in chapter {requested}; copy the exact text",
            }, {}
        chapter["content"] = chapter["content"][: span[0]] + new_text + chapter["content"][span[1] :]
        return (
            {"ok": True, "chapter_id": requested, "applied": True},
            {"correct_used": correct_used + 1, "edited_ids": [requested]},
        )

    @staticmethod
    def _tool_write(
        args: Dict[str, Any], ch_id: int, target: int, min_words: int, max_words: int,
        write_accepted: bool, current_corrected: bool,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if write_accepted:
            return {
                "ok": False,
                "error": "the single successful write for this chapter is used; use correct for further changes",
            }, {}
        requested = args.get("chapter_id")
        if not isinstance(requested, int) or requested != ch_id:
            return {"ok": False, "error": f"chapter_id must be {ch_id}"}, {}
        title = str(args.get("title") or "").strip()
        content = str(args.get("content") or "").strip()
        if not content:
            return {"ok": False, "error": "content must be non-empty"}, {}
        if current_corrected:
            return {
                "ok": False,
                "error": "the current chapter was already corrected and is final; use correct only",
            }, {}
        word_count = count_words(content)
        within = min_words <= word_count <= max_words
        if not within:
            return (
                {
                    "ok": False,
                    "error": "length out of range; rewrite the complete chapter",
                    "word_count": word_count,
                    "target": target,
                    "accepted_range": [min_words, max_words],
                },
                {"word_count": word_count, "accepted": False},
            )
        return (
            {
                "ok": True,
                "word_count": word_count,
                "target": target,
                "accepted_range": [min_words, max_words],
            },
            {"accepted": True, "draft": {"title": title or f"Chapter {ch_id}", "content": content}, "word_count": word_count},
        )

    @staticmethod
    def _tool_update(
        args: Dict[str, Any], state: NarrativeState, write_accepted: bool, update_done: bool
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if update_done:
            return {"ok": False, "error": "update was already submitted for this chapter"}, {}
        if not write_accepted:
            return {"ok": False, "error": "update is only allowed after a successful write"}, {}
        candidate = state.snapshot()
        info, errors = candidate.apply_narrative_ops(args)
        if errors:
            return (
                {
                    "ok": False,
                    "errors": errors[:8],
                    "note": (
                        "The update was rejected atomically; fix the reported issues and "
                        "resubmit the complete update once."
                    ),
                },
                {},
            )
        state.commit_snapshot(candidate)
        return (
            {
                "ok": True,
                "applied": info,
            },
            {"done": True, "info": info, "errors": []},
        )


def _tool_summary(name: str, payload: Dict[str, Any], extras: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"ok": bool(payload.get("ok"))}
    if name == "read":
        summary["chapter_id"] = payload.get("chapter_id")
    elif name == "search":
        summary["query"] = payload.get("query")
        summary["hit_count"] = payload.get("hit_count")
    elif name == "write":
        summary["word_count"] = payload.get("word_count", extras.get("word_count"))
        summary["accepted_range"] = payload.get("accepted_range")
    elif name == "correct":
        summary["chapter_id"] = payload.get("chapter_id")
    elif name == "update":
        summary["applied"] = payload.get("ok")
        if payload.get("errors"):
            summary["errors"] = payload.get("errors")
    return summary