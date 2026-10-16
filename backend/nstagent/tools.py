from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Optional, Tuple

from .prompts import (
    MAX_CORRECT_PER_CHAPTER,
    MAX_READ,
    MAX_SEARCH,
    SEARCH_MAX_HITS,
    WORD_COUNT_TOLERANCE,
    WORD_COUNT_TOLERANCE_PERCENT,
)

_ASCII_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'_-]*")
_CJK = re.compile(
    "[㐀-䶿一-鿿豈-﫿]|[\U00020000-\U0002ffff]"
)

TOOLS: list[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": (
                "Read a written chapter in full. Prior chapters are always "
                "available; the current chapter becomes available after a "
                "successful write. Counts against "
                f"the {MAX_READ}-call read budget, but only "
                "when the call succeeds — rejected calls (bad chapter_id, "
                "unknown chapter) are free."
            ),
            "parameters": {
                "type": "object",
                "properties": {"chapter_id": {"type": "integer"}},
                "required": ["chapter_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "Search prior chapters using one or more keywords or a short "
                "phrase. The query is segmented into terms, sentence windows "
                "matching any term are ranked by BM25 relevance, and every "
                f"valid executed search counts against the {MAX_SEARCH}-call "
                f"search budget; at most {SEARCH_MAX_HITS} ranked windows "
                "are returned."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "correct",
            "description": (
                "Fix a factual or continuity error in an already-written "
                "chapter (current or prior). Style polishing is not a "
                f"valid use. At most {MAX_CORRECT_PER_CHAPTER} correct calls "
                "per chapter. Correcting the CURRENT chapter is final: once "
                "you correct it you can no longer rewrite it with write, so "
                "settle the chapter's length first and correct last."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chapter_id": {"type": "integer"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["chapter_id", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": (
                "Write the full text of the current chapter. Only one "
                "successful write is allowed per chapter. A draft rejected "
                "for length does not consume that opportunity, so rewrite it "
                "until the tool returns ok=true. After that, use correct for "
                "any changes; another write will be rejected. The tool returns "
                f"ok=true only when the submitted chapter is within "
                f"±{WORD_COUNT_TOLERANCE_PERCENT}% of its target length."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chapter_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["chapter_id", "title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update",
            "description": (
                "Atomically submit every narrative-state change caused by the "
                "completed chapter. In upsert_character_state, use {name, "
                "description} to provide each character's complete current "
                "location, goal, relationships, knowledge, possessions, and "
                "physical or emotional condition; an existing name is replaced "
                "automatically. In add_past_event, add a chapter event only "
                "when it is not explicit in the outline and may affect later "
                "plot, and give it a stable snake_case key. In "
                "add_future_requirement, add only a concrete matter that must "
                "be fulfilled later, also with a stable key. Pass fulfilled "
                "keys to resolve_future_requirement. All four fields must be "
                "native JSON arrays; use [] when a field has no changes."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "upsert_character_state": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["name", "description"],
                            "additionalProperties": False,
                        },
                    },
                    "add_past_event": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["key", "description"],
                            "additionalProperties": False,
                        },
                    },
                    "add_future_requirement": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["key", "description"],
                            "additionalProperties": False,
                        },
                    },
                    "resolve_future_requirement": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "upsert_character_state",
                    "add_past_event",
                    "add_future_requirement",
                    "resolve_future_requirement",
                ],
                "additionalProperties": False,
            },
        },
    },
]


def count_words(text: str) -> int:
    if not text:
        return 0
    return len(_CJK.findall(text)) + len(_ASCII_WORD.findall(text))


def length_limits(target: int) -> Tuple[int, int]:
    return (
        math.ceil(target * (1 - WORD_COUNT_TOLERANCE)),
        math.floor(target * (1 + WORD_COUNT_TOLERANCE)),
    )


def fuzzy_locate(
    content: str, old_text: str, anchor_chars: int = 20
) -> Optional[Tuple[int, int]]:
    if not old_text:
        return None
    index = content.find(old_text)
    if index != -1:
        return index, index + len(old_text)
    if len(old_text) <= 2 * anchor_chars + 4:
        return None

    def ws_pattern(part: str) -> str:
        pieces = re.split(r"\s+", part)
        return r"\s+".join(re.escape(p) for p in pieces if p)

    head = ws_pattern(old_text[:anchor_chars])
    tail = ws_pattern(old_text[-anchor_chars:])
    if not head or not tail:
        return None
    span_limit = max(len(old_text) * 2, len(old_text) + 200)
    pattern = head + r".{0," + str(span_limit) + r"}?" + tail
    match = re.search(pattern, content, flags=re.DOTALL)
    if match:
        return match.start(), match.end()
    return None


def parse_tool_arguments(raw: str) -> Dict[str, Any]:
    try:
        args = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        try:
            repaired = raw.replace("'", '"') if isinstance(raw, str) else raw
            args = json.loads(repaired)
        except Exception:
            return {}
    return args if isinstance(args, dict) else {}