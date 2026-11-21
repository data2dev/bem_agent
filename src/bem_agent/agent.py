"""The agent loop: an LLM (any provider in bem_agent.llm) calling the simulation tools.

    python -m bem_agent.agent "Size HVAC for a 3-story, 2,800 m2 office in Chicago"
    python -m bem_agent.agent            # interactive

The loop keeps each request under a token budget (Groq's free tier allows 8K tokens
per minute): tool results are compact JSON, old tool results are shortened first, and
the oldest exchanges are dropped if that is not enough.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .llm import (
    LLMClient,
    LLMRequestError,
    Message,
    RateLimitExceeded,
    ToolCall,
    ToolUseFailed,
    Usage,
    get_client,
)
from .tools import ToolContext, execute, to_text, tool_specs

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a building energy modeling assistant. You run EnergyPlus through tools.

Rules:
- Every number you report must come from a tool result; never estimate or invent results.
- Inputs and results are SI. Convert the user's units (1 ft2 = 0.0929 m2, 1 ton = 3.517 kW, \
°F = °C*9/5+32) and report in their units too.
- Check each run's status and issues before using its numbers. If a run failed or has \
errors, say so and fix the inputs if you can.
- Peak zone loads are what the rooms need and do not depend on the HVAC system. To compare \
HVAC systems use cooling_capacity_kw / heating_capacity_kw, and annual energy (EUI) from \
run_period="annual" runs.
- To compare several options, use one run_parametric_study instead of many single runs.
- Use few tool calls. Reuse run_ids instead of re-running.
- Interpret, don't just rank: say how big a difference is (in %), and call differences \
under about 5% roughly equal. Central plant capacity (chiller, boiler) and the sum of \
per-zone units (PTHP) are sized differently; mention it when comparing them.
- Finish with a short answer: the key numbers, the run_id or study_id they came from, \
and the main assumptions (presets used, sizing-only vs annual) and any warnings.
"""

CHARS_PER_TOKEN = 3.5
MAX_REJECTED_TOOL_CALLS = 2  # per question
TRIMMED_TOOL_CHARS = 300


@dataclass
class Step:
    tool: str
    arguments: dict[str, Any]
    result: dict[str, Any]


@dataclass
class AgentResult:
    answer: str
    steps: list[Step] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    llm_calls: int = 0
    stopped_early: bool = False
    rejected_tool_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "steps": [s.__dict__ for s in self.steps],
            "usage": {"input": self.usage.input_tokens, "output": self.usage.output_tokens},
            "llm_calls": self.llm_calls,
            "stopped_early": self.stopped_early,
            "rejected_tool_calls": self.rejected_tool_calls,
        }


def estimate_tokens(messages: list[Message], tools_chars: int = 0) -> int:
    chars = tools_chars
    for m in messages:
        chars += len(m.content) + 20
        for c in m.tool_calls:
            chars += len(c.name) + len(json.dumps(c.arguments)) + 20
    return int(chars / CHARS_PER_TOKEN)


def trim_history(messages: list[Message], budget_tokens: int, tools_chars: int) -> list[Message]:
    """Shrink history to fit the budget, keeping the system prompt and the question.

    1. Shorten old tool results (all but the latest exchange).
    2. Drop the oldest whole exchanges (assistant tool calls + their results).
    """
    if estimate_tokens(messages, tools_chars) <= budget_tokens:
        return messages
    msgs = list(messages)
    last_assistant = max((i for i, m in enumerate(msgs) if m.role == "assistant"), default=-1)
    for i, m in enumerate(msgs):
        if m.role == "tool" and i < last_assistant and len(m.content) > TRIMMED_TOOL_CHARS:
            msgs[i] = Message(
                role="tool",
                content=m.content[:TRIMMED_TOOL_CHARS] + '..."(older result shortened)"',
                tool_call_id=m.tool_call_id,
                name=m.name,
            )
    head = [m for m in msgs[:2]]  # system + first user message
    body = msgs[2:]
    while estimate_tokens(head + body, tools_chars) > budget_tokens and body:
        # drop the first exchange: an assistant message and the tool results after it
        drop = 1
        while drop < len(body) and body[drop].role == "tool":
            drop += 1
        if drop >= len(body):  # never drop the latest exchange
            break
        body = body[drop:]
    if len(body) < len(msgs) - 2:
        note = Message.user("(Earlier steps were removed to save space; run_ids remain valid.)")
        return [*head, note, *body]
    return head + body


class Agent:
    def __init__(
        self,
        client: LLMClient,
        ctx: ToolContext | None = None,
        *,
        max_steps: int = 12,
        max_input_tokens: int | None = None,
        max_output_tokens: int = 1500,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.client = client
        self.ctx = ctx or ToolContext()
        self.max_steps = max_steps
        self.max_input_tokens = max_input_tokens or int(
            os.environ.get("BEM_AGENT_MAX_INPUT_TOKENS", "6000")
        )
        self.max_output_tokens = max_output_tokens
        self.on_event = on_event or (lambda kind, data: None)
        self.tools = tool_specs()
        self._tools_chars = sum(
            len(json.dumps({"n": t.name, "d": t.description, "p": t.parameters}))
            for t in self.tools
        )

    def run(self, question: str, history: list[Message] | None = None) -> AgentResult:
        messages = history or [Message.system(SYSTEM_PROMPT)]
        messages.append(Message.user(question))
        result = AgentResult(answer="")
        for _ in range(self.max_steps):
            messages = trim_history(messages, self.max_input_tokens, self._tools_chars)
            try:
                response = self._chat(messages, result, with_tools=True)
            except ToolUseFailed as exc:
                # The provider rejected the model's tool call before we saw it (Groq
                # validates calls against the schema). Say what went wrong and retry.
                result.rejected_tool_calls += 1
                if result.rejected_tool_calls > MAX_REJECTED_TOOL_CALLS:
                    raise
                self.on_event("rejected", {"reason": exc.detail})
                messages.append(Message.user(_rejection_hint(exc)))
                continue
            messages.append(response.as_message())
            if not response.tool_calls:
                result.answer = response.text.strip()
                self._history = messages
                return result
            if response.text.strip():
                self.on_event("thought", {"text": response.text.strip()})
            for call in response.tool_calls:
                tool_result = self._call_tool(call)
                result.steps.append(Step(call.name, call.arguments, tool_result))
                messages.append(Message.tool_result(call, to_text(tool_result)))
        # Out of steps: ask for a final answer without tools.
        result.stopped_early = True
        messages.append(
            Message.user("Step limit reached. Answer now with the results you already have.")
        )
        messages = trim_history(messages, self.max_input_tokens, 0)
        response = self._chat(messages, result, with_tools=False)
        result.answer = response.text.strip()
        self._history = messages
        return result

    @property
    def history(self) -> list[Message]:
        return getattr(self, "_history", [])

    def _chat(self, messages: list[Message], result: AgentResult, with_tools: bool):
        response = self.client.chat(
            messages,
            self.tools if with_tools else None,
            max_tokens=self.max_output_tokens,
        )
        result.llm_calls += 1
        result.usage.input_tokens += response.usage.input_tokens
        result.usage.output_tokens += response.usage.output_tokens
        return response

    def _call_tool(self, call: ToolCall) -> dict[str, Any]:
        self.on_event("tool_call", {"tool": call.name, "arguments": call.arguments})
        if call.parse_error:
            tool_result = {"error": f"{call.parse_error}. Send valid JSON arguments."}
        else:
            tool_result = execute(self.ctx, call.name, call.arguments)
        self.on_event("tool_result", {"tool": call.name, "result": tool_result})
        return tool_result


def _rejection_hint(exc: ToolUseFailed) -> str:
    generation = " ".join(exc.failed_generation.split())[:400]
    return (
        f"Your last tool call was rejected: {exc.detail}"
        + (f" You sent: {generation}" if generation else "")
        + " Call one tool with a single JSON object of arguments that matches its "
        "parameters exactly, or answer in plain text."
    )


# ---------- CLI ----------


def _print_event(kind: str, data: dict[str, Any]) -> None:
    if kind == "tool_call":
        args = json.dumps(data["arguments"], separators=(",", ":"))
        print(f"  → {data['tool']}({args[:200]}{'…' if len(args) > 200 else ''})", flush=True)
    elif kind == "tool_result":
        r = data["result"]
        if "error" in r:
            print(f"    ✗ {r['error'][:200]}", flush=True)
        else:
            keys = [k for k in ("run_id", "study_id", "status", "chart") if k in r]
            brief = ", ".join(f"{k}={r[k]}" for k in keys) or "ok"
            print(f"    ✓ {brief}", flush=True)
    elif kind == "rejected":
        print(
            f"    ↺ tool call rejected by the provider, retrying: {data['reason'][:200]}",
            flush=True,
        )
    elif kind == "thought":
        print(f"  … {data['text'][:300]}", flush=True)


def _save_transcript(base_dir: Path, question: str, result: AgentResult, client: LLMClient) -> Path:
    folder = base_dir / "transcripts"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(
        json.dumps(
            {
                "provider": client.provider,
                "model": client.model,
                "question": question,
                **result.to_dict(),
            },
            indent=2,
            default=str,
        )
    )
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bem-agent", description="Ask the BEM agent a question.")
    p.add_argument("question", nargs="*", help="omit for interactive mode")
    p.add_argument(
        "--provider", help="groq (default), anthropic, gemini, openai, openai_compatible"
    )
    p.add_argument("--model")
    p.add_argument("--max-steps", type=int, default=12)
    p.add_argument("--runs-dir", type=Path, help="default $BEM_RUNS_DIR or ./runs")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING)

    client = get_client(a.provider, a.model)
    ctx = ToolContext(base_dir=a.runs_dir) if a.runs_dir else ToolContext()
    agent = Agent(client, ctx, max_steps=a.max_steps, on_event=_print_event)
    print(f"[{client.provider} / {client.model}]  runs: {ctx.base_dir}", flush=True)

    def ask(question: str, history: list[Message] | None) -> list[Message]:
        try:
            result = agent.run(question, history)
        except RateLimitExceeded as exc:
            print(f"\nRate limit: {exc}. Wait a minute and try again.")
            return history or []
        except LLMRequestError as exc:
            print(f"\nLLM error: {exc}")
            if isinstance(exc, ToolUseFailed) and exc.failed_generation:
                print(f"Model output that was rejected: {exc.failed_generation[:1000]}")
            return history or []
        print("\n" + result.answer + "\n")
        u = result.usage
        print(
            f"[{result.llm_calls} LLM calls, {len(result.steps)} tool calls, "
            f"{u.input_tokens:,} in / {u.output_tokens:,} out tokens]"
        )
        print(f"[transcript: {_save_transcript(ctx.base_dir, question, result, client)}]")
        return agent.history

    if a.question:
        ask(" ".join(a.question), None)
        return 0
    history: list[Message] | None = None
    print("Ask about a building (empty line to quit).")
    while True:
        try:
            question = input("\n> ").strip()
        except EOFError:
            break
        if not question:
            break
        history = ask(question, history)
    return 0


if __name__ == "__main__":
    sys.exit(main())
