"""Provider-neutral types for chat with tool calling.

The agent loop only ever sees these types. Each provider client converts them to and
from its own wire format, so switching providers never touches agent code.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger(__name__)

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolSpec:
    """A tool the model may call. `parameters` is a JSON Schema object."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # Set when the model produced arguments that were not valid JSON. The agent should
    # send this back to the model as an error instead of calling the tool.
    parse_error: str | None = None


@dataclass
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # For role == "tool": which call this is the result of, and the tool's name.
    tool_call_id: str | None = None
    name: str | None = None

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role="user", content=content)

    @classmethod
    def tool_result(cls, call: ToolCall, content: str) -> Message:
        return cls(role="tool", content=content, tool_call_id=call.id, name=call.name)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str | None
    usage: Usage
    raw: Any = None

    def as_message(self) -> Message:
        """The assistant turn to append to the conversation history."""
        return Message(role="assistant", content=self.text, tool_calls=list(self.tool_calls))


class RateLimitExceeded(RuntimeError):
    """Raised when the provider keeps rate-limiting after all retries."""


class LLMClient(ABC):
    """Base class for provider clients.

    Subclasses implement `_chat` and list their SDK's rate-limit exception types in
    `_rate_limit_errors`; `chat` adds retry with backoff on top.
    """

    provider: str = ""
    _rate_limit_errors: tuple[type[BaseException], ...] = ()

    def __init__(
        self,
        model: str,
        *,
        max_retries: int = 5,
        base_delay: float = 2.0,
        max_delay: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._sleep = sleep

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        attempt = 0
        while True:
            try:
                return self._chat(
                    messages, tools or [], max_tokens=max_tokens, temperature=temperature
                )
            except self._rate_limit_errors as exc:
                if attempt >= self.max_retries:
                    raise RateLimitExceeded(
                        f"{self.provider}: still rate-limited after {attempt} retries"
                    ) from exc
                delay = _retry_after_seconds(exc)
                if delay is None:
                    delay = min(self.base_delay * (2**attempt), self.max_delay)
                delay = min(delay, self.max_delay)
                log.warning("%s rate limit hit; retrying in %.1fs", self.provider, delay)
                self._sleep(delay)
                attempt += 1

    @abstractmethod
    def _chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse: ...

    def __repr__(self) -> str:
        return f"{type(self).__name__}(provider={self.provider!r}, model={self.model!r})"


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Read a Retry-After header from an SDK error, if there is one."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return None
