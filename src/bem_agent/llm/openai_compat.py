"""Client for OpenAI-compatible chat completion APIs.

Covers Groq (the default), OpenAI, Google Gemini's OpenAI-compatible endpoint, and any
other server that speaks the same protocol (OpenRouter, vLLM, Ollama, ...).
"""

from __future__ import annotations

import json
from typing import Any

from .base import (
    LLMClient,
    LLMRequestError,
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
    ToolUseFailed,
    Usage,
)


class OpenAICompatibleClient(LLMClient):
    def __init__(
        self,
        model: str,
        *,
        provider: str = "openai_compatible",
        api_key: str | None = None,
        base_url: str | None = None,
        sdk_client: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(model, **kwargs)
        self.provider = provider
        if sdk_client is None:
            import openai

            sdk_client = openai.OpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        self._client = sdk_client
        try:
            import openai

            self._rate_limit_errors = (openai.RateLimitError,)
        except ImportError:  # pragma: no cover - openai is a hard dependency
            self._rate_limit_errors = ()

    def _chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = to_openai_tools(tools)
        try:
            completion = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            if self._rate_limit_errors and isinstance(exc, self._rate_limit_errors):
                raise
            status = getattr(exc, "status_code", None)
            if status is None or not 400 <= status < 500:
                raise
            raise request_error(self.provider, status, exc) from exc
        return from_openai_completion(completion)


def _error_body(exc: Exception) -> dict[str, Any]:
    """The provider's error object, from the parsed body or the raw response."""
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        response = getattr(exc, "response", None)
        try:
            body = json.loads(response.text) if response is not None else None
        except (ValueError, AttributeError):
            text = getattr(response, "text", "") if response is not None else ""
            body = {"message": text or str(exc)}
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    return body if isinstance(body, dict) else {"message": str(exc)}


def request_error(provider: str, status: int, exc: Exception) -> LLMRequestError:
    body = _error_body(exc)
    message = str(body.get("message") or exc)
    if body.get("code") == "tool_use_failed":
        return ToolUseFailed(provider, message, str(body.get("failed_generation") or ""))
    return LLMRequestError(provider, status, message, body.get("code"))


def to_openai_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


def to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        elif m.role == "assistant" and m.tool_calls:
            out.append(
                {
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                        }
                        for c in m.tool_calls
                    ],
                }
            )
        else:
            out.append({"role": m.role, "content": m.content})
    return out


def from_openai_completion(completion: Any) -> LLMResponse:
    choice = completion.choices[0]
    msg = choice.message
    calls: list[ToolCall] = []
    for tc in getattr(msg, "tool_calls", None) or []:
        raw_args = tc.function.arguments or "{}"
        try:
            args = json.loads(raw_args)
            error = None if isinstance(args, dict) else "arguments must be a JSON object"
            if error:
                args = {}
        except json.JSONDecodeError as exc:
            args, error = {}, f"invalid JSON in tool arguments: {exc}"
        calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args, parse_error=error))
    usage = getattr(completion, "usage", None)
    return LLMResponse(
        text=msg.content or "",
        tool_calls=calls,
        stop_reason=choice.finish_reason,
        usage=Usage(
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        ),
        raw=completion,
    )
