from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI


class LlmError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0

    @property
    def cost(self) -> float:
        return (
            self.prompt_tokens / 1_000_000 * self.price_in_per_m
            + self.completion_tokens / 1_000_000 * self.price_out_per_m
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost": round(self.cost, 6),
        }


@dataclass
class LlmResponse:
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)


@dataclass
class LlmProfile:
    id: str = ""
    name: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.7
    max_output_tokens: int = 32768
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0

    def to_dict(self, hide_key: bool = True) -> Dict[str, Any]:
        key = "***" if (hide_key and self.api_key) else self.api_key
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "api_key_set": bool(self.api_key),
            "api_key": key,
            "model": self.model,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "price_in_per_m": self.price_in_per_m,
            "price_out_per_m": self.price_out_per_m,
        }


class LlmClient:
    def __init__(self, profile: LlmProfile):
        self.profile = profile
        self._client = AsyncOpenAI(
            base_url=profile.base_url or None,
            api_key=profile.api_key or "sk-no-key-required",
            max_retries=0,
            timeout=600.0,
        )

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        temperature: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
    ) -> LlmResponse:
        kwargs: Dict[str, Any] = {
            "model": self.profile.model,
            "messages": messages,
            "temperature": self.profile.temperature if temperature is None else temperature,
        }
        token_limit = max_output_tokens or self.profile.max_output_tokens
        kwargs["max_tokens"] = token_limit
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise LlmError(str(exc), status_code=status) from exc
        choice = response.choices[0]
        message = choice.message
        content = (message.content or "").strip()
        if not content and hasattr(message, "reasoning_content") and message.reasoning_content:
            reasoning = message.reasoning_content or ""
            content = reasoning.split("</think>", 1)[1].strip() if "</think>" in reasoning else reasoning.strip()
        tool_calls: List[ToolCall] = []
        for call in message.tool_calls or []:
            tool_calls.append(
                ToolCall(
                    call_id=call.id,
                    name=call.function.name,
                    arguments=call.function.arguments or "{}",
                )
            )
        usage = Usage()
        if response.usage is not None:
            usage.prompt_tokens = response.usage.prompt_tokens or 0
            usage.completion_tokens = response.usage.completion_tokens or 0
        usage.price_in_per_m = self.profile.price_in_per_m
        usage.price_out_per_m = self.profile.price_out_per_m
        return LlmResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )

    async def list_models(self) -> List[str]:
        try:
            page = await self._client.models.list()
        except Exception as exc:
            raise LlmError(str(exc)) from exc
        return sorted(model.id for model in page.data)


def extract_json(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for pattern in (r"```json\s*\n?(.*?)\n?\s*```", r"```\s*\n?(.*?)\n?\s*```"):
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                continue
    best: Any | None = None
    best_len = -1
    for start_char, end_char in (("[", "]"), ("{", "}")):
        for start in (i for i, c in enumerate(text) if c == start_char):
            depth, in_string, escape = 0, False, False
            for i in range(start, len(text)):
                c = text[i]
                if escape:
                    escape = False
                    continue
                if c == "\\" and in_string:
                    escape = True
                    continue
                if c == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if c == start_char:
                    depth += 1
                elif c == end_char:
                    depth -= 1
                    if depth == 0:
                        try:
                            parsed = json.loads(text[start : i + 1])
                        except json.JSONDecodeError:
                            break
                        size = i + 1 - start
                        if size > best_len:
                            best, best_len = parsed, size
                        break
    if best is not None and best_len > 0:
        return best
    raise ValueError(f"Could not extract JSON: {text[:300]}...")