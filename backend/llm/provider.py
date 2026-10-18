from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx
from openai import AsyncOpenAI

from .app_settings import get_ollama_cloud_key, is_ollama_cloud_base

_INLINE_THINKING = re.compile(r"<think(?:ing)?>(?:.|\n)*?</think(?:ing)?>", re.IGNORECASE)


def strip_inline_thinking(text: str) -> str:
    """Remove inline `<thinking>...</thinking>` blocks some servers emit inside content."""
    if not text:
        return text
    cleaned = _INLINE_THINKING.sub("", text)
    return cleaned.strip()


def _next_significant(raw: str, index: int) -> Optional[str]:
    while index < len(raw):
        char = raw[index]
        if not char.isspace():
            return char
        index += 1
    return None


def native_request_body(
    profile: "LlmProfile",
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    temperature: Optional[float],
    max_output_tokens: Optional[int],
) -> Dict[str, Any]:
    """Build an Ollama-native `/api/chat` body from our OpenAI-style state."""
    body: Dict[str, Any] = {
        "model": profile.model,
        "stream": True,
        "temperature": profile.temperature if temperature is None else temperature,
        "num_predict": max_output_tokens or profile.max_output_tokens,
    }
    native_messages: List[Dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            native_calls = []
            for call in message["tool_calls"]:
                function = call.get("function") or {}
                name = function.get("name") or call.get("name")
                raw_args = function.get("arguments", call.get("arguments", "{}"))
                try:
                    arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    arguments = {"_unparseable": str(raw_args)[:300]}
                if name:
                    native_calls.append({"function": {"name": name, "arguments": arguments}})
            entry: Dict[str, Any] = {
                "role": "assistant",
                "content": str(message.get("content") or ""),
            }
            if native_calls:
                entry["tool_calls"] = native_calls
            native_messages.append(entry)
            continue
        entry = {
            "role": role,
            "content": str(message.get("content") or ""),
        }
        if role == "tool" and message.get("name"):
            entry["tool_name"] = message["name"]
        native_messages.append(entry)
    body["messages"] = native_messages
    if tools:
        body["tools"] = [
            {
                "type": tool.get("type", "function"),
                "function": tool.get("function"),
            }
            for tool in tools
            if tool.get("function")
        ]
    level = (profile.thinking or "default").lower()
    if level == "off":
        body["think"] = False
    elif level in ("low", "medium", "high"):
        body["think"] = True
    return body


def native_chunk_event(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize one Ollama-native JSONL chunk into a plain event dict."""
    message = obj.get("message") or {}
    tool_calls = []
    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {}, ensure_ascii=False)
        tool_calls.append(
            {"id": f"native-{index}", "name": function.get("name", ""), "arguments": arguments}
        )
    usage = None
    if obj.get("done"):
        usage = {
            "prompt_tokens": int(obj.get("prompt_eval_count") or 0),
            "completion_tokens": int(obj.get("eval_count") or 0),
        }
    return {
        "content": message.get("content") or "",
        "thinking": message.get("thinking") or "",
        "tool_calls": tool_calls,
        "done": bool(obj.get("done")),
        "usage": usage,
    }


def sanitize_tool_call_arguments(raw: Any) -> str:
    """Return provider-valid JSON for streamed tool-call arguments.

    Some models emit literal control characters (raw newlines inside strings,
    pretty-printed fragments) that are invalid JSON; providers then reject the
    whole follow-up request with `invalid tool call arguments`. This repairs
    control characters only inside string literals, then re-serializes
    canonically. Unrepairable payloads become an explicit error object.
    """
    if not isinstance(raw, str) or not raw.strip():
        return "{}"
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False)
    except Exception:
        pass

    repaired_chars: List[str] = []
    in_string = False
    escaped = False
    control_escapes = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    for index, char in enumerate(raw):
        if in_string:
            if escaped:
                repaired_chars.append(char)
                escaped = False
                continue
            if char == "\\":
                repaired_chars.append(char)
                escaped = True
                continue
            if char == '"':
                # Decide whether this quote closes the string: only when the next
                # non-whitespace character is a structural token. An embedded quote
                # followed by prose (e.g. `with "quote"` inside a value) is escaped.
                look = _next_significant(raw, index + 1)
                if look is not None and look not in ",}]:)":
                    repaired_chars.append('\\"')
                    index += 1
                    continue
                repaired_chars.append(char)
                in_string = False
                continue
            if ord(char) < 0x20:
                replaced = control_escapes.get(char) or "\\u%04x" % ord(char)
                repaired_chars.append(replaced)
                continue
            repaired_chars.append(char)
            continue
        if char == '"':
            repaired_chars.append(char)
            in_string = True
            continue
        repaired_chars.append(char)
    repaired = "".join(repaired_chars)
    try:
        return json.dumps(json.loads(repaired), ensure_ascii=False)
    except Exception:
        return json.dumps(
            {"unparseable": True, "excerpt": raw[:300]}, ensure_ascii=False
        )


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
    thinking_ignored: bool = False


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
    thinking: str = "default"
    extra_params: Dict[str, Any] = field(default_factory=dict)

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
            "thinking": self.thinking,
            "extra_params": self.extra_params,
        }

    def thinking_kwargs(self) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Map the thinking setting onto provider-specific request parameters.

        Returns (sdk_kwargs, extra_body). ``sdk_kwargs`` holds OpenAI-SDK-native
        fields such as ``reasoning_effort``; ``extra_body`` carries raw
        provider-scheme keys (Ollama ``think``, OpenRouter ``reasoning``) and the
        user's free-form ``extra_params``. Empty dicts mean "no special params".
        """
        level = (self.thinking or "default").lower()
        sdk: Dict[str, Any] = {}
        extra: Dict[str, Any] = dict(self.extra_params or {})
        if level == "default" and not extra:
            return {}, {}
        base = (self.base_url or "").lower()
        openrouter = "openrouter.ai" in base
        ollama = "ollama" in base or ":11434" in base
        if level in ("low", "medium", "high"):
            if openrouter:
                extra.setdefault("reasoning", {"effort": level})
            else:
                sdk["reasoning_effort"] = level
        elif level == "off":
            if openrouter:
                existing = extra.get("reasoning") if isinstance(extra.get("reasoning"), dict) else {}
                extra["reasoning"] = {**existing, "exclude": True}
            elif ollama:
                extra["think"] = False
            # Generic OpenAI-compatible servers have no off-switch; leaving the
            # params empty lets the model use its default behavior.
        return sdk, extra


def _thinking_params_rejected(exc: Exception, sdk_params: Dict[str, Any], extra_params: Dict[str, Any]) -> bool:
    text = str(exc).lower()
    try:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    except Exception:
        status = None
    if status is not None and status != 400:
        return False
    keys = [str(k) for k in list(sdk_params) + list(extra_params)]
    if keys and all(k.lower() in text for k in keys):
        return True
    signals = (
        "unrecognized",
        "unknown field",
        "unknown parameter",
        "not supported",
        "extra_forbidden",
        "extra fields",
    )
    return any(s in text for s in signals)


class LlmClient:
    def __init__(self, profile: LlmProfile):
        self.profile = profile
        self.profile_is_cloud = is_ollama_cloud_base(profile.base_url)
        # the profile's OWN key, kept pristine: while a global key exists it is
        # the active one, the moment it is cleared this one takes over again
        self._own_api_key = (profile.api_key or "").strip()
        self._client = AsyncOpenAI(
            base_url=profile.base_url or None,
            api_key=self._resolve_api_key(),
            max_retries=0,
            timeout=httpx.Timeout(900.0, read=900.0, write=600.0, connect=30.0),
        )
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(900.0, read=900.0, write=600.0, connect=30.0)
        )

    def _resolve_api_key(self) -> str:
        """Effective key for this client.

        Ollama Cloud is governed by the ONE global key, read fresh from the
        settings file; a profile key never gets a chance to go stale while a
        global key exists. Without a global key the profile's own key applies,
        then the harmless placeholder for key-less local servers.
        """
        if self.profile_is_cloud:
            key = get_ollama_cloud_key().strip()
            if key:
                self.profile.api_key = key
                return key
            return self._own_api_key or "sk-no-key-required"
        return (self.profile.api_key or "").strip() or "sk-no-key-required"

    def _apply_cloud_key(self) -> str:
        """Resolve the effective key and push it into the SDK client.

        The openai SDK derives the Authorization header from ``api_key`` on
        EVERY request it makes, so re-running this at the top of each chat()
        / list_models() call ships a settings-file key swap to the very next
        request — mid-run included, on any openai 1.x/2.x version."""
        key = self._resolve_api_key()
        self._client.api_key = key
        return key

    async def aclose(self) -> None:
        """Release the pooled HTTP connections (translation ensemble loop)."""
        try:
            await self._http.aclose()
        except Exception:
            pass
        try:
            await self._client.close()
        except Exception:
            pass

    def _is_ollama(self) -> bool:
        base = (self.profile.base_url or "").lower()
        return "ollama" in base or ":11434" in base

    def _use_native(self) -> bool:
        override = (self.profile.extra_params or {}).get("use_native")
        if override is not None:
            return bool(override)
        base = (self.profile.base_url or "").lower()
        if "ollama.com" in base or "ollama.ai" in base:
            # Ollama CLOUD is OpenAI-compatible only — the native /api/chat
            # path has no Authorization header and returns 401 there
            return False
        return self._is_ollama() and (self.profile.thinking or "default") != "default"

    def _native_endpoint(self) -> str:
        base = (self.profile.base_url or "").rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        return base + "/api/chat"

    async def _ollama_native_chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        tool_choice: Optional[str],
        temperature: Optional[float],
        max_output_tokens: Optional[int],
        on_delta: Optional[Any],
    ) -> LlmResponse:
        """Talk to Ollama's native /api/chat (supports the think knob reliably)."""
        body = native_request_body(self.profile, messages, tools, temperature, max_output_tokens)
        headers = None
        if self.profile.api_key:
            headers = {"Authorization": f"Bearer {self.profile.api_key}"}
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        usage = Usage()
        usage.price_in_per_m = self.profile.price_in_per_m
        usage.price_out_per_m = self.profile.price_out_per_m
        usage.estimated = True
        finish_reason = "stop"
        async with self._http.stream("POST", self._native_endpoint(), json=body, headers=headers) as response:
            if response.status_code != 200:
                raw = (await response.aread()).decode("utf-8", "ignore")
                raise LlmError(f"Ollama native error {response.status_code}: {raw[:300]}", status_code=response.status_code)
            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = native_chunk_event(obj)
                if event["thinking"]:
                    reasoning_parts.append(event["thinking"])
                    if on_delta is not None:
                        await on_delta("reasoning", event["thinking"])
                if event["content"]:
                    content_parts.append(event["content"])
                    if on_delta is not None:
                        await on_delta("content", event["content"])
                for call_entry in event["tool_calls"]:
                    if on_delta is not None and call_entry["arguments"]:
                        await on_delta("tool_args", call_entry["arguments"])
                    tool_calls.append(
                        ToolCall(call_id=call_entry["id"], name=call_entry["name"], arguments=call_entry["arguments"])
                    )
                if event["done"]:
                    if event["usage"]:
                        usage.prompt_tokens = event["usage"]["prompt_tokens"]
                        usage.completion_tokens = event["usage"]["completion_tokens"]
                        usage.estimated = False
                    if tool_calls:
                        finish_reason = "tool_calls"
                    break
        content = strip_inline_thinking("".join(content_parts).strip())
        if not content:
            content = strip_inline_thinking("".join(reasoning_parts).strip())
        return LlmResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

    def _apply_thinking(self, kwargs: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        sdk_params, extra_params = self.profile.thinking_kwargs()
        if sdk_params:
            kwargs.update(sdk_params)
        if extra_params:
            merged = dict(kwargs.get("extra_body") or {})
            merged.update(extra_params)
            kwargs["extra_body"] = merged
        return sdk_params, extra_params

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        temperature: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        on_delta: Optional[Any] = None,
    ) -> LlmResponse:
        # fresh key resolution on every call — the native path (and anything
        # reading profile.api_key / client.api_key) never lags behind a
        # settings change
        self._apply_cloud_key()
        if self._use_native():
            return await self._ollama_native_chat(
                messages, tools, tool_choice, temperature, max_output_tokens, on_delta
            )
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
        sdk_params, _ = self._apply_thinking(kwargs)
        try:
            response = await self._client.chat.completions.create(**kwargs)
            thinking_ignored = False
        except Exception as exc:
            if not _thinking_params_rejected(exc, sdk_params, {}):
                status = getattr(getattr(exc, "response", None), "status_code", None)
                raise LlmError(str(exc), status_code=status) from exc
            stripped = {k: v for k, v in kwargs.items() if k != "reasoning_effort"}
            if stripped.get("extra_body"):
                stripped.pop("extra_body")
            try:
                response = await self._client.chat.completions.create(**stripped)
                thinking_ignored = True
            except Exception as retry_exc:
                status = getattr(getattr(retry_exc, "response", None), "status_code", None)
                raise LlmError(str(retry_exc), status_code=status) from retry_exc
        choice = response.choices[0]
        message = choice.message
        content = (message.content or "").strip()
        content = strip_inline_thinking(content)
        if not content and hasattr(message, "reasoning_content") and message.reasoning_content:
            content = strip_inline_thinking(message.reasoning_content or "")
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
            thinking_ignored=thinking_ignored,
        )

    async def list_models(self) -> List[str]:
        self._apply_cloud_key()
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
        sdk_params, _ = self._apply_thinking(kwargs)

        stream = None
        thinking_ignored = False
        try:
            try:
                stream = await self._client.chat.completions.create(**kwargs)
            except Exception as first_error:
                if "stream_options" in str(first_error):
                    kwargs.pop("stream_options", None)
                    stream = await self._client.chat.completions.create(**kwargs)
                elif _thinking_params_rejected(first_error, sdk_params, {}):
                    stripped = {k: v for k, v in kwargs.items() if k != "reasoning_effort"}
                    stripped.pop("extra_body", None)
                    thinking_ignored = True
                    stream = await self._client.chat.completions.create(**stripped)
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
        content = strip_inline_thinking(content)
        if not content:
            content = strip_inline_thinking("".join(reasoning_parts).strip())
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
            thinking_ignored=thinking_ignored,
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