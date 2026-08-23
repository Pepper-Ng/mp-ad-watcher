from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from marktplaats_ad_watcher.config import Settings
from marktplaats_ad_watcher.evaluation import build_evaluation_prompt
from marktplaats_ad_watcher.model_providers import (
    AnthropicMessagesEvaluator,
    FallbackEvaluator,
    ModelOutputError,
    ModelTransportError,
    OpenAICompatibleEvaluator,
    OpenAIResponsesEvaluator,
    build_model_evaluator,
)
from marktplaats_ad_watcher.models import Ad
from marktplaats_ad_watcher.usage import ModelUsageStore


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    settings = Settings(
        marktplaats_search_url="https://www.marktplaats.nl/lrp/api/search?query=test",
        marktplaats_use_case="Find chest freezers.",
        poll_interval_seconds=600,
        max_ads_per_poll=30,
        bootstrap_existing_ads=False,
        exclude_admarkt_ads=True,
        notify_min_confidence=0.65,
        review_min_confidence=0.0,
        notify_review_actions=True,
        model_provider="deepseek",
        model_api_key="dummy-key",
        model_base_url="https://api.deepseek.com/v1",
        model_name="deepseek-v4-flash",
        model_temperature=0.0,
        model_max_tokens=700,
        model_reasoning_effort=None,
        model_json_mode=True,
        notify_ai_failures=True,
        fallback_model_enabled=False,
        fallback_model_provider=None,
        fallback_model_api_key=None,
        fallback_model_base_url=None,
        fallback_model_name=None,
        fallback_model_temperature=0.0,
        fallback_model_max_tokens=700,
        fallback_model_reasoning_effort=None,
        fallback_model_json_mode=False,
        send_image_content_to_model=False,
        max_images_for_model=3,
        telegram_bot_token=None,
        telegram_chat_id=None,
        telegram_disable_web_page_preview=False,
        state_file=tmp_path / "seen.json",
        results_file=tmp_path / "results.jsonl",
        status_file=tmp_path / "status.json",
        request_timeout_seconds=20.0,
        user_agent="test",
        web_admin_token=None,
    )
    return replace(settings, **overrides)


def _ad() -> Ad:
    return Ad(
        id="m1",
        title="Chest freezer",
        url="https://example.test/m1",
        image_urls=["https://example.test/1.jpg", "https://example.test/2.jpg"],
    )


def test_provider_factory_selects_protocol_adapter(tmp_path: Path) -> None:
    assert isinstance(build_model_evaluator(_settings(tmp_path)), OpenAICompatibleEvaluator)
    assert isinstance(
        build_model_evaluator(
            _settings(
                tmp_path,
                model_provider="openai",
                model_base_url="https://api.openai.com/v1",
                model_name="gpt-5.6-luna",
                model_reasoning_effort="medium",
            )
        ),
        OpenAIResponsesEvaluator,
    )
    assert isinstance(
        build_model_evaluator(
            _settings(
                tmp_path,
                model_provider="anthropic",
                model_base_url="https://api.anthropic.com",
                model_name="claude-haiku-4-5",
            )
        ),
        AnthropicMessagesEvaluator,
    )


def test_openai_compatible_request_supports_json_reasoning_and_images(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        model_provider="gemini",
        model_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        model_name="gemini-3.5-flash-lite",
        model_reasoning_effort="low",
        send_image_content_to_model=True,
        max_images_for_model=1,
    )
    evaluator = OpenAICompatibleEvaluator(settings)

    endpoint, headers, payload = evaluator.request(
        build_evaluation_prompt(settings.marktplaats_use_case, _ad(), include_image_content=True)
    )

    assert endpoint.endswith("/v1beta/openai/chat/completions")
    assert headers["Authorization"] == "Bearer dummy-key"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["reasoning_effort"] == "low"
    assert len(payload["messages"][1]["content"]) == 2


def test_deepseek_omits_unsupported_reasoning_effort(tmp_path: Path) -> None:
    settings = _settings(tmp_path, model_reasoning_effort="medium")
    evaluator = OpenAICompatibleEvaluator(settings)

    _, _, payload = evaluator.request(build_evaluation_prompt(settings.marktplaats_use_case, _ad()))

    assert "reasoning_effort" not in payload


def test_openai_responses_request_uses_native_schema_reasoning_and_images(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        model_provider="openai",
        model_base_url="https://api.openai.com/v1",
        model_name="gpt-5.6-luna",
        model_reasoning_effort="medium",
        send_image_content_to_model=True,
        max_images_for_model=1,
    )
    evaluator = OpenAIResponsesEvaluator(settings)

    endpoint, headers, payload = evaluator.request(
        build_evaluation_prompt(settings.marktplaats_use_case, _ad(), include_image_content=True)
    )

    assert endpoint == "https://api.openai.com/v1/responses"
    assert headers["Authorization"] == "Bearer dummy-key"
    assert payload["reasoning"] == {"effort": "medium"}
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["store"] is False
    assert payload["input"][0]["content"][1] == {
        "type": "input_image",
        "image_url": "https://example.test/1.jpg",
        "detail": "low",
    }


def test_openai_sends_temperature_only_when_reasoning_is_disabled(tmp_path: Path) -> None:
    base = _settings(
        tmp_path,
        model_provider="openai",
        model_base_url="https://api.openai.com/v1",
        model_name="gpt-5.6-luna",
        model_temperature=0.4,
        model_reasoning_effort="medium",
    )
    with_reasoning = OpenAIResponsesEvaluator(base)
    without_reasoning = OpenAIResponsesEvaluator(
        replace(base, model_reasoning_effort="none")
    )
    prompt = build_evaluation_prompt(base.marktplaats_use_case, _ad())

    _, _, reasoning_payload = with_reasoning.request(prompt)
    _, _, no_reasoning_payload = without_reasoning.request(prompt)

    assert "temperature" not in reasoning_payload
    assert no_reasoning_payload["temperature"] == 0.4


def test_anthropic_request_uses_native_headers_schema_and_images(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        model_provider="anthropic",
        model_base_url="https://api.anthropic.com",
        model_name="claude-haiku-4-5",
        model_reasoning_effort="high",
        send_image_content_to_model=True,
        max_images_for_model=1,
    )
    evaluator = AnthropicMessagesEvaluator(settings)

    endpoint, headers, payload = evaluator.request(
        build_evaluation_prompt(settings.marktplaats_use_case, _ad(), include_image_content=True)
    )

    assert endpoint == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "dummy-key"
    assert headers["anthropic-version"] == "2023-06-01"
    assert payload["output_config"]["format"]["type"] == "json_schema"
    assert payload["messages"][0]["content"][0] == {
        "type": "image",
        "source": {"type": "url", "url": "https://example.test/1.jpg"},
    }
    assert "temperature" not in payload
    assert "effort" not in payload["output_config"]


@pytest.mark.asyncio
async def test_openai_compatible_evaluator_parses_valid_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class StubAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> StubAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def post(
            self,
            endpoint: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            captured.update(endpoint=endpoint, headers=headers, payload=json)
            return httpx.Response(
                200,
                request=httpx.Request("POST", endpoint),
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"relevant":true,"confidence":0.9,'
                                    '"reason":"Good match.","signals":[],'
                                    '"concerns":[],"next_action":"notify"}'
                                )
                            }
                        }
                    ]
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", StubAsyncClient)
    result = await OpenAICompatibleEvaluator(_settings(tmp_path)).evaluate(_ad())

    assert result.next_action == "notify"
    assert captured["endpoint"] == "https://api.deepseek.com/v1/chat/completions"


def test_native_response_parsers_accept_documented_shapes(tmp_path: Path) -> None:
    expected = (
        '{"relevant":false,"confidence":0.2,"reason":"No match.",'
        '"signals":[],"concerns":[],"next_action":"ignore"}'
    )
    openai = OpenAIResponsesEvaluator(
        _settings(
            tmp_path,
            model_provider="openai",
            model_base_url="https://api.openai.com/v1",
            model_name="gpt-5.6-luna",
        )
    )
    anthropic = AnthropicMessagesEvaluator(
        _settings(
            tmp_path,
            model_provider="anthropic",
            model_base_url="https://api.anthropic.com",
            model_name="claude-haiku-4-5",
        )
    )

    assert openai.response_text({"status": "completed", "output_text": expected}) == expected
    assert anthropic.response_text({"content": [{"type": "text", "text": expected}]}) == expected


@pytest.mark.asyncio
async def test_parse_failure_after_successful_http_call_counts_toward_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            del timeout

        async def __aenter__(self) -> StubAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def post(
            self,
            endpoint: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            del headers, json
            return httpx.Response(
                200,
                request=httpx.Request("POST", endpoint),
                json={"choices": [{"message": {"content": "not-json"}}]},
            )

    monkeypatch.setattr(httpx, "AsyncClient", StubAsyncClient)

    with pytest.raises(ModelOutputError):
        await OpenAICompatibleEvaluator(_settings(tmp_path)).evaluate(_ad())

    assert ModelUsageStore(_settings(tmp_path).global_model_usage_file).snapshot().used == 1


@pytest.mark.asyncio
async def test_fallback_model_is_used_when_primary_model_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            del timeout

        async def __aenter__(self) -> StubAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def post(
            self,
            endpoint: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            del headers, json
            if "primary.example.test" in endpoint:
                return httpx.Response(
                    503,
                    request=httpx.Request("POST", endpoint),
                    json={"error": {"message": "upstream unavailable"}},
                )
            return httpx.Response(
                200,
                request=httpx.Request("POST", endpoint),
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"relevant":false,"confidence":0.2,'
                                    '"reason":"Fallback succeeded.","signals":[],'
                                    '"concerns":[],"next_action":"ignore"}'
                                )
                            }
                        }
                    ]
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", StubAsyncClient)
    evaluator = build_model_evaluator(
        _settings(
            tmp_path,
            model_provider="openai-compatible",
            model_api_key="primary-key",
            model_base_url="https://primary.example.test/v1",
            model_name="cheap-model",
            model_json_mode=False,
            fallback_model_enabled=True,
            fallback_model_provider="openai-compatible",
            fallback_model_api_key="fallback-key",
            fallback_model_base_url="https://fallback.example.test/v1",
            fallback_model_name="reliable-model",
            fallback_model_json_mode=True,
        )
    )

    assert isinstance(evaluator, FallbackEvaluator)
    result = await evaluator.evaluate(_ad())

    assert result.reason == "Fallback succeeded."


@pytest.mark.asyncio
async def test_transport_error_retries_once_before_failing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class StubAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            del timeout

        async def __aenter__(self) -> StubAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def post(
            self,
            endpoint: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            del headers, json
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                request = httpx.Request("POST", endpoint)
                raise httpx.ReadTimeout("temporary timeout", request=request)
            return httpx.Response(
                200,
                request=httpx.Request("POST", endpoint),
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"relevant":false,"confidence":0.2,'
                                    '"reason":"Retried successfully.","signals":[],'
                                    '"concerns":[],"next_action":"ignore"}'
                                )
                            }
                        }
                    ]
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", StubAsyncClient)

    result = await OpenAICompatibleEvaluator(_settings(tmp_path)).evaluate(_ad())

    assert attempts == 2
    assert result.reason == "Retried successfully."


@pytest.mark.asyncio
async def test_transport_error_reports_underlying_httpx_subtype(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            del timeout

        async def __aenter__(self) -> StubAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def post(
            self,
            endpoint: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            del headers, json
            request = httpx.Request("POST", endpoint)
            raise httpx.ConnectError("connection reset", request=request)

    monkeypatch.setattr(httpx, "AsyncClient", StubAsyncClient)

    with pytest.raises(ModelTransportError, match="ConnectError while calling"):
        await OpenAICompatibleEvaluator(_settings(tmp_path)).evaluate(_ad())
