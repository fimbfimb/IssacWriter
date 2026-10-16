from __future__ import annotations

import json
import math
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from ..llm.provider import LlmClient, LlmError
from .prompts import (
    SYSTEM_OUTLINE,
    codex_context,
    outline_acts_prompt,
    outline_chapters_prompt,
    outline_detail_prompt,
    outline_premise_prompt,
    recommend_chapter_count,
)

EmitFn = Callable[[Dict[str, Any]], Awaitable[None]]

_JSON_RETRY_FEEDBACK = (
    "Your previous answer was not valid JSON in the required format. "
    "Output ONLY the required artifact, nothing else."
)


def normalize_outline_word_targets(
    chapters: List[Dict[str, Any]], total_target: int
) -> List[Dict[str, Any]]:
    if not chapters or total_target <= 0:
        return chapters
    raw = [max(1, int(item.get("word_count") or 1)) for item in chapters]
    raw_total = sum(raw)
    exact = [value * total_target / raw_total for value in raw]
    scaled = [max(1, math.floor(value)) for value in exact]
    difference = total_target - sum(scaled)
    order = sorted(
        range(len(chapters)),
        key=lambda i: exact[i] - math.floor(exact[i]),
        reverse=difference > 0,
    )
    step = 1 if difference > 0 else -1
    for offset in range(abs(difference)):
        index = order[offset % len(order)]
        if step < 0 and scaled[index] <= 1:
            continue
        scaled[index] += step
    normalized = [dict(item) for item in chapters]
    for item, target in zip(normalized, scaled):
        item["word_count"] = target
    return normalized


def sanitize_chapters(raw: Any, target_words: int) -> List[Dict[str, Any]]:
    if isinstance(raw, dict):
        for key in ("chapters", "outline"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
    if not isinstance(raw, list) or not raw:
        raise ValueError("outline is empty")
    chapters: List[Dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("title") or f"Chapter {i}").strip()
        description = str(item.get("description") or item.get("summary") or "").strip()
        try:
            word_count = int(item.get("word_count") or item.get("words") or 0)
        except (TypeError, ValueError):
            word_count = 0
        if word_count <= 0:
            word_count = max(300, round(target_words / max(1, len(raw))))
        chapters.append(
            {
                "id": i,
                "name": name[:200],
                "description": description,
                "word_count": word_count,
            }
        )
    if not chapters:
        raise ValueError("outline is empty")
    return normalize_outline_word_targets(chapters, target_words)


def _extract_act_sections(text: str) -> List[Dict[str, str]]:
    acts: List[Dict[str, str]] = []
    pattern = re.compile(
        r"(?ms)^(?:Act|PART|Volume|Volume|\*\*)\s*[0-9IVX]+[\.\):]?\s*[—–-]?\s*(.+?)"
        r"(?=^\s*(?:Act|PART|Volume|Volume)\s*[0-9IVX]+[\.\):]?\s*[—–-]?\s*|\Z)"
    )
    if re.search(r"^\s*(?:Act|PART|Volume)\s*[0-9IVX]+\s*", text, re.MULTILINE):
        for match in pattern.finditer(text):
            body = match.group(0).strip()
            first_line, _, rest = body.partition("\n")
            title = re.sub(r"^\s*(?:Act|PART|Volume)\s*[0-9IVX]+[\.\):]?\s*[—–-]*\s*", "", first_line).strip(" *#")
            acts.append({"title": title or first_line.strip(" *#"), "description": rest.strip()})
    return acts


class OutlineBuilder:
    def __init__(
        self,
        llm: LlmClient,
        emit: Optional[EmitFn] = None,
        temperature: float = 0.7,
        max_stage_tokens: int = 16384,
    ):
        self.llm = llm
        self.emit = emit
        self.temperature = temperature
        self.max_stage_tokens = max_stage_tokens

    async def _emit(self, event: Dict[str, Any]) -> None:
        if self.emit is not None:
            await self.emit(event)

    async def _stage_text(
        self, lang: str, prompt: str, stage_name: str
    ) -> str:
        messages = [
            {"role": "system", "content": SYSTEM_OUTLINE[lang]},
            {"role": "user", "content": prompt},
        ]
        for attempt in range(3):
            response = await self.llm.chat(
                messages, temperature=self.temperature, max_output_tokens=self.max_stage_tokens
            )
            await self._emit(
                {
                    "type": "planner_usage",
                    "stage": stage_name,
                    "attempt": attempt,
                    "usage": response.usage.to_dict(),
                }
            )
            text = response.content.strip()
            if text:
                return text
            messages = messages[:2] + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": _JSON_RETRY_FEEDBACK},
            ]
        raise LlmError(f"planner stage {stage_name} produced no usable output")

    async def run(
        self,
        story_prompt: str,
        target_words: int,
        lang: str,
        codex: Dict[str, Any],
        existing: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        codex_text = codex_context(codex)
        detailed = target_words >= 10000
        outline: Dict[str, Any] = dict(existing or {})

        if not outline.get("premise"):
            await self._emit({"type": "planner_stage_started", "stage": "premise"})
            premise = await self._stage_text(
                lang,
                outline_premise_prompt(lang, story_prompt, target_words, codex_text),
                "premise",
            )
            outline["premise"] = premise
            await self._emit({"type": "planner_stage_done", "stage": "premise", "content": premise})

        context_parts: List[str] = []
        if detailed:
            if not outline.get("synopsis"):
                await self._emit({"type": "planner_stage_started", "stage": "synopsis"})
                synopsis_target = max(1000, min(2500, target_words // 20))
                synopsis = await self._stage_text(
                    lang,
                    outline_detail_prompt(
                        lang, story_prompt, outline["premise"], synopsis_target, codex_text
                    ),
                    "synopsis",
                )
                outline["synopsis"] = synopsis
                await self._emit(
                    {"type": "planner_stage_done", "stage": "synopsis", "content": synopsis}
                )
            context_parts.append(outline["synopsis"])
            if not outline.get("acts"):
                await self._emit({"type": "planner_stage_started", "stage": "acts"})
                acts = await self._stage_text(
                    lang,
                    outline_acts_prompt(
                        lang, story_prompt, outline["premise"], outline["synopsis"],
                        target_words, codex_text,
                    ),
                    "acts",
                )
                outline["acts"] = acts
                await self._emit({"type": "planner_stage_done", "stage": "acts", "content": acts})
                outline["act_sections"] = _extract_act_sections(acts)
            context_parts.append(outline["acts"])
        context_parts.append(outline["premise"])
        context = "\n\n".join(context_parts)

        rec = recommend_chapter_count(target_words)
        per_chapter = target_words // rec if rec else target_words
        await self._emit({"type": "planner_stage_started", "stage": "chapters"})
        chapters_raw = await self._stage_text(
            lang,
            outline_chapters_prompt(
                lang, story_prompt, context, target_words, rec, per_chapter, codex_text
            ),
            "chapters",
        )
        attempts = 0
        last_error: Exception | None = None
        while attempts < 3:
            attempts += 1
            try:
                parsed = json.loads(chapters_raw) if chapters_raw.lstrip().startswith("[") else _safe_json(chapters_raw)
                chapters = sanitize_chapters(parsed, target_words)
                chapters_raw = json.dumps(chapters, ensure_ascii=False, indent=2)
                break
            except Exception as exc:
                last_error = exc
                messages = [
                    {"role": "system", "content": SYSTEM_OUTLINE[lang]},
                    {"role": "user", "content": outline_chapters_prompt(
                        lang, story_prompt, context, target_words, rec, per_chapter, codex_text
                    )},
                    {"role": "assistant", "content": chapters_raw},
                    {"role": "user", "content": (
                        "Your previous answer was not a valid JSON array of chapters. "
                        "Output ONLY the JSON array in the required format."
                        if lang == "en" else
                        "Deine vorherige Antwort war kein gültiges JSON-Array von Kapiteln. "
                        "Gib NUR das JSON-Array im verlangten Format aus."
                    )},
                ]
                response = await self.llm.chat(
                    messages, temperature=self.temperature, max_output_tokens=self.max_stage_tokens
                )
                chapters_raw = response.content
        else:
            raise LlmError(f"chapter outline could not be parsed: {last_error}")

        outline["chapters"] = json.loads(chapters_raw)
        outline["target_words"] = target_words
        outline["frozen"] = False
        await self._emit(
            {
                "type": "planner_stage_done",
                "stage": "chapters",
                "content": outline["chapters"],
            }
        )
        return outline


def _safe_json(text: str) -> Any:
    from ..llm.provider import extract_json

    return extract_json(text)


def outline_to_prompt_json(chapters: Sequence[Dict[str, Any]]) -> str:
    compact = [
        {
            "id": ch.get("id"),
            "name": ch.get("name"),
            "description": ch.get("description"),
            "word_count": ch.get("word_count"),
        }
        for ch in chapters
    ]
    return json.dumps(compact, ensure_ascii=False, indent=2)