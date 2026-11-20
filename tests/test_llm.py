import json
from types import SimpleNamespace as NS

import anthropic
import openai
import pytest

from bem_agent.llm import (
    AnthropicClient,
    LLMConfigError,
    Message,
    OpenAICompatibleClient,
    RateLimitExceeded,
    ToolCall,
    ToolSpec,
    get_client,
)
from bem_agent.llm.anthropic_client import to_anthropic_messages
from bem_agent.llm.openai_compat import to_openai_messages

TOOL = ToolSpec(
    name="run_simulation",
    description="Run EnergyPlus",
    parameters={"type": "object", "properties": {"building_id": {"type": "string"}}},
)

CONVERSATION = [
    Message.system("You are an energy modeler."),
    Message.user("Size HVAC for b1"),
    Message(
        role="assistant",
        content="Running it.",
        tool_calls=[ToolCall(id="c1", name="run_simulation", arguments={"building_id": "b1"})],
    ),
    Message.tool_result(ToolCall(id="c1", name="run_simulation", arguments={}), "peak 40 kW"),
]


# ---------- fakes ----------


class FakeOpenAI:
    def __init__(self, responses):
        self.calls = []
        self._responses = list(responses)
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        r = self._responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


class FakeAnthropic:
    def __init__(self, responses):
        self.calls = []
        self._responses = list(responses)
        self.messages = NS(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        r = self._responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


def openai_completion(content=None, tool_calls=(), finish="stop"):
    tcs = [
        NS(id=i, function=NS(name=n, arguments=a))
        for i, n, a in tool_calls  # noqa: B007
    ]
    return NS(
        choices=[NS(message=NS(content=content, tool_calls=tcs or None), finish_reason=finish)],
        usage=NS(prompt_tokens=100, completion_tokens=20),
    )


def _sdk_error(cls, headers=None):
    """Instantiate an SDK error without its HTTP-library-specific constructor."""
    err = cls.__new__(cls)
    Exception.__init__(err, "rate limited")
    err.response = NS(headers=headers or {})
    return err


def openai_rate_limit(retry_after="3"):
    return _sdk_error(openai.RateLimitError, {"retry-after": retry_after})


def anthropic_rate_limit():
    return _sdk_error(anthropic.RateLimitError)


# ---------- OpenAI-compatible (Groq) ----------


def test_openai_message_conversion():
    out = to_openai_messages(CONVERSATION)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool"]
    call = out[2]["tool_calls"][0]
    assert call["function"]["name"] == "run_simulation"
    assert json.loads(call["function"]["arguments"]) == {"building_id": "b1"}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "peak 40 kW"}


def test_openai_client_parses_tool_calls_and_usage():
    fake = FakeOpenAI(
        [
            openai_completion(
                tool_calls=[("c9", "run_simulation", '{"building_id": "b2"}')], finish="tool_calls"
            )
        ]
    )
    client = OpenAICompatibleClient("gpt-oss", provider="groq", sdk_client=fake)
    resp = client.chat(CONVERSATION, [TOOL])
    assert resp.tool_calls[0].arguments == {"building_id": "b2"}
    assert resp.tool_calls[0].parse_error is None
    assert resp.stop_reason == "tool_calls"
    assert resp.usage.total_tokens == 120
    sent = fake.calls[0]
    assert sent["model"] == "gpt-oss"
    assert sent["tools"][0]["function"]["name"] == "run_simulation"


def test_openai_client_flags_malformed_arguments():
    fake = FakeOpenAI([openai_completion(tool_calls=[("c1", "run_simulation", "{not json")])])
    resp = OpenAICompatibleClient("m", sdk_client=fake).chat([Message.user("hi")], [TOOL])
    assert resp.tool_calls[0].arguments == {}
    assert "invalid JSON" in resp.tool_calls[0].parse_error


def test_openai_client_omits_tools_when_none():
    fake = FakeOpenAI([openai_completion(content="hello")])
    resp = OpenAICompatibleClient("m", sdk_client=fake).chat([Message.user("hi")])
    assert resp.text == "hello"
    assert "tools" not in fake.calls[0]


def test_retry_uses_retry_after_header():
    slept = []
    fake = FakeOpenAI([openai_rate_limit("3"), openai_completion(content="ok")])
    client = OpenAICompatibleClient("m", sdk_client=fake, sleep=slept.append)
    assert client.chat([Message.user("hi")]).text == "ok"
    assert slept == [3.0]


def test_retry_gives_up():
    fake = FakeOpenAI([openai_rate_limit() for _ in range(3)])
    client = OpenAICompatibleClient("m", sdk_client=fake, max_retries=2, sleep=lambda s: None)
    with pytest.raises(RateLimitExceeded):
        client.chat([Message.user("hi")])


# ---------- Anthropic (Claude) ----------


def test_anthropic_message_conversion():
    system, out = to_anthropic_messages(CONVERSATION)
    assert system == "You are an energy modeler."
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[1]["content"][1] == {
        "type": "tool_use",
        "id": "c1",
        "name": "run_simulation",
        "input": {"building_id": "b1"},
    }
    assert out[2]["content"][0]["type"] == "tool_result"
    assert out[2]["content"][0]["tool_use_id"] == "c1"


def test_anthropic_merges_consecutive_tool_results():
    calls = [ToolCall(id=f"c{i}", name="run_simulation", arguments={}) for i in range(2)]
    msgs = [
        Message.user("go"),
        Message(role="assistant", tool_calls=calls),
        *(Message.tool_result(c, "done") for c in calls),
    ]
    _, out = to_anthropic_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert len(out[2]["content"]) == 2


def test_anthropic_client_parses_response():
    response = NS(
        content=[
            NS(type="text", text="Running."),
            NS(type="tool_use", id="t1", name="run_simulation", input={"building_id": "b1"}),
        ],
        stop_reason="tool_use",
        usage=NS(input_tokens=50, output_tokens=10),
    )
    fake = FakeAnthropic([anthropic_rate_limit(), response])
    client = AnthropicClient("claude-opus-5-5", sdk_client=fake, sleep=lambda s: None)
    resp = client.chat(CONVERSATION, [TOOL])
    assert resp.text == "Running."
    assert resp.tool_calls[0].name == "run_simulation"
    assert resp.usage.total_tokens == 60
    sent = fake.calls[-1]
    assert sent["system"] == "You are an energy modeler."
    assert sent["tools"][0]["input_schema"] == TOOL.parameters


# ---------- config ----------


@pytest.fixture
def clean_env(monkeypatch):
    for var in [
        "BEM_LLM_PROVIDER",
        "BEM_LLM_MODEL",
        "BEM_LLM_BASE_URL",
        "GROQ_API_KEY",
        "ANTHROPIC_API_KEY",
        "BEM_LLM_API_KEY",
    ]:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_default_provider_is_groq(clean_env):
    clean_env.setenv("GROQ_API_KEY", "k")
    client = get_client()
    assert isinstance(client, OpenAICompatibleClient)
    assert client.provider == "groq"
    assert client.model == "openai/gpt-oss-120b"
    assert str(client._client.base_url).startswith("https://api.groq.com/openai/v1")


def test_switch_to_claude_by_env(clean_env):
    clean_env.setenv("BEM_LLM_PROVIDER", "anthropic")
    clean_env.setenv("BEM_LLM_MODEL", "claude-opus-5-5")
    clean_env.setenv("ANTHROPIC_API_KEY", "k")
    client = get_client()
    assert isinstance(client, AnthropicClient)
    assert client.model == "claude-opus-5-5"


def test_arguments_override_env(clean_env):
    clean_env.setenv("BEM_LLM_PROVIDER", "anthropic")
    client = get_client("groq", "llama-3.3-70b-versatile", api_key="k")
    assert client.provider == "groq"
    assert client.model == "llama-3.3-70b-versatile"


def test_config_errors(clean_env):
    with pytest.raises(LLMConfigError, match="GROQ_API_KEY"):
        get_client("groq")
    with pytest.raises(LLMConfigError, match="Unknown"):
        get_client("nope", api_key="k")
    with pytest.raises(LLMConfigError, match="BEM_LLM_MODEL"):
        get_client("openai", api_key="k")
    with pytest.raises(LLMConfigError, match="BASE_URL"):
        get_client("openai_compatible", model="m", api_key="k")
