"""Pick an LLM provider and model from arguments or environment variables.

Environment variables:
    BEM_LLM_PROVIDER   groq (default) | anthropic | openai | gemini | openai_compatible
    BEM_LLM_MODEL      model name; defaults to the provider's default below
    BEM_LLM_BASE_URL   base URL, required for openai_compatible
    <provider key>     GROQ_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY,
                       or BEM_LLM_API_KEY for openai_compatible

Examples:
    BEM_LLM_PROVIDER=groq                                   # free tier, gpt-oss-120b
    BEM_LLM_PROVIDER=groq BEM_LLM_MODEL=llama-3.3-70b-versatile
    BEM_LLM_PROVIDER=anthropic BEM_LLM_MODEL=claude-opus-5-5
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .anthropic_client import AnthropicClient
from .base import LLMClient
from .openai_compat import OpenAICompatibleClient


@dataclass(frozen=True)
class ProviderPreset:
    name: str
    api_key_env: str
    default_model: str | None
    base_url: str | None = None
    kind: str = "openai"  # "openai" (chat completions protocol) or "anthropic"


PROVIDERS: dict[str, ProviderPreset] = {
    "groq": ProviderPreset(
        name="groq",
        api_key_env="GROQ_API_KEY",
        default_model="openai/gpt-oss-120b",
        base_url="https://api.groq.com/openai/v1",
    ),
    "anthropic": ProviderPreset(
        name="anthropic",
        api_key_env="ANTHROPIC_API_KEY",
        default_model="claude-sonnet-5-5",
        kind="anthropic",
    ),
    "openai": ProviderPreset(
        name="openai",
        api_key_env="OPENAI_API_KEY",
        default_model=None,  # set BEM_LLM_MODEL explicitly
    ),
    "gemini": ProviderPreset(
        name="gemini",
        api_key_env="GEMINI_API_KEY",
        default_model="gemini-3.5-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    ),
    "openai_compatible": ProviderPreset(
        name="openai_compatible",
        api_key_env="BEM_LLM_API_KEY",
        default_model=None,
    ),
}

DEFAULT_PROVIDER = "groq"


class LLMConfigError(ValueError):
    pass


def get_client(
    provider: str | None = None,
    model: str | None = None,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    **client_kwargs: Any,
) -> LLMClient:
    """Build an LLM client. Arguments take precedence over environment variables."""
    name = (provider or os.environ.get("BEM_LLM_PROVIDER") or DEFAULT_PROVIDER).lower()
    preset = PROVIDERS.get(name)
    if preset is None:
        raise LLMConfigError(
            f"Unknown LLM provider {name!r}. Choose one of: {', '.join(sorted(PROVIDERS))}"
        )

    model = model or os.environ.get("BEM_LLM_MODEL") or preset.default_model
    if not model:
        raise LLMConfigError(f"Provider {name!r} has no default model; set BEM_LLM_MODEL.")

    api_key = api_key or os.environ.get(preset.api_key_env)
    if not api_key and client_kwargs.get("sdk_client") is None:
        raise LLMConfigError(f"Missing API key: set {preset.api_key_env}.")

    if preset.kind == "anthropic":
        return AnthropicClient(model, api_key=api_key, **client_kwargs)

    base_url = base_url or os.environ.get("BEM_LLM_BASE_URL") or preset.base_url
    if name == "openai_compatible" and not base_url:
        raise LLMConfigError("Provider 'openai_compatible' needs BEM_LLM_BASE_URL.")
    return OpenAICompatibleClient(
        model, provider=name, api_key=api_key, base_url=base_url, **client_kwargs
    )
