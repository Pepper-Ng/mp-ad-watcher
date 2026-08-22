from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from marktplaats_ad_watcher.model_config import REASONING_EFFORTS, provider_preset
from marktplaats_ad_watcher.search_url import normalize_marktplaats_search_url

if TYPE_CHECKING:
    from marktplaats_ad_watcher.profiles import SearchProfile


@dataclass(frozen=True)
class Settings:
    marktplaats_search_url: str
    marktplaats_use_case: str
    poll_interval_seconds: int
    max_ads_per_poll: int
    bootstrap_existing_ads: bool
    exclude_admarkt_ads: bool
    notify_min_confidence: float
    review_min_confidence: float
    notify_review_actions: bool
    model_provider: str
    model_api_key: str | None
    model_base_url: str
    model_name: str
    model_temperature: float
    model_max_tokens: int
    model_reasoning_effort: str | None
    model_json_mode: bool
    send_image_content_to_model: bool
    max_images_for_model: int
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    telegram_disable_web_page_preview: bool
    state_file: Path
    results_file: Path
    status_file: Path
    request_timeout_seconds: float
    user_agent: str
    web_admin_token: str | None
    dry_run: bool = False
    persistent_data_root: Path | None = None
    active_profile_id: str | None = None
    active_profile_name: str | None = None
    notify_ai_failures: bool = True

    def __post_init__(self) -> None:
        normalized_search_url = normalize_marktplaats_search_url(self.marktplaats_search_url)
        object.__setattr__(self, "marktplaats_search_url", normalized_search_url)
        preset = provider_preset(self.model_provider)
        _validate_url("MARKTPLAATS_SEARCH_URL", normalized_search_url)
        _validate_url("MODEL_BASE_URL", self.model_base_url)
        _validate_range("POLL_INTERVAL_SECONDS", self.poll_interval_seconds, minimum=1)
        _validate_range("MAX_ADS_PER_POLL", self.max_ads_per_poll, minimum=1, maximum=100)
        _validate_range("NOTIFY_MIN_CONFIDENCE", self.notify_min_confidence, minimum=0, maximum=1)
        _validate_range("REVIEW_MIN_CONFIDENCE", self.review_min_confidence, minimum=0, maximum=1)
        _validate_range("MODEL_TEMPERATURE", self.model_temperature, minimum=0, maximum=2)
        _validate_range("MODEL_MAX_TOKENS", self.model_max_tokens, minimum=1)
        _validate_range("MAX_IMAGES_FOR_MODEL", self.max_images_for_model, minimum=1, maximum=10)
        _validate_range("REQUEST_TIMEOUT_SECONDS", self.request_timeout_seconds, minimum=0.1)

        if preset.protocol == "openai_responses" and self.model_max_tokens < 16:
            raise ValueError("MODEL_MAX_TOKENS must be at least 16 for the OpenAI Responses API.")
        if not self.model_name.strip():
            raise ValueError("MODEL_NAME must not be empty.")
        if (
            self.model_reasoning_effort is not None
            and self.model_reasoning_effort not in REASONING_EFFORTS
        ):
            supported = ", ".join(sorted(REASONING_EFFORTS))
            raise ValueError(f"MODEL_REASONING_EFFORT must be one of: {supported}.")
        if not self.user_agent.strip():
            raise ValueError("USER_AGENT must not be empty.")

    @property
    def data_root(self) -> Path:
        """Persistent root shared by profiles and the global model quota."""

        return self.persistent_data_root or self.results_file.parent

    @property
    def global_model_usage_file(self) -> Path:
        return self.data_root / "model_usage.json"

    @property
    def pipeline_progress_file(self) -> Path:
        """Persistent pipeline progress owned by this settings instance's search scope."""

        return self.results_file.parent / "pipeline_progress.json"

    def legacy_search_file_paths(self) -> dict[str, Path]:
        """Return legacy single-search persistence paths without including global usage."""

        return {
            "seen_ads.json": self.state_file,
            "evaluations.jsonl": self.results_file,
            "runtime_status.json": self.status_file,
            "pipeline_progress.json": self.results_file.parent / "pipeline_progress.json",
        }

    def for_profile(self, profile: SearchProfile) -> Settings:
        """Resolve search-specific values and storage while retaining root-global settings."""

        from marktplaats_ad_watcher.profiles import profile_storage_paths

        paths = profile_storage_paths(self.data_root, profile.id)
        return replace(
            self,
            marktplaats_search_url=profile.search_url,
            marktplaats_use_case=profile.use_case,
            poll_interval_seconds=(
                profile.poll_interval_seconds
                if profile.poll_interval_seconds is not None
                else self.poll_interval_seconds
            ),
            bootstrap_existing_ads=profile.bootstrap_existing_ads,
            state_file=paths.state_file,
            results_file=paths.results_file,
            status_file=paths.status_file,
            persistent_data_root=self.data_root,
            active_profile_id=profile.id,
            active_profile_name=profile.name,
        )

    @staticmethod
    def from_environment(
        env: Mapping[str, str] | None = None,
        *,
        dry_run: bool = False,
    ) -> Settings:
        values = env if env is not None else os.environ

        search_url = _required(values, "MARKTPLAATS_SEARCH_URL")
        use_case = _required(values, "MARKTPLAATS_USE_CASE")
        model_provider = values.get("MODEL_PROVIDER", "deepseek").strip().lower() or "deepseek"
        preset = provider_preset(model_provider)
        use_legacy_deepseek = model_provider == "deepseek"

        model_api_key = _optional(values, "MODEL_API_KEY")
        model_base_url = _optional(values, "MODEL_BASE_URL")
        model_name = _optional(values, "MODEL_NAME")
        model_temperature = _optional(values, "MODEL_TEMPERATURE")
        model_max_tokens = _optional(values, "MODEL_MAX_TOKENS")
        if use_legacy_deepseek:
            if "MODEL_API_KEY" not in values:
                model_api_key = _optional(values, "DEEPSEEK_API_KEY")
            if "MODEL_BASE_URL" not in values:
                model_base_url = _optional(values, "DEEPSEEK_BASE_URL")
            if "MODEL_NAME" not in values:
                model_name = _optional(values, "DEEPSEEK_MODEL")
            if "MODEL_TEMPERATURE" not in values:
                model_temperature = _optional(values, "DEEPSEEK_TEMPERATURE")
            if "MODEL_MAX_TOKENS" not in values:
                model_max_tokens = _optional(values, "DEEPSEEK_MAX_TOKENS")

        reasoning_value = values.get("MODEL_REASONING_EFFORT", "").strip().lower()
        model_reasoning_effort = reasoning_value or preset.reasoning_effort

        return Settings(
            marktplaats_search_url=search_url,
            marktplaats_use_case=use_case,
            poll_interval_seconds=_int(values, "POLL_INTERVAL_SECONDS", 600),
            max_ads_per_poll=_int(values, "MAX_ADS_PER_POLL", 30),
            bootstrap_existing_ads=_bool(values, "BOOTSTRAP_EXISTING_ADS", True),
            exclude_admarkt_ads=_bool(values, "EXCLUDE_ADMARKT_ADS", True),
            notify_min_confidence=_float(values, "NOTIFY_MIN_CONFIDENCE", 0.65),
            review_min_confidence=_float(values, "REVIEW_MIN_CONFIDENCE", 0.0),
            notify_review_actions=_bool(values, "NOTIFY_REVIEW_ACTIONS", True),
            model_provider=model_provider,
            model_api_key=model_api_key,
            model_base_url=(model_base_url or preset.base_url).rstrip("/"),
            model_name=model_name or preset.model,
            model_temperature=float(model_temperature) if model_temperature is not None else 0.0,
            model_max_tokens=int(model_max_tokens) if model_max_tokens is not None else 700,
            model_reasoning_effort=model_reasoning_effort,
            model_json_mode=_bool(values, "MODEL_JSON_MODE", preset.json_mode),
            send_image_content_to_model=_bool(values, "SEND_IMAGE_CONTENT_TO_MODEL", False),
            max_images_for_model=_int(values, "MAX_IMAGES_FOR_MODEL", 3),
            telegram_bot_token=_optional(values, "TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_optional(values, "TELEGRAM_CHAT_ID"),
            telegram_disable_web_page_preview=_bool(
                values, "TELEGRAM_DISABLE_WEB_PAGE_PREVIEW", False
            ),
            state_file=Path(_optional(values, "STATE_FILE") or "data/seen_ads.json"),
            results_file=Path(_optional(values, "RESULTS_FILE") or "data/evaluations.jsonl"),
            status_file=Path(_optional(values, "STATUS_FILE") or "data/runtime_status.json"),
            request_timeout_seconds=_float(values, "REQUEST_TIMEOUT_SECONDS", 20.0),
            user_agent=(
                _optional(values, "USER_AGENT")
                or "marktplaats-ad-watcher/0.1 (+local personal watcher)"
            ),
            web_admin_token=_optional(values, "WEB_ADMIN_TOKEN"),
            dry_run=dry_run,
            notify_ai_failures=_bool(values, "NOTIFY_AI_FAILURES", True),
        )


def load_dotenv(path: Path, *, override: bool = False) -> None:
    if not path.exists():
        return

    for key, value in parse_dotenv(path).items():
        if key and (override or key not in os.environ):
            os.environ[key] = value


def parse_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = _decode_env_value(value.strip())

    return values


def write_dotenv(path: Path, values: Mapping[str, str]) -> None:
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    lines = [f"{key}={_encode_env_value(value)}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _decode_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return str(json.loads(value))
        except json.JSONDecodeError:
            pass

    if len(value) >= 2 and value[0] == value[-1] == "'":
        value = value[1:-1]

    return value.replace("\\n", "\n")


def _encode_env_value(value: str) -> str:
    return json.dumps(value)


def _required(values: Mapping[str, str], name: str) -> str:
    value = _optional(values, name)
    if value is None:
        raise ValueError(f"Missing required environment variable {name}.")
    return value


def _optional(values: Mapping[str, str], name: str) -> str | None:
    value = values.get(name)
    if value is None:
        return None

    stripped = value.strip()
    if not stripped or stripped.lower() in {"replace-me", "changeme", "none", "null"}:
        return None

    return stripped


def _bool(values: Mapping[str, str], name: str, default: bool) -> bool:
    value = values.get(name)
    if value is None or not value.strip():
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False

    raise ValueError(f"Environment variable {name} must be a boolean value.")


def _int(values: Mapping[str, str], name: str, default: int) -> int:
    value = values.get(name)
    if value is None or not value.strip():
        return default
    return int(value)


def _float(values: Mapping[str, str], name: str, default: float) -> float:
    value = values.get(name)
    if value is None or not value.strip():
        return default
    return float(value)


def _validate_url(name: str, value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Environment variable {name} must be an HTTP(S) URL.")


def _validate_range(
    name: str,
    value: int | float,
    *,
    minimum: int | float,
    maximum: int | float | None = None,
) -> None:
    if value < minimum or (maximum is not None and value > maximum):
        expected = f"at least {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise ValueError(f"Environment variable {name} must be {expected}.")
