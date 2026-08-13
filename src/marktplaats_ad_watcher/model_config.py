from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ModelProtocol = Literal["openai_chat", "openai_responses", "anthropic_messages"]
REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    label: str
    protocol: ModelProtocol
    base_url: str
    model: str
    reasoning_effort: str | None = None
    json_mode: bool = True
    supports_reasoning_effort: bool = False
    allowed_reasoning_efforts: frozenset[str] = frozenset()
    supports_temperature: bool = True
    temperature_requires_no_reasoning: bool = False
    help_text: str = ""


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "deepseek": ProviderPreset(
        id="deepseek",
        label="DeepSeek",
        protocol="openai_chat",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-v4-flash",
        help_text=(
            "Uses OpenAI-compatible Chat Completions. Temperature is supported; a separate "
            "reasoning-effort field is not sent."
        ),
    ),
    "openai": ProviderPreset(
        id="openai",
        label="OpenAI",
        protocol="openai_responses",
        base_url="https://api.openai.com/v1",
        model="gpt-5.6-luna",
        reasoning_effort="medium",
        supports_reasoning_effort=True,
        allowed_reasoning_efforts=frozenset(REASONING_EFFORTS),
        temperature_requires_no_reasoning=True,
        help_text=(
            "Uses OpenAI's native Responses API. Temperature is sent only when reasoning is "
            "disabled."
        ),
    ),
    "gemini": ProviderPreset(
        id="gemini",
        label="Google Gemini (OpenAI compatibility)",
        protocol="openai_chat",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        model="gemini-3.5-flash-lite",
        supports_reasoning_effort=True,
        allowed_reasoning_efforts=frozenset({"minimal", "low", "medium", "high"}),
        help_text=(
            "Uses Gemini's OpenAI-compatible API. Reasoning and temperature support still "
            "depends on the selected Gemini model."
        ),
    ),
    "anthropic": ProviderPreset(
        id="anthropic",
        label="Anthropic Claude",
        protocol="anthropic_messages",
        base_url="https://api.anthropic.com",
        model="claude-haiku-4-5",
        supports_temperature=False,
        help_text=(
            "Uses Anthropic's native Messages API. The Haiku preset does not send reasoning "
            "effort or temperature controls."
        ),
    ),
    "openai-compatible": ProviderPreset(
        id="openai-compatible",
        label="Custom OpenAI-compatible API",
        protocol="openai_chat",
        base_url="",
        model="",
        help_text=(
            "Uses the portable OpenAI Chat Completions fields. Provider-specific reasoning "
            "controls are not sent."
        ),
    ),
}

def provider_preset(provider: str) -> ProviderPreset:
    normalized = provider.strip().lower()
    try:
        return PROVIDER_PRESETS[normalized]
    except KeyError as error:
        supported = ", ".join(PROVIDER_PRESETS)
        raise ValueError(f"MODEL_PROVIDER must be one of: {supported}.") from error


def resolved_model_environment(values: dict[str, str]) -> dict[str, str]:
    """Return UI-friendly generic model values, including legacy DeepSeek migration."""
    resolved = dict(values)
    provider = resolved.get("MODEL_PROVIDER", "").strip().lower()
    if not provider:
        provider = "deepseek"
    resolved["MODEL_PROVIDER"] = provider

    preset = provider_preset(provider)
    legacy_deepseek = provider == "deepseek"

    aliases = {
        "MODEL_API_KEY": "DEEPSEEK_API_KEY",
        "MODEL_BASE_URL": "DEEPSEEK_BASE_URL",
        "MODEL_NAME": "DEEPSEEK_MODEL",
        "MODEL_TEMPERATURE": "DEEPSEEK_TEMPERATURE",
        "MODEL_MAX_TOKENS": "DEEPSEEK_MAX_TOKENS",
    }
    for generic, legacy in aliases.items():
        if generic not in resolved and legacy_deepseek and legacy in resolved:
            resolved[generic] = resolved[legacy]

    resolved.setdefault("MODEL_BASE_URL", preset.base_url)
    resolved.setdefault("MODEL_NAME", preset.model)
    resolved.setdefault("MODEL_TEMPERATURE", "0")
    resolved.setdefault("MODEL_MAX_TOKENS", "700")
    resolved.setdefault("MODEL_REASONING_EFFORT", preset.reasoning_effort or "")
    resolved.setdefault("MODEL_JSON_MODE", "true" if preset.json_mode else "false")
    return resolved
