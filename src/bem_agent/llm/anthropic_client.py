"""Client for Anthropic's Claude models via the Messages API."""

from __future__ import annotations

from typing import Any

from .base import LLMClient, LLMResponse, Message, ToolCall, ToolSpec, Usage


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        sdk_client: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(model, **kwargs)
        if sdk_client is None:
            import anthropic

            sdk_client = anthropic.Anthropic(api_key=api_key, max_retries=0)
        self._client = sdk_client
        try:
            import anthropic

            self._rate_limit_errors = (anthropic.RateLimitError,)
        except ImportError:  # pragma: no cover - anthropic is a hard dependency
            self._rate_limit_errors = ()

    def _chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        system, converted = to_anthropic_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = to_anthropic_tools(tools)
        response = self._client.messages.create(**kwargs)
        return from_anthropic_response(response)


def to_anthropic_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
    ]


def to_anthropic_messages(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Convert neutral messages to Anthropic format.

    System messages move to the separate `system` parameter. Tool results become
    `tool_result` blocks in a user turn, and consecutive turns with the same role are
    merged, because the Messages API requires user and assistant turns to alternate.
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    def append(role: str, blocks: list[dict[str, Any]]) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
        elif m.role == "user":
            append("user", [{"type": "text", "text": m.content}])
        elif m.role == "tool":
            append(
                "user",
                [{"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}],
            )
        elif m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for c in m.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                )
            if blocks:
                append("assistant", blocks)
    return "\n\n".join(system_parts), out


def from_anthropic_response(response: Any) -> LLMResponse:
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in response.content:
        if block.type == "text":
            texts.append(block.text)
        elif block.type == "tool_use":
            args = block.input if isinstance(block.input, dict) else {}
            error = None if isinstance(block.input, dict) else "arguments must be a JSON object"
            calls.append(ToolCall(id=block.id, name=block.name, arguments=args, parse_error=error))
    usage = getattr(response, "usage", None)
    return LLMResponse(
        text="".join(texts),
        tool_calls=calls,
        stop_reason=response.stop_reason,
        usage=Usage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        ),
        raw=response,
    )
