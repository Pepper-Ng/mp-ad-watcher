from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from marktplaats_ad_watcher.config import Settings
from marktplaats_ad_watcher.evaluation import (
    EVALUATION_JSON_SCHEMA,
    EvaluationPrompt,
    Evaluator,
    build_evaluation_prompt,
    parse_evaluation,
)
from marktplaats_ad_watcher.model_config import ModelProtocol, provider_preset
from marktplaats_ad_watcher.models import Ad, EvaluationResult
from marktplaats_ad_watcher.usage import ModelUsageStore

LOGGER = logging.getLogger(__name__)


class ModelProviderError(RuntimeError):
    def __init__(self, *, status_code: int, code: str | None, message: str) -> None:
        self.status_code = status_code
        self.code = code
        code_text = f" ({code})" if code else ""
        super().__init__(f"Model provider returned HTTP {status_code}{code_text}: {message}")


class ProviderAdapter(Protocol):
    async def evaluate(self, ad: Ad) -> EvaluationResult: ...


class HttpModelEvaluator(ABC):
    def __init__(self, settings: Settings) -> None:
        if not settings.model_api_key:
            raise ValueError("MODEL_API_KEY is required for normal evaluation runs.")
        self._settings = settings
        self._preset = provider_preset(settings.model_provider)
        self._usage = ModelUsageStore(settings.global_model_usage_file)

    async def evaluate(self, ad: Ad) -> EvaluationResult:
        prompt = build_evaluation_prompt(
            self._settings.marktplaats_use_case,
            ad,
            include_image_content=self._settings.send_image_content_to_model,
        )
        endpoint, headers, payload = self.request(prompt)
        reservation = self._usage.acquire()
        try:
            async with httpx.AsyncClient(timeout=self._settings.request_timeout_seconds) as client:
                response = await client.post(endpoint, headers=headers, json=payload)
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as error:
                    code, message = _provider_error_details(response)
                    raise ModelProviderError(
                        status_code=response.status_code,
                        code=code,
                        message=message,
                    ) from error
        except Exception:
            reservation.release()
            raise

        response_payload: dict[str, Any] | None = None
        try:
            parsed_payload = response.json()
            if not isinstance(parsed_payload, dict):
                raise ValueError("Model response was not a JSON object at the protocol level.")
            response_payload = parsed_payload
            response_text = self.response_text(response_payload)
            result = parse_evaluation(response_text)
        except Exception:
            LOGGER.warning(
                "%sModel response could not be parsed into a valid evaluation for ad %s; "
                "releasing its model-budget reservation.",
                _profile_log_context(self._settings),
                ad.id,
                extra={"diagnostic_detail": _diagnostic_response_payload(response_payload)},
            )
            reservation.release()
            raise

        reservation.commit()
        profile_context = _profile_log_context(self._settings)
        LOGGER.info(
            "%sModel response received for ad %s.",
            profile_context,
            ad.id,
            extra={"diagnostic_detail": response_text[:8000]},
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


class OpenAICompatibleEvaluator(HttpModelEvaluator):
    """OpenAI Chat Completions-compatible adapter used by DeepSeek, Gemini, and custom APIs."""

    def request(self, prompt: EvaluationPrompt) -> tuple[str, dict[str, str], dict[str, Any]]:
        user_content: str | list[dict[str, Any]]
        if prompt.image_urls:
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
            choice = response_payload["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError(
                f"Unexpected OpenAI-compatible response shape: {response_payload}"
            ) from error

        content = message.get("content") if isinstance(message, dict) else None
        text = _message_text(content)
        if text:
            return text

        if isinstance(message, dict):
            for field in ("text", "output_text"):
                text = _message_text(message.get(field))
                if text:
                    return text

            for field in ("reasoning_content", "reasoning"):
                reasoning = message.get(field)
                if isinstance(reasoning, str) and _contains_json_object(reasoning):
                    return reasoning

        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        reasoning_present = bool(
            isinstance(message, dict)
            and any(
                isinstance(message.get(field), str) and message[field].strip()
                for field in ("reasoning_content", "reasoning")
            )
        )
        hint = (
            " The response contains reasoning text but no final answer."
            if reasoning_present
            else ""
        )
        raise ValueError(
            "Model returned no final assistant content"
            f" (finish_reason={finish_reason!r}).{hint}"
        )


class OpenAIResponsesEvaluator(HttpModelEvaluator):
    """Native OpenAI Responses API adapter for current OpenAI reasoning and vision models."""

    def request(self, prompt: EvaluationPrompt) -> tuple[str, dict[str, str], dict[str, Any]]:
        input_content: str | list[dict[str, Any]]
        if prompt.image_urls:
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
        if prompt.image_urls:
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
    return adapter(settings)


def _endpoint(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _provider_error_details(response: httpx.Response) -> tuple[str | None, str]:
    code: str | None = None
    message = response.reason_phrase or "Request failed."
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            raw_code = error.get("code")
            raw_message = error.get("message")
            if isinstance(raw_code, str) and raw_code.strip():
                code = raw_code.strip()
            if isinstance(raw_message, str) and raw_message.strip():
                message = raw_message.strip()
    return code, message[:500]


def _message_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    if not isinstance(value, list):
        return None

    parts = [
        block["text"]
        for block in value
        if isinstance(block, dict)
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    ]
    return "".join(parts) if parts else None


def _contains_json_object(value: str) -> bool:
    return "{" in value and "}" in value


def _diagnostic_response_payload(payload: dict[str, Any] | None) -> str:
    if payload is None:
        return "No JSON response payload was available."
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)[:8000]
    except (TypeError, ValueError):
        return repr(payload)[:8000]


def _profile_log_context(settings: Settings) -> str:
    if settings.active_profile_name and settings.active_profile_id:
        return f"[{settings.active_profile_name} · {settings.active_profile_id}] "
    if settings.active_profile_name:
        return f"[{settings.active_profile_name}] "
    if settings.active_profile_id:
        return f"[{settings.active_profile_id}] "
    return ""
