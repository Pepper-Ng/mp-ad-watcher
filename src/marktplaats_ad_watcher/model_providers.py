from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from marktplaats_ad_watcher.config import Settings
from marktplaats_ad_watcher.diagnostics import ModelCallAuditStore
from marktplaats_ad_watcher.evaluation import (
    EVALUATION_JSON_SCHEMA,
    EvaluationPrompt,
    Evaluator,
    build_evaluation_prompt,
    parse_evaluation,
)
from marktplaats_ad_watcher.model_config import ModelProtocol, provider_preset
from marktplaats_ad_watcher.models import Ad, EvaluationResult
from marktplaats_ad_watcher.usage import ModelDailyLimitExceeded, ModelUsageStore


class ProviderAdapter(Protocol):
    async def evaluate(self, ad: Ad) -> EvaluationResult: ...


class ModelEvaluationError(RuntimeError):
    attempt_consumed = True


class ModelTransportError(ModelEvaluationError):
    attempt_consumed = False


class ModelProviderError(ModelEvaluationError):
    pass


class ModelOutputError(ModelEvaluationError):
    pass


class FallbackModelError(ModelEvaluationError):
    def __init__(self, primary_error: Exception, fallback_error: Exception) -> None:
        self.primary_error = primary_error
        self.fallback_error = fallback_error
        self.attempt_consumed = bool(
            getattr(primary_error, "attempt_consumed", True)
            or getattr(fallback_error, "attempt_consumed", True)
        )
        message = (
            f"Primary model failed: {type(primary_error).__name__}: {primary_error}. "
            f"Fallback model failed: {type(fallback_error).__name__}: {fallback_error}"
        )
        super().__init__(message)


class HttpModelEvaluator(ABC):
    def __init__(self, settings: Settings) -> None:
        if not settings.model_api_key:
            raise ValueError("MODEL_API_KEY is required for normal evaluation runs.")
        self._settings = settings
        self._preset = provider_preset(settings.model_provider)
        self._usage = ModelUsageStore(settings.global_model_usage_file)
        self._audit = ModelCallAuditStore(settings.data_root / "model_calls.jsonl")

    async def evaluate(self, ad: Ad) -> EvaluationResult:
        prompt = build_evaluation_prompt(
            self._settings.marktplaats_use_case,
            ad,
            include_image_content=self._settings.send_image_content_to_model,
        )
        endpoint, headers, payload = self.request(prompt)
        reservation = self._usage.acquire()
        response: httpx.Response | None = None
        last_transport_error: httpx.RequestError | None = None
        for _attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    timeout=self._settings.request_timeout_seconds
                ) as client:
                    response = await client.post(endpoint, headers=headers, json=payload)
                last_transport_error = None
                break
            except httpx.RequestError as error:
                last_transport_error = error
                self._record_audit(
                    outcome="transport_error",
                    error=_transport_error_message(error, endpoint),
                )
                continue
            except Exception:
                reservation.release()
                raise

        if last_transport_error is not None:
            reservation.release()
            raise ModelTransportError(
                _transport_error_message(last_transport_error, endpoint)
            ) from last_transport_error
        assert response is not None

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            reservation.commit()
            self._record_audit(
                outcome=f"http_{response.status_code}",
                error=str(error),
                response=response.text,
            )
            raise ModelProviderError(str(error)) from error

        response_text = ""
        response_detail = ""
        try:
            response_payload = response.json()
            response_detail = _serialize_response(response_payload)
            if not isinstance(response_payload, dict):
                raise ModelOutputError(
                    "Model response was not a JSON object at the protocol level."
                )
            response_text = self.response_text(response_payload)
            result = parse_evaluation(response_text)
        except ModelOutputError as error:
            reservation.commit()
            self._record_audit(
                outcome="invalid_output",
                error=str(error),
                response=response_text or response_detail,
            )
            raise
        except Exception as error:
            reservation.commit()
            self._record_audit(
                outcome="invalid_output",
                error=str(error),
                response=response_text or response_detail,
            )
            raise ModelOutputError(str(error)) from error

        reservation.commit()
        self._record_audit(
            outcome="success",
            response=response_text,
            action=result.next_action,
            confidence=f"{result.confidence:.2f}",
        )
        return result

    @abstractmethod
    def request(self, prompt: EvaluationPrompt) -> tuple[str, dict[str, str], dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def response_text(self, response_payload: dict[str, Any]) -> str:
        raise NotImplementedError

    def common_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "User-Agent": self._settings.user_agent,
        }

    def _record_audit(
        self,
        *,
        outcome: str,
        response: str = "",
        error: str = "",
        action: str = "",
        confidence: str = "",
    ) -> None:
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "profile_id": self._settings.active_profile_id or "legacy",
            "profile_name": self._settings.active_profile_name or "",
            "provider": self._settings.model_provider,
            "model": self._settings.model_name,
            "outcome": outcome,
            "action": action,
            "confidence": confidence,
            "error": error[:1200],
            "response": response[:8000],
        }
        with suppress(OSError):
            self._audit.append(record)


class OpenAICompatibleEvaluator(HttpModelEvaluator):
    """OpenAI Chat Completions-compatible adapter used by DeepSeek, Gemini, and custom APIs."""

    def request(self, prompt: EvaluationPrompt) -> tuple[str, dict[str, str], dict[str, Any]]:
        user_content: str | list[dict[str, Any]]
        if self._settings.send_image_content_to_model and prompt.image_urls:
            user_content = [{"type": "text", "text": prompt.user}]
            for image_url in prompt.image_urls[: self._settings.max_images_for_model]:
                user_content.append({"type": "image_url", "image_url": {"url": image_url}})
        else:
            user_content = prompt.user

        payload: dict[str, Any] = {
            "model": self._settings.model_name,
            "temperature": self._settings.model_temperature,
            "max_tokens": self._settings.model_max_tokens,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": user_content},
            ],
        }
        if self._settings.model_json_mode:
            payload["response_format"] = {"type": "json_object"}
        if (
            self._preset.supports_reasoning_effort
            and self._settings.model_reasoning_effort
            and self._settings.model_reasoning_effort
            in self._preset.allowed_reasoning_efforts
        ):
            payload["reasoning_effort"] = self._settings.model_reasoning_effort

        headers = self.common_headers()
        headers["Authorization"] = f"Bearer {self._settings.model_api_key}"
        return _endpoint(self._settings.model_base_url, "chat/completions"), headers, payload

    def response_text(self, response_payload: dict[str, Any]) -> str:
        try:
            content = response_payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError(
                f"Unexpected OpenAI-compatible response shape: {response_payload}"
            ) from error
        if not isinstance(content, str):
            raise ValueError(
                f"Unexpected OpenAI-compatible assistant content type: {type(content).__name__}"
            )
        return content


class OpenAIResponsesEvaluator(HttpModelEvaluator):
    """Native OpenAI Responses API adapter for current OpenAI reasoning and vision models."""

    def request(self, prompt: EvaluationPrompt) -> tuple[str, dict[str, str], dict[str, Any]]:
        input_content: str | list[dict[str, Any]]
        if self._settings.send_image_content_to_model and prompt.image_urls:
            input_content = [{"type": "input_text", "text": prompt.user}]
            for image_url in prompt.image_urls[: self._settings.max_images_for_model]:
                input_content.append(
                    {"type": "input_image", "image_url": image_url, "detail": "low"}
                )
        else:
            input_content = prompt.user

        payload: dict[str, Any] = {
            "model": self._settings.model_name,
            "instructions": prompt.system,
            "input": [{"role": "user", "content": input_content}],
            "max_output_tokens": self._settings.model_max_tokens,
            "store": False,
        }
        reasoning_disabled = self._settings.model_reasoning_effort in {None, "none"}
        if (
            self._preset.supports_temperature
            and self._settings.model_temperature > 0
            and (
                not self._preset.temperature_requires_no_reasoning
                or reasoning_disabled
            )
        ):
            payload["temperature"] = self._settings.model_temperature
        if (
            self._preset.supports_reasoning_effort
            and self._settings.model_reasoning_effort
            and self._settings.model_reasoning_effort
            in self._preset.allowed_reasoning_efforts
        ):
            payload["reasoning"] = {"effort": self._settings.model_reasoning_effort}
        if self._settings.model_json_mode:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "marktplaats_ad_evaluation",
                    "strict": True,
                    "schema": EVALUATION_JSON_SCHEMA,
                }
            }

        headers = self.common_headers()
        headers["Authorization"] = f"Bearer {self._settings.model_api_key}"
        return _endpoint(self._settings.model_base_url, "responses"), headers, payload

    def response_text(self, response_payload: dict[str, Any]) -> str:
        status = response_payload.get("status")
        if status not in {None, "completed"}:
            raise ValueError(f"OpenAI response did not complete successfully: {status}")

        output_text = response_payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text

        for output in response_payload.get("output", []):
            if not isinstance(output, dict) or output.get("type") != "message":
                continue
            for content in output.get("content", []):
                if not isinstance(content, dict):
                    continue
                if content.get("type") == "refusal":
                    raise ValueError(
                        f"OpenAI model refused the evaluation: {content.get('refusal')}"
                    )
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    return content["text"]

        raise ValueError(f"Unexpected OpenAI Responses API shape: {response_payload}")


class AnthropicMessagesEvaluator(HttpModelEvaluator):
    """Native Anthropic Messages API adapter with structured output and URL-based images."""

    def request(self, prompt: EvaluationPrompt) -> tuple[str, dict[str, str], dict[str, Any]]:
        user_content: str | list[dict[str, Any]]
        if self._settings.send_image_content_to_model and prompt.image_urls:
            user_content = []
            for image_url in prompt.image_urls[: self._settings.max_images_for_model]:
                user_content.append(
                    {"type": "image", "source": {"type": "url", "url": image_url}}
                )
            user_content.append({"type": "text", "text": prompt.user})
        else:
            user_content = prompt.user

        payload: dict[str, Any] = {
            "model": self._settings.model_name,
            "max_tokens": self._settings.model_max_tokens,
            "system": prompt.system,
            "messages": [{"role": "user", "content": user_content}],
        }
        output_config: dict[str, Any] = {}
        if (
            self._preset.supports_reasoning_effort
            and self._settings.model_reasoning_effort
            and self._settings.model_reasoning_effort
            in self._preset.allowed_reasoning_efforts
        ):
            output_config["effort"] = self._settings.model_reasoning_effort
        if self._settings.model_json_mode:
            output_config["format"] = {
                "type": "json_schema",
                "schema": EVALUATION_JSON_SCHEMA,
            }
        if output_config:
            payload["output_config"] = output_config

        headers = self.common_headers()
        headers["x-api-key"] = self._settings.model_api_key or ""
        headers["anthropic-version"] = "2023-06-01"
        return _endpoint(self._settings.model_base_url, "v1/messages"), headers, payload

    def response_text(self, response_payload: dict[str, Any]) -> str:
        text_parts = [
            block["text"]
            for block in response_payload.get("content", [])
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        if text_parts:
            return "".join(text_parts)
        raise ValueError(f"Unexpected Anthropic Messages API shape: {response_payload}")


AdapterFactory = Callable[[Settings], Evaluator]
ADAPTERS: dict[ModelProtocol, AdapterFactory] = {
    "openai_chat": OpenAICompatibleEvaluator,
    "openai_responses": OpenAIResponsesEvaluator,
    "anthropic_messages": AnthropicMessagesEvaluator,
}


def build_model_evaluator(settings: Settings) -> Evaluator:
    preset = provider_preset(settings.model_provider)
    try:
        adapter = ADAPTERS[preset.protocol]
    except KeyError as error:
        raise ValueError(f"No evaluator adapter is registered for {preset.protocol}.") from error
    primary = adapter(settings)
    if not settings.fallback_model_enabled:
        return primary

    fallback_provider = settings.fallback_model_provider
    if not fallback_provider:
        raise ValueError("FALLBACK_MODEL_PROVIDER is required when fallback is enabled.")
    fallback_settings = replace(
        settings,
        model_provider=fallback_provider,
        model_api_key=settings.fallback_model_api_key,
        model_base_url=settings.fallback_model_base_url or "",
        model_name=settings.fallback_model_name or "",
        model_temperature=settings.fallback_model_temperature,
        model_max_tokens=settings.fallback_model_max_tokens,
        model_reasoning_effort=settings.fallback_model_reasoning_effort,
        model_json_mode=settings.fallback_model_json_mode,
        fallback_model_enabled=False,
    )
    fallback_preset = provider_preset(fallback_provider)
    try:
        fallback_adapter = ADAPTERS[fallback_preset.protocol]
    except KeyError as error:
        raise ValueError(
            f"No evaluator adapter is registered for fallback protocol {fallback_preset.protocol}."
        ) from error
    return FallbackEvaluator(primary=primary, fallback=fallback_adapter(fallback_settings))


class FallbackEvaluator:
    def __init__(self, *, primary: Evaluator, fallback: Evaluator) -> None:
        self._primary = primary
        self._fallback = fallback

    async def evaluate(self, ad: Ad) -> EvaluationResult:
        try:
            return await self._primary.evaluate(ad)
        except ModelDailyLimitExceeded:
            raise
        except Exception as primary_error:
            try:
                return await self._fallback.evaluate(ad)
            except ModelDailyLimitExceeded:
                raise
            except Exception as fallback_error:
                raise FallbackModelError(primary_error, fallback_error) from fallback_error


def _endpoint(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _serialize_response(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _transport_error_message(error: httpx.RequestError, endpoint: str) -> str:
    host = urlsplit(endpoint).netloc or endpoint
    detail = str(error).strip() or "No transport details were supplied."
    return f"{type(error).__name__} while calling {host}: {detail}"
