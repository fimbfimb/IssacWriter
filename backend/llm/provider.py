from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx
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
    estimated: bool = False

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
            "estimated": self.estimated,
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
            timeout=httpx.Timeout(900.0, read=900.0, write=600.0, connect=30.0),
        )

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        temperature: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        on_delta: Optional[Any] = None,
    ) -> LlmResponse:
        if on_delta is not None:
            return await self._chat_stream(
                messages, tools, tool_choice, temperature, max_output_tokens, on_delta
            )
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

    async def _chat_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        tool_choice: Optional[str],
        temperature: Optional[float],
        max_output_tokens: Optional[int],
        on_delta: Any,
    ) -> LlmResponse:
        kwargs: Dict[str, Any] = {
            "model": self.profile.model,
            "messages": messages,
            "temperature": self.profile.temperature if temperature is None else temperature,
            "max_tokens": max_output_tokens or self.profile.max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice

        stream = None
        try:
            try:
                stream = await self._client.chat.completions.create(**kwargs)
            except Exception as first_error:
                if "stream_options" in str(first_error):
                    kwargs.pop("stream_options", None)
                    stream = await self._client.chat.completions.create(**kwargs)
                else:
                    raise
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise LlmError(str(exc), status_code=status) from exc

        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        calls: Dict[int, Dict[str, Any]] = {}
        usage_payload: Dict[str, Any] | None = None
        finish_reason = "stop"
        try:
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage_payload = {
                        "prompt_tokens": chunk.usage.prompt_tokens or 0,
                        "completion_tokens": chunk.usage.completion_tokens or 0,
                    }
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                dump = delta.model_dump() if hasattr(delta, "model_dump") else {}
                reasoning = (
                    dump.get("reasoning_content")
                    or dump.get("reasoning")
                    or dump.get("thinking")
                    or ""
                )
                if reasoning:
                    reasoning_parts.append(reasoning)
                    await on_delta("reasoning", reasoning)
                if delta.content:
                    content_parts.append(delta.content)
                    await on_delta("content", delta.content)
                for tc in delta.tool_calls or []:
                    index = getattr(tc, "index", 0) or 0
                    entry = calls.setdefault(index, {"id": tc.id or "", "name": None, "args": []})
                    if tc.id:
                        entry["id"] = tc.id
                    function = getattr(tc, "function", None)
                    if function is not None and function.name and entry["name"] is None:
                        entry["name"] = function.name
                    if function is not None and function.arguments:
                        entry["args"].append(function.arguments)
                        await on_delta("tool_args", function.arguments)
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result

        content = "".join(content_parts).strip()
        if not content:
            content = "".join(reasoning_parts).strip()
        tool_calls: List[ToolCall] = []
        for index in sorted(calls):
            entry = calls[index]
            tool_calls.append(
                ToolCall(
                    call_id=entry["id"] or f"call-{index}",
                    name=entry["name"] or "unknown",
                    arguments="".join(entry["args"]) or "{}",
                )
            )
        usage = Usage()
        if usage_payload is not None:
            usage.prompt_tokens = int(usage_payload["prompt_tokens"])
            usage.completion_tokens = int(usage_payload["completion_tokens"])
        else:
            import math as _math

            prompt_chars = sum(len(str(m.get("content") or "")) for m in messages)
            completion_chars = len(content) + sum(
                len(c.arguments) + len(c.name) for c in tool_calls
            )
            usage.prompt_tokens = _math.ceil(prompt_chars / 4)
            usage.completion_tokens = _math.ceil(completion_chars / 4)
            usage.estimated = True
        usage.price_in_per_m = self.profile.price_in_per_m
        usage.price_out_per_m = self.profile.price_out_per_m
        return LlmResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )


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