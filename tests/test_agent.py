import json

import pytest

from bem_agent.agent import Agent, estimate_tokens, main, trim_history
from bem_agent.llm import LLMClient, LLMResponse, Message, ToolCall, Usage
from bem_agent.tools import ToolContext


class ScriptedClient(LLMClient):
    """Plays back a list of responses and records what the agent sent."""

    provider = "scripted"

    def __init__(self, responses):
        super().__init__("scripted-model", sleep=lambda s: None)
        self.responses = list(responses)
        self.requests = []

    def _chat(self, messages, tools, *, max_tokens, temperature):
        self.requests.append({"messages": list(messages), "tools": [t.name for t in tools]})
        return self.responses.pop(0)


def say(text):
    return LLMResponse(text=text, tool_calls=[], stop_reason="stop", usage=Usage(100, 20))


def call(name, args, id_="c1", parse_error=None):
    tc = ToolCall(id=id_, name=name, arguments=args, parse_error=parse_error)
    return LLMResponse(text="", tool_calls=[tc], stop_reason="tool_calls", usage=Usage(100, 10))


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(base_dir=tmp_path, max_simulations=5)


def test_agent_calls_tool_then_answers(ctx):
    client = ScriptedClient([call("list_options", {}), say("Chicago is available.")])
    events = []
    agent = Agent(client, ctx, on_event=lambda k, d: events.append(k))
    result = agent.run("Which cities can I use?")
    assert result.answer == "Chicago is available."
    assert [s.tool for s in result.steps] == ["list_options"]
    assert "chicago" in result.steps[0].result["locations"]
    assert result.llm_calls == 2 and result.usage.input_tokens == 200
    second = client.requests[1]["messages"]
    assert second[0].role == "system" and second[-1].role == "tool"
    assert json.loads(second[-1].content)["locations"]
    assert "run_simulation" in client.requests[0]["tools"]
    assert events == ["tool_call", "tool_result"]


def test_tool_errors_go_back_to_the_model(ctx):
    client = ScriptedClient(
        [
            call("run_simulation", {"location": "atlantis"}),
            call("run_simulation", {}, id_="c2", parse_error="invalid JSON in tool arguments"),
            say("Atlantis is not a supported location."),
        ]
    )
    result = Agent(client, ctx).run("Simulate a building in Atlantis")
    assert "error" in result.steps[0].result
    assert "valid JSON" in result.steps[1].result["error"]
    assert result.answer.startswith("Atlantis")
    assert ctx.simulations_used == 0


def test_step_limit_forces_final_answer(ctx):
    client = ScriptedClient(
        [call("list_options", {}, id_=f"c{i}") for i in range(3)] + [say("Partial answer.")]
    )
    result = Agent(client, ctx, max_steps=3).run("loop forever")
    assert result.stopped_early
    assert result.answer == "Partial answer."
    assert client.requests[-1]["tools"] == []  # final call has no tools


def test_multi_turn_history(ctx):
    client = ScriptedClient([say("Hi."), say("Still here.")])
    agent = Agent(client, ctx)
    agent.run("hello")
    agent.run("again", agent.history)
    roles = [m.role for m in client.requests[1]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]


def test_trim_history_shortens_then_drops():
    big = "x" * 5000
    msgs = [Message.system("sys"), Message.user("question")]
    for i in range(4):
        tc = ToolCall(id=f"c{i}", name="run_simulation", arguments={"i": i})
        msgs.append(Message(role="assistant", tool_calls=[tc]))
        msgs.append(Message.tool_result(tc, big))
    full = estimate_tokens(msgs)

    shortened = trim_history(msgs, budget_tokens=2000, tools_chars=0)
    assert estimate_tokens(shortened) < full
    assert shortened[0].content == "sys" and shortened[1].content == "question"
    assert shortened[-1].content == big  # latest result kept whole
    assert all(len(m.content) < 400 for m in shortened[2:-1] if m.role == "tool")

    dropped = trim_history(msgs, budget_tokens=1600, tools_chars=0)
    assert "removed to save space" in dropped[2].content
    assert dropped[-2].tool_calls[0].id == "c3"  # latest exchange survives
    # every tool result still follows its assistant call
    for i, m in enumerate(dropped):
        if m.role == "tool":
            assert dropped[i - 1].role in ("assistant", "tool")

    assert trim_history(msgs[:3], budget_tokens=10_000, tools_chars=0) == msgs[:3]


def test_cli_one_shot(ctx, monkeypatch, capsys):
    client = ScriptedClient([call("list_options", {}), say("Done.")])
    monkeypatch.setattr("bem_agent.agent.get_client", lambda provider, model: client)
    assert main(["--runs-dir", str(ctx.base_dir), "What can you do?"]) == 0
    out = capsys.readouterr().out
    assert "→ list_options" in out and "Done." in out
    transcripts = list((ctx.base_dir / "transcripts").glob("*.json"))
    assert len(transcripts) == 1
    saved = json.loads(transcripts[0].read_text())
    assert saved["provider"] == "scripted" and saved["steps"][0]["tool"] == "list_options"
