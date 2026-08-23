# ruff: noqa: E501

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import deque
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from marktplaats_ad_watcher.config import Settings, parse_dotenv, write_dotenv
from marktplaats_ad_watcher.factory import build_profile_orchestrator
from marktplaats_ad_watcher.marktplaats import MarktplaatsClient
from marktplaats_ad_watcher.model_config import (
    PROVIDER_PRESETS,
    REASONING_EFFORTS,
    provider_preset,
    resolved_model_environment,
)
from marktplaats_ad_watcher.model_providers import ModelProviderError, build_model_evaluator
from marktplaats_ad_watcher.models import Ad, EvaluatedAd, WatcherRunSummary
from marktplaats_ad_watcher.pipeline_progress import (
    PipelineProgressRecord,
    PipelineProgressStore,
)
from marktplaats_ad_watcher.profiles import (
    DEFAULT_PROFILE_ID,
    ProfileConfigurationError,
    ProfileMigrationError,
    ProfileRegistry,
    ProfileRegistryStore,
    SearchProfile,
    ensure_profile_registry,
    verify_profile_registry,
)
from marktplaats_ad_watcher.runner import ProfileExecutionSummary
from marktplaats_ad_watcher.state import SeenStore
from marktplaats_ad_watcher.status import RuntimeStatus, RuntimeStatusStore
from marktplaats_ad_watcher.telegram import TelegramNotifier
from marktplaats_ad_watcher.usage import (
    ModelDailyLimitExceeded,
    ModelUsageSnapshot,
    ModelUsageStore,
)

EDITABLE_KEYS = [
    "MARKTPLAATS_SEARCH_URL",
    "MARKTPLAATS_USE_CASE",
    "BOOTSTRAP_EXISTING_ADS",
    "EXCLUDE_ADMARKT_ADS",
    "POLL_INTERVAL_SECONDS",
    "MAX_ADS_PER_POLL",
    "NOTIFY_MIN_CONFIDENCE",
    "REVIEW_MIN_CONFIDENCE",
    "NOTIFY_REVIEW_ACTIONS",
    "NOTIFY_AI_FAILURES",
    "MODEL_PROVIDER",
    "MODEL_API_KEY",
    "MODEL_BASE_URL",
    "MODEL_NAME",
    "MODEL_TEMPERATURE",
    "MODEL_MAX_TOKENS",
    "MODEL_REASONING_EFFORT",
    "MODEL_JSON_MODE",
    "FALLBACK_MODEL_ENABLED",
    "FALLBACK_MODEL_PROVIDER",
    "FALLBACK_MODEL_API_KEY",
    "FALLBACK_MODEL_BASE_URL",
    "FALLBACK_MODEL_NAME",
    "FALLBACK_MODEL_TEMPERATURE",
    "FALLBACK_MODEL_MAX_TOKENS",
    "FALLBACK_MODEL_REASONING_EFFORT",
    "FALLBACK_MODEL_JSON_MODE",
    "SEND_IMAGE_CONTENT_TO_MODEL",
    "MAX_IMAGES_FOR_MODEL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_DISABLE_WEB_PAGE_PREVIEW",
    "STATE_FILE",
    "RESULTS_FILE",
    "STATUS_FILE",
    "REQUEST_TIMEOUT_SECONDS",
    "USER_AGENT",
    "WEB_ADMIN_TOKEN",
]

BOOLEAN_KEYS = {
    "BOOTSTRAP_EXISTING_ADS",
    "EXCLUDE_ADMARKT_ADS",
    "NOTIFY_REVIEW_ACTIONS",
    "NOTIFY_AI_FAILURES",
    "MODEL_JSON_MODE",
    "FALLBACK_MODEL_ENABLED",
    "FALLBACK_MODEL_JSON_MODE",
    "SEND_IMAGE_CONTENT_TO_MODEL",
    "TELEGRAM_DISABLE_WEB_PAGE_PREVIEW",
}

SECRET_KEYS = {
    "MODEL_API_KEY",
    "FALLBACK_MODEL_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "WEB_ADMIN_TOKEN",
}
LEGACY_MODEL_KEYS = {
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_TEMPERATURE",
    "DEEPSEEK_MAX_TOKENS",
}
CONFIG_DEFAULTS = {
    "BOOTSTRAP_EXISTING_ADS": "true",
    "EXCLUDE_ADMARKT_ADS": "true",
    "POLL_INTERVAL_SECONDS": "600",
    "MAX_ADS_PER_POLL": "30",
    "NOTIFY_MIN_CONFIDENCE": "0.65",
    "REVIEW_MIN_CONFIDENCE": "0",
    "NOTIFY_REVIEW_ACTIONS": "true",
    "NOTIFY_AI_FAILURES": "true",
    "FALLBACK_MODEL_ENABLED": "false",
    "FALLBACK_MODEL_TEMPERATURE": "0",
    "FALLBACK_MODEL_MAX_TOKENS": "700",
    "FALLBACK_MODEL_JSON_MODE": "false",
    "SEND_IMAGE_CONTENT_TO_MODEL": "false",
    "MAX_IMAGES_FOR_MODEL": "3",
    "TELEGRAM_DISABLE_WEB_PAGE_PREVIEW": "false",
    "STATE_FILE": "data/seen_ads.json",
    "RESULTS_FILE": "data/evaluations.jsonl",
    "STATUS_FILE": "data/runtime_status.json",
    "REQUEST_TIMEOUT_SECONDS": "20",
    "USER_AGENT": "marktplaats-ad-watcher/0.1 (+local personal watcher)",
}
LOGGER = logging.getLogger(__name__)
ERROR_RETRY_SECONDS = 60
DEPLOYMENT_ENV_KEYS = {"WEB_ADMIN_TOKEN"}


class RecentLogBuffer(logging.Handler):
    def __init__(self, *, maximum: int = 200) -> None:
        super().__init__(level=logging.INFO)
        self.entries: deque[dict[str, str]] = deque(maxlen=maximum)

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if record.exc_info and record.exc_info[1]:
            error = record.exc_info[1]
            message += f" — {type(error).__name__}: {error}"
        message = _redact_diagnostic_text(message)
        detail = _redact_diagnostic_text(str(getattr(record, "diagnostic_detail", "")))
        self.entries.append(
            {
                "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": message[:1200],
                "detail": detail[:8000],
            }
        )


def _redact_diagnostic_text(value: str) -> str:
    value = re.sub(r"(?i)([?&]token=)[^&\s\"']+", r"\1[REDACTED]", value)
    return re.sub(
        r"(?i)((?:authorization|password|token|api[_-]?key)\s*[:=]\s*)\S+",
        r"\1[REDACTED]",
        value,
    )


@dataclass(frozen=True)
class ProfileSelection:
    """The requested web scope, with ``all`` reserved for read-only aggregation."""

    registry: ProfileRegistry | None
    profile: SearchProfile | None
    is_all: bool = False

    @property
    def label(self) -> str:
        if self.is_all:
            return "All searches"
        if self.profile is not None:
            return self.profile.name
        return "Freezers"


class WatcherService:
    def __init__(self, *, env_file: Path, dry_run: bool) -> None:
        self._env_file = env_file
        self._dry_run = dry_run
        self._run_lock = asyncio.Lock()
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._manual_tasks: set[asyncio.Task[None]] = set()
        self._stopping = False
        self._preview_ads: dict[str, Ad] = {}
        self._preview_fetched_at: datetime | None = None
        self._preview_counts = (0, 0, 0)
        self._profile_preview_ads: dict[str, dict[str, Ad]] = {}
        self._profile_preview_fetched_at: dict[str, datetime] = {}
        self._profile_preview_counts: dict[str, tuple[int, int, int]] = {}

    @property
    def env_file(self) -> Path:
        return self._env_file

    def read_config(self) -> dict[str, str]:
        values = parse_dotenv(self._env_file)
        for key in DEPLOYMENT_ENV_KEYS:
            if key not in values and key in os.environ:
                values[key] = os.environ[key]
        for key, default in CONFIG_DEFAULTS.items():
            if not values.get(key, "").strip():
                values[key] = default
        return resolved_model_environment(values)

    def admin_token(self) -> str | None:
        value = self.read_config().get("WEB_ADMIN_TOKEN", "").strip()
        return value if _secret_is_configured(value) else None

    def status_store(self) -> RuntimeStatusStore:
        values = self.read_config()
        status_file = Path(values.get("STATUS_FILE", "data/runtime_status.json"))
        return RuntimeStatusStore(status_file)

    def activate_profile_registry(self) -> ProfileRegistry:
        """Safely migrate once, then verify profile copies before profile-aware work starts."""

        settings = self._load_settings()
        result = ensure_profile_registry(settings)
        verification = verify_profile_registry(settings)
        if result.registry != verification.registry:
            raise ProfileConfigurationError("Profile registry changed during verification.")
        return verification.registry

    def profile_selection(self, requested_profile_id: str | None) -> ProfileSelection:
        """Resolve a safe profile scope while retaining an unconfigured legacy UI fallback."""

        try:
            registry = self.activate_profile_registry()
        except ValueError as error:
            if not _is_missing_search_settings_error(error):
                raise
            if requested_profile_id and requested_profile_id not in {DEFAULT_PROFILE_ID, "freezers"}:
                raise ValueError("Profiles are unavailable until search settings are configured.") from error
            return ProfileSelection(registry=None, profile=None)

        requested = (requested_profile_id or registry.default_profile_id).strip().lower()
        if requested == "all":
            return ProfileSelection(registry=registry, profile=None, is_all=True)
        try:
            profile = registry.profile(requested)
        except ProfileConfigurationError as error:
            raise ValueError(str(error)) from error
        return ProfileSelection(registry=registry, profile=profile)

    def profiles(self) -> tuple[SearchProfile, ...]:
        """Return ordered active profiles, or no profiles for a not-yet-configured legacy UI."""

        try:
            return self.activate_profile_registry().active_profiles
        except ValueError as error:
            if _is_missing_search_settings_error(error):
                return ()
            raise

    def settings_for_profile(self, profile: SearchProfile | None) -> Settings:
        try:
            settings = self._load_settings()
        except ValueError as error:
            if profile is not None or not _is_missing_search_settings_error(error):
                raise
            values = self.read_config()
            values.setdefault(
                "MARKTPLAATS_SEARCH_URL",
                "https://www.marktplaats.nl/lrp/api/search?query=legacy",
            )
            values.setdefault("MARKTPLAATS_USE_CASE", "Legacy single-search settings.")
            settings = Settings.from_environment(values, dry_run=self._dry_run)
        return settings.for_profile(profile) if profile is not None else settings

    def status_store_for(self, profile: SearchProfile | None) -> RuntimeStatusStore:
        return RuntimeStatusStore(self.settings_for_profile(profile).status_file)

    def model_usage_store(self) -> ModelUsageStore:
        values = self.read_config()
        results_file = Path(values["RESULTS_FILE"])
        return ModelUsageStore(results_file.parent / "model_usage.json")

    def model_usage(self) -> ModelUsageSnapshot:
        return self.model_usage_store().snapshot()

    def set_model_daily_limit(self, limit: int) -> ModelUsageSnapshot:
        return self.model_usage_store().set_limit(limit)

    def reset_model_usage_today(self) -> ModelUsageSnapshot:
        return self.model_usage_store().reset_today()

    def pipeline_progress_store(self) -> PipelineProgressStore:
        values = self.read_config()
        results_file = Path(values["RESULTS_FILE"])
        return PipelineProgressStore(results_file.parent / "pipeline_progress.json")

    def pipeline_progress_store_for(self, profile: SearchProfile | None) -> PipelineProgressStore:
        return PipelineProgressStore(self.settings_for_profile(profile).pipeline_progress_file)

    def pipeline_progress(self) -> list[PipelineProgressRecord]:
        values = self.read_config()
        return self.pipeline_progress_store().sync_evaluations(Path(values["RESULTS_FILE"]))

    def pipeline_progress_for(self, profile: SearchProfile | None) -> list[PipelineProgressRecord]:
        if profile is None:
            return self.pipeline_progress()
        settings = self.settings_for_profile(profile)
        return _profile_progress_records(
            self.pipeline_progress_store_for(profile).sync_evaluations(settings.results_file),
            profile,
        )

    @property
    def preview_ads(self) -> list[Ad]:
        if self._preview_fetched_at is None:
            return []
        if datetime.now(UTC) - self._preview_fetched_at > timedelta(minutes=30):
            self._preview_ads.clear()
            self._preview_fetched_at = None
            self._preview_counts = (0, 0, 0)
            return []
        return list(self._preview_ads.values())

    @property
    def preview_fetched_at(self) -> datetime | None:
        return self._preview_fetched_at

    @property
    def preview_counts(self) -> tuple[int, int, int]:
        return self._preview_counts

    def preview_ads_for(self, profile: SearchProfile | None) -> list[Ad]:
        profile_id = _preview_profile_id(profile)
        if profile is None:
            return self.preview_ads
        fetched_at = self._profile_preview_fetched_at.get(profile_id)
        if fetched_at is None and profile.id == DEFAULT_PROFILE_ID:
            return self.preview_ads
        if fetched_at is None or datetime.now(UTC) - fetched_at > timedelta(minutes=30):
            self._profile_preview_ads.pop(profile_id, None)
            self._profile_preview_fetched_at.pop(profile_id, None)
            self._profile_preview_counts.pop(profile_id, None)
            return []
        return list(self._profile_preview_ads.get(profile_id, {}).values())

    def preview_fetched_at_for(self, profile: SearchProfile | None) -> datetime | None:
        if profile is None:
            return self.preview_fetched_at
        value = self._profile_preview_fetched_at.get(_preview_profile_id(profile))
        return value if value is not None else self.preview_fetched_at

    def preview_counts_for(self, profile: SearchProfile | None) -> tuple[int, int, int]:
        if profile is None:
            return self.preview_counts
        return self._profile_preview_counts.get(_preview_profile_id(profile), self.preview_counts)

    async def fetch_preview(self, profile: SearchProfile | None = None) -> list[Ad]:
        settings = self.settings_for_profile(profile)
        client = MarktplaatsClient(
            timeout_seconds=settings.request_timeout_seconds,
            user_agent=settings.user_agent,
        )
        fetched = await client.fetch_ads(
            settings.marktplaats_search_url,
            limit=settings.max_ads_per_poll,
        )
        eligible = [
            ad
            for ad in fetched
            if not settings.exclude_admarkt_ads or not ad.id.lower().startswith("a")
        ]
        profile_id = _preview_profile_id(profile)
        fetched_at = datetime.now(UTC)
        counts = (len(fetched), len(eligible), len(fetched) - len(eligible))
        self._profile_preview_ads[profile_id] = {ad.id: ad for ad in eligible}
        self._profile_preview_fetched_at[profile_id] = fetched_at
        self._profile_preview_counts[profile_id] = counts
        if profile is None or profile.id == DEFAULT_PROFILE_ID:
            self._preview_ads = self._profile_preview_ads[profile_id]
            self._preview_fetched_at = fetched_at
            self._preview_counts = counts
        return eligible

    async def test_preview_ad(
        self,
        ad_id: str,
        profile: SearchProfile | None = None,
    ) -> EvaluatedAd:
        ads = {ad.id: ad for ad in self.preview_ads_for(profile)}
        ad = ads.get(ad_id)
        if ad is None:
            raise ValueError("The preview expired or the selected ad is unavailable. Fetch again.")
        settings = self.settings_for_profile(profile)
        client = MarktplaatsClient(
            timeout_seconds=settings.request_timeout_seconds,
            user_agent=settings.user_agent,
        )
        enriched_ad = await client.enrich_ad(ad)
        profile_id = _preview_profile_id(profile)
        self._profile_preview_ads.setdefault(profile_id, {})[enriched_ad.id] = enriched_ad
        if profile is None or profile.id == DEFAULT_PROFILE_ID:
            self._preview_ads[enriched_ad.id] = enriched_ad
        result = await build_model_evaluator(settings).evaluate(enriched_ad)
        evaluated_ad = EvaluatedAd(
            ad=enriched_ad,
            result=result,
            profile_id=settings.active_profile_id,
            profile_name=settings.active_profile_name,
        )
        seen_store = SeenStore(settings.state_file)
        seen_store.append_result(settings.results_file, evaluated_ad)
        seen_store.mark_seen(ad, result)
        self.pipeline_progress_store_for(profile).save_ai_result(evaluated_ad)
        RuntimeStatusStore(settings.status_file).resolve_evaluation_failure(ad.id)
        return evaluated_ad

    async def send_pipeline_result_to_telegram(
        self,
        ad_id: str,
        profile: SearchProfile | None = None,
    ) -> PipelineProgressRecord:
        settings = self.settings_for_profile(profile)
        progress_store = self.pipeline_progress_store_for(profile)
        progress_store.sync_evaluations(settings.results_file)
        record = progress_store.get(ad_id)
        if record is None:
            raise ValueError("No saved AI result exists for this ad.")
        evaluated_ad = _evaluation_with_profile_defaults(
            record.evaluated_ad,
            profile_id=settings.active_profile_id,
            profile_name=settings.active_profile_name,
        )
        send_result = await TelegramNotifier(settings).send(evaluated_ad)
        if not send_result.sent:
            raise ValueError(send_result.reason or "Telegram did not send the result.")
        return progress_store.mark_telegram_sent(
            ad_id,
            message_id=send_result.message_id,
            profile_id=evaluated_ad.profile_id,
            profile_name=evaluated_ad.profile_name,
        )

    async def send_standalone_telegram_test(self, profile: SearchProfile | None = None) -> None:
        send_result = await TelegramNotifier(self.settings_for_profile(profile)).send_test_message()
        if not send_result.sent:
            raise ValueError(send_result.reason or "Telegram connectivity test failed.")

    async def start(self) -> None:
        self._stopping = False
        try:
            self.activate_profile_registry()
        except ValueError as error:
            if _is_missing_search_settings_error(error):
                LOGGER.info(
                    "Watcher startup is paused until Marktplaats search settings are configured."
                )
            else:
                LOGGER.info("Watcher startup is paused: %s", error)
        except (ProfileConfigurationError, ProfileMigrationError):
            LOGGER.exception("Watcher startup detected an inconsistent profile migration state.")
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._stopping = True
        if self._stop_event is not None:
            self._stop_event.set()
        if self._loop_task is not None:
            await self._loop_task
        if self._manual_tasks:
            await asyncio.gather(*tuple(self._manual_tasks), return_exceptions=True)

    async def run_once(self) -> ProfileExecutionSummary:
        async with self._run_lock:
            try:
                settings = self._load_settings()
                execution = await build_profile_orchestrator(settings).run_all_enabled()
            except Exception as error:
                self.status_store().mark_failed(error)
                raise

            LOGGER.info("Scheduled profile execution: %s", execution.model_dump())
            return execution

    async def run_profile_once(self, profile: SearchProfile) -> None:
        async with self._run_lock:
            settings = self._load_settings()
            execution = await build_profile_orchestrator(settings).run_profile(profile.id)
            LOGGER.info("Manual profile execution: %s", execution.model_dump())

    def queue_run_once(self) -> bool:
        if self._stopping or self._run_lock.locked() or self._manual_tasks:
            return False

        task = asyncio.create_task(self._run_once_safely())
        self._manual_tasks.add(task)
        task.add_done_callback(self._manual_tasks.discard)
        return True

    def queue_run_profile(self, profile: SearchProfile) -> bool:
        if self._stopping or self._run_lock.locked() or self._manual_tasks:
            return False

        task = asyncio.create_task(self._run_profile_safely(profile))
        self._manual_tasks.add(task)
        task.add_done_callback(self._manual_tasks.discard)
        return True

    async def _run_once_safely(self) -> None:
        with suppress(Exception):
            await self.run_once()

    async def _run_profile_safely(self, profile: SearchProfile) -> None:
        with suppress(Exception):
            await self.run_profile_once(profile)

    async def _run_forever(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            delay_seconds: float = float(ERROR_RETRY_SECONDS)
            try:
                execution = await self.run_once()
                delay_seconds = (
                    self._next_profile_due_delay()
                    if execution is not None
                    else float(self._load_settings().poll_interval_seconds)
                )
            except Exception:
                LOGGER.exception("Watcher run failed; retrying in %s seconds.", delay_seconds)

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=delay_seconds,
                )
            except TimeoutError:
                continue

    def _load_settings(self) -> Settings:
        return Settings.from_environment(self.read_config(), dry_run=self._dry_run)

    def _next_profile_due_delay(self) -> float:
        registry = self.activate_profile_registry()
        scheduled = [
            self.status_store_for(profile).read().next_run_at
            for profile in registry.active_profiles
            if profile.enabled
        ]
        due_times = [value for value in scheduled if value is not None]
        if not due_times:
            return float(self._load_settings().poll_interval_seconds)
        return max(0.0, (min(due_times) - datetime.now(UTC)).total_seconds())


def _preview_profile_id(profile: SearchProfile | None) -> str:
    return profile.id if profile is not None else "__legacy__"


def _select_profile(
    request: Request,
    service: WatcherService,
    *,
    require_concrete: bool = False,
) -> ProfileSelection | HTMLResponse:
    try:
        selection = service.profile_selection(request.query_params.get("profile"))
    except Exception as error:
        return HTMLResponse(
            _page("Invalid profile", _notice(_safe_error("Profile selection failed", error), error="")),
            status_code=400,
        )
    if require_concrete and (
        selection.is_all or (selection.profile is not None and selection.profile.archived)
    ):
        return HTMLResponse(
            _page(
                "Read-only aggregate view",
                "<p class='alert'>This profile scope is read-only. Select one active profile before "
                "running an action that changes state.</p>",
            ),
            status_code=400,
        )
    return selection


def _status_for_selection(service: WatcherService, selection: ProfileSelection) -> RuntimeStatus:
    if not selection.is_all:
        return service.status_store_for(selection.profile).read()
    registry = selection.registry
    if registry is None:
        return RuntimeStatus()

    statuses = [
        (profile, service.status_store_for(profile).read())
        for profile in registry.active_profiles
    ]
    if not statuses:
        return RuntimeStatus()

    latest = max(
        (status for _, status in statuses),
        key=lambda status: status.last_finished_at or datetime.min.replace(tzinfo=UTC),
    )
    totals = {
        field: sum(getattr(status, field) for _, status in statuses)
        for field in (
            "total_runs",
            "total_errors",
            "total_fetched",
            "total_kept",
            "total_filtered",
            "total_new",
            "total_evaluated",
            "total_notified",
            "total_ignored",
            "total_reviewed",
            "total_notify_actions",
            "total_evaluation_failed",
        )
    }
    errors = [
        f"{profile.name}: {status.last_error}"
        for profile, status in statuses
        if status.last_error
    ]
    return RuntimeStatus(
        is_running=any(status.is_running for _, status in statuses),
        last_started_at=latest.last_started_at,
        last_finished_at=latest.last_finished_at,
        next_run_at=min(
            (status.next_run_at for _, status in statuses if status.next_run_at is not None),
            default=None,
        ),
        last_error=" · ".join(errors) if errors else None,
        last_summary=latest.last_summary,
        total_runs=totals["total_runs"],
        total_errors=totals["total_errors"],
        total_fetched=totals["total_fetched"],
        total_kept=totals["total_kept"],
        total_filtered=totals["total_filtered"],
        total_new=totals["total_new"],
        total_evaluated=totals["total_evaluated"],
        total_notified=totals["total_notified"],
        total_ignored=totals["total_ignored"],
        total_reviewed=totals["total_reviewed"],
        total_notify_actions=totals["total_notify_actions"],
        total_evaluation_failed=totals["total_evaluation_failed"],
    )


def _profiles_for_selection(selection: ProfileSelection) -> tuple[SearchProfile | None, ...]:
    if selection.is_all:
        return selection.registry.active_profiles if selection.registry is not None else ()
    return (selection.profile,)


def _diagnostic_entries(
    entries: deque[dict[str, str]],
    selection: ProfileSelection,
) -> list[dict[str, str]]:
    if selection.is_all or selection.profile is None:
        return list(entries)
    identifiers = (selection.profile.id.lower(), selection.profile.name.lower())
    return [
        entry
        for entry in entries
        if any(
            identifier in f"{entry['message']} {entry['detail']}".lower()
            for identifier in identifiers
        )
    ]


def _read_scoped_evaluations(
    service: WatcherService,
    selection: ProfileSelection,
    *,
    action: str,
) -> list[EvaluatedAd]:
    evaluations: list[EvaluatedAd] = []
    for profile in _profiles_for_selection(selection):
        settings = service.settings_for_profile(profile)
        for evaluation in _read_evaluations(settings.results_file, action=action):
            if profile is not None and (
                evaluation.profile_id != profile.id or evaluation.profile_name != profile.name
            ):
                evaluation = evaluation.model_copy(
                    update={"profile_id": profile.id, "profile_name": profile.name}
                )
            evaluations.append(evaluation)
    return sorted(evaluations, key=lambda evaluation: evaluation.evaluated_at, reverse=True)


def _read_scoped_seen_ads(
    service: WatcherService,
    selection: ProfileSelection,
    *,
    kind: str,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for profile in _profiles_for_selection(selection):
        settings = service.settings_for_profile(profile)
        for entry in _read_seen_ads(settings.state_file, kind=kind):
            if profile is not None:
                entry = {**entry, "profile_id": profile.id, "profile_name": profile.name}
            entries.append(entry)
    return sorted(entries, key=lambda entry: str(entry.get("first_seen_at", "")), reverse=True)


def _profile_progress_records(
    records: list[PipelineProgressRecord],
    profile: SearchProfile | None,
) -> list[PipelineProgressRecord]:
    if profile is None:
        return records
    return [
        record.model_copy(
            update={
                "evaluated_ad": record.evaluated_ad.model_copy(
                    update={"profile_id": profile.id, "profile_name": profile.name}
                )
            }
        )
        for record in records
    ]


def _evaluation_with_profile_defaults(
    evaluated_ad: EvaluatedAd,
    *,
    profile_id: str | None,
    profile_name: str | None,
) -> EvaluatedAd:
    updates: dict[str, str] = {}
    if evaluated_ad.profile_id is None and profile_id is not None:
        updates["profile_id"] = profile_id
    if evaluated_ad.profile_name is None and profile_name is not None:
        updates["profile_name"] = profile_name
    return evaluated_ad.model_copy(update=updates) if updates else evaluated_ad


def _is_missing_search_settings_error(error: Exception) -> bool:
    return str(error).startswith("Missing required environment variable MARKTPLAATS_")


def _profile_log_context(profile: SearchProfile | None) -> str:
    if profile is None:
        return "[legacy]"
    return f"[{profile.name} · {profile.id}]"


def create_web_app(*, env_file: Path, dry_run: bool = False) -> Starlette:
    service = WatcherService(env_file=env_file, dry_run=dry_run)
    recent_logs = RecentLogBuffer()

    @asynccontextmanager
    async def lifespan(_: Starlette):
        root_logger = logging.getLogger()
        root_logger.addHandler(recent_logs)
        await service.start()
        try:
            yield
        finally:
            await service.stop()
            root_logger.removeHandler(recent_logs)

    async def index(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied

        selection = _select_profile(request, service)
        if isinstance(selection, HTMLResponse):
            return selection
        status = _status_for_selection(service, selection)
        body = f"""
                {_navigation(request, current="dashboard", selection=selection)}
        {_warning_for_missing_token(service)}
                {_profile_scope_heading(selection)}
                {_status_panel(status)}
                {_last_run_panel(status)}
        {_model_usage_panel(service.model_usage(), request)}
                <section class="panel">
                    <h2>Activity since reset</h2>
                    <div class="metric-grid">
                        {_metric("Runs", status.total_runs)}
                        {_metric("Errors", status.total_errors)}
                        {_metric("Evaluated", status.total_evaluated)}
                        {_metric("AI failed", status.total_evaluation_failed)}
                        {_metric("Telegram sent", status.total_notified)}
                    </div>
                    <details>
                        <summary>All counters</summary>
                        <table>
                            {_row("Fetched results", status.total_fetched)}
                            {_row("Kept after filters", status.total_kept)}
                            {_row("Filtered locally", status.total_filtered)}
                              {_row("New-ad attempts", status.total_new)}
                              {_row("AI evaluation failures", status.total_evaluation_failed)}
                            {_row("Model ignored", status.total_ignored)}
                            {_row("Model review", status.total_reviewed)}
                            {_row("Model notify", status.total_notify_actions)}
                        </table>
                    </details>
        </section>
                <p class="action-row">
                    <a class="button-link" href="/tools{_token_query(request)}">
                        Open pipeline tools
                    </a>
                    <a href="/api/status{_token_query(request)}">Status JSON</a>
                </p>
        """
        profile_statuses = _profile_statuses_panel(service, selection)
        return HTMLResponse(_page("Marktplaats watcher", body + profile_statuses))

    async def status_json(request: Request) -> JSONResponse | PlainTextResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return PlainTextResponse("Unauthorized", status_code=401)

        selection = _select_profile(request, service)
        if isinstance(selection, HTMLResponse):
            return PlainTextResponse("Invalid profile", status_code=400)
        if not selection.is_all:
            return JSONResponse(_status_for_selection(service, selection).model_dump(mode="json"))
        registry = selection.registry
        if registry is None:
            return PlainTextResponse("Invalid profile", status_code=400)
        active_profiles = registry.active_profiles

        return JSONResponse(
            {
                "profiles": [
                    {
                        "profile_id": profile.id,
                        "profile_name": profile.name,
                        "status": service.status_store_for(profile).read().model_dump(mode="json"),
                    }
                    for profile in active_profiles
                ],
                "aggregate": _status_for_selection(service, selection).model_dump(mode="json"),
            }
        )

    async def health(_: Request) -> PlainTextResponse:
        try:
            settings = service._load_settings()
        except ValueError as error:
            if _is_missing_search_settings_error(error):
                return PlainTextResponse(
                    "ok (paused: waiting for Marktplaats search configuration)"
                )
            return PlainTextResponse("ok (paused)")

        try:
            ensure_profile_registry(settings)
            verify_profile_registry(settings)
        except (ProfileConfigurationError, ProfileMigrationError) as error:
            LOGGER.error(
                "Health check detected inconsistent profile migration state: %s",
                error,
            )
            return PlainTextResponse("not ok: profile migration inconsistent", status_code=503)
        return PlainTextResponse("ok")

    async def evaluations(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied

        selection = _select_profile(request, service)
        if isinstance(selection, HTMLResponse):
            return selection
        action = _evaluation_action(request)
        evaluations = _read_scoped_evaluations(service, selection, action=action)
        action_query = _query_with_token(request, action=action)
        filename = _evaluations_download_name(selection)
        body = f"""
        {_navigation(request, current="evaluations", selection=selection)}
        {_profile_scope_heading(selection)}
        <section class="panel">
          <form class="filter-form" method="get" action="/evaluations">
            {_token_hidden_input(request)}
            <label>Decision
              <select name="action">
                {_evaluation_filter_options(action)}
              </select>
            </label>
            <button type="submit">Filter</button>
          </form>
          <p>{len(evaluations)} evaluation(s). Newest first.</p>
          <p><a href="/api/evaluations{action_query}" download="{escape(filename)}">Download JSON</a>
          </p>
          {_evaluation_cards(evaluations)}
        </section>
        <p><a href="/{_token_query(request)}">Back to status</a></p>
        """
        return HTMLResponse(_page("Evaluations", body))

    async def evaluations_json(request: Request) -> JSONResponse | PlainTextResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return PlainTextResponse("Unauthorized", status_code=401)

        selection = _select_profile(request, service)
        if isinstance(selection, HTMLResponse):
            return PlainTextResponse("Invalid profile", status_code=400)
        evaluations = _read_scoped_evaluations(
            service, selection, action=_evaluation_action(request)
        )
        return JSONResponse(
            [evaluation.model_dump(mode="json") for evaluation in evaluations],
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{_evaluations_download_name(selection)}"'
                )
            },
        )

    async def seen_ads(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied

        selection = _select_profile(request, service)
        if isinstance(selection, HTMLResponse):
            return selection
        selected_kind = _seen_filter(request)
        entries = _read_scoped_seen_ads(service, selection, kind=selected_kind)
        body = f"""
        {_navigation(request, current="seen", selection=selection)}
        {_profile_scope_heading(selection)}
        <section class="panel">
          <form class="filter-form" method="get" action="/seen">
            {_token_hidden_input(request)}
            <label>Seen reason
              <select name="kind">{_seen_filter_options(selected_kind)}</select>
            </label>
            <button type="submit">Filter</button>
          </form>
          <p>{len(entries)} seen ad(s). Baseline ads were present when tracking started and
          intentionally skipped AI evaluation.</p>
          <p class="hint">A currently new ad appears here only after its production AI evaluation
          succeeds. Failed ads remain pending, stay off this page, and retry on later runs.</p>
          {_seen_ads_table(entries, show_profile=selection.is_all or selection.profile is not None)}
        </section>
        """
        return HTMLResponse(_page("Seen ads", body))

    def tools_page(
        request: Request,
        selection: ProfileSelection,
        *,
        notice: str | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        if selection.is_all:
            registry = selection.registry
            if registry is None:
                return HTMLResponse(
                    _page("Profiles unavailable", "<p class='alert'>No profile registry is active.</p>"),
                    status_code=400,
                )
            records: list[PipelineProgressRecord] = []
            for profile in registry.active_profiles:
                for record in service.pipeline_progress_for(profile):
                    evaluated = record.evaluated_ad.model_copy(
                        update={"profile_id": profile.id, "profile_name": profile.name}
                    )
                    records.append(record.model_copy(update={"evaluated_ad": evaluated}))
            body = f"""
            {_navigation(request, current="tools", selection=selection)}
            {_profile_scope_heading(selection)}
            <section class="panel">
              <h2>Aggregate pipeline history</h2>
              <p>All searches is read-only. Select one profile to fetch ads, test AI, send a
              saved result, or start a production run.</p>
              {_pipeline_progress_cards(request, records, read_only=True)}
            </section>
            """
            return HTMLResponse(_page("Pipeline tools", body))

        settings = service.settings_for_profile(selection.profile)
        seen = _read_seen_ads(settings.state_file, kind="all")
        seen_by_id = {str(entry["id"]): entry for entry in seen}
        runtime_status = service.status_store_for(selection.profile).read()
        summary = runtime_status.last_summary
        attempt_time = _format_time_text(runtime_status.last_finished_at)
        failures_by_id = (
            {
                failure.ad_id: f"Latest production attempt at {attempt_time}: {failure.error}"
                for failure in summary.evaluation_failures
            }
            if summary
            else {}
        )
        progress = _profile_progress_records(
            service.pipeline_progress_for(selection.profile),
            selection.profile,
        )
        body = f"""
        {_navigation(request, current="tools", selection=selection)}
        {_profile_scope_heading(selection)}
        {_notice(notice, error=error)}
        {_model_usage_panel(service.model_usage(), request, compact=True)}
        <section class="panel">
          <h2>Phase 1 · Fetch current ads</h2>
          <p><strong>Fetch only.</strong> Contacts Marktplaats and changes no local state.</p>
          <form method="post" action="/tools/fetch{_token_query(request)}">
            <button type="submit">Fetch current ads</button>
          </form>
          {_preview_summary(service, selection.profile)}
          {_preview_ads_form(
              request,
              service.preview_ads_for(selection.profile),
              seen_by_id,
              failures_by_id,
          )}
        </section>
        <section class="panel">
          <h2>Phase 2 · AI test</h2>
                    <p>Sends one fetched ad to the configured model. A successful result is saved to
          Evaluations, marks the ad processed, and clears its pending AI failure. Telegram is
          not called automatically.</p>
                    {_pipeline_progress_cards(request, progress)}
                </section>
                <section class="panel">
                    <h2>Phase 3 · Telegram for a saved result</h2>
                    <p>Each saved AI result has its own explicit Telegram action. No result means no
                    per-ad Telegram action is available.</p>
                    {_pipeline_telegram_actions(request, progress)}
                </section>
                <section class="panel">
                    <h2>Standalone Telegram test</h2>
                    <p>Sends a neutral connectivity message without an ad or AI result.</p>
                    <form method="post" action="/tools/telegram-test{_token_query(request)}">
                        <button type="submit">Send standalone Telegram test</button>
                    </form>
        </section>
        <section class="panel full-run-panel">
          <h2>Full production run</h2>
          <p>Fetches current ads, processes only new ads, writes state and evaluations, updates
          runtime status, and may send Telegram.</p>
          <a class="button-link warning-button" href="/tools/full-run{_token_query(request)}">
            Review full run…
          </a>
        </section>
        """
        return HTMLResponse(_page("Pipeline tools", body))

    async def tools(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        selection = _select_profile(request, service)
        if isinstance(selection, HTMLResponse):
            return selection
        notice = request.query_params.get("notice")
        return tools_page(request, selection, notice=notice)

    async def tools_fetch(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        selection = _select_profile(request, service, require_concrete=True)
        if isinstance(selection, HTMLResponse):
            return selection
        try:
            if selection.profile is not None and request.query_params.get("profile"):
                await service.fetch_preview(selection.profile)
            else:
                await service.fetch_preview()
        except Exception as error:
            LOGGER.exception("%s Pipeline fetch preview failed.", _profile_log_context(selection.profile))
            return tools_page(request, selection, error=_safe_error("Fetch failed", error))
        return tools_page(
            request,
            selection,
            notice="Fetched current ads without changing watcher state.",
        )

    async def tools_test(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        selection = _select_profile(request, service, require_concrete=True)
        if isinstance(selection, HTMLResponse):
            return selection
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        ad_id = form.get("ad_id", [""])[0].strip()
        try:
            result = await service.test_preview_ad(ad_id, selection.profile)
        except Exception as error:
            LOGGER.exception("%s Pipeline AI preview failed.", _profile_log_context(selection.profile))
            return tools_page(request, selection, error=_safe_error("AI test failed", error))
        return tools_page(
            request,
            selection,
            notice=(
                f"AI phase completed for {result.ad.title}. The result was saved and the ad is "
                "now processed. Telegram was not called."
            ),
        )

    async def tools_telegram(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        selection = _select_profile(request, service, require_concrete=True)
        if isinstance(selection, HTMLResponse):
            return selection
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        ad_id = form.get("ad_id", [""])[0].strip()
        try:
            record = await service.send_pipeline_result_to_telegram(ad_id, selection.profile)
        except Exception as error:
            LOGGER.exception(
                "%s Pipeline Telegram result test failed.",
                _profile_log_context(selection.profile),
            )
            return tools_page(request, selection, error=_safe_error("Telegram test failed", error))
        return tools_page(
            request,
            selection,
            notice=f"Telegram sent for {record.evaluated_ad.ad.title} and delivery was recorded.",
        )

    async def tools_telegram_test(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        selection = _select_profile(request, service, require_concrete=True)
        if isinstance(selection, HTMLResponse):
            return selection
        try:
            await service.send_standalone_telegram_test(selection.profile)
        except Exception as error:
            LOGGER.exception(
                "%s Standalone Telegram test failed.",
                _profile_log_context(selection.profile),
            )
            return tools_page(request, selection, error=_safe_error("Telegram test failed", error))
        return tools_page(
            request,
            selection,
            notice="Standalone Telegram test message sent successfully.",
        )

    async def full_run_confirm(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        selection = _select_profile(request, service, require_concrete=True)
        if isinstance(selection, HTMLResponse):
            return selection
        body = f"""
        {_navigation(request, current="tools", selection=selection)}
        {_profile_scope_heading(selection)}
        <section class="panel full-run-panel">
          <h2>Confirm full production run</h2>
          <p>This action writes seen/evaluation state and may send Telegram for new ads.</p>
          <form method="post" action="/run-now{_token_query(request)}">
            <button class="warning-button" type="submit">Start full run</button>
            <a href="/tools{_token_query(request)}">Cancel</a>
          </form>
        </section>
        """
        return HTMLResponse(_page("Confirm full run", body))

    async def diagnostics(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        selection = _select_profile(request, service)
        if isinstance(selection, HTMLResponse):
            return selection
        body = f"""
        {_navigation(request, current="diagnostics", selection=selection)}
        {_profile_scope_heading(selection)}
        <section class="panel">
          <h2>Recent watcher logs</h2>
                    <p>Shows the latest in-process messages since this container started. A concrete profile
                    only shows entries that identify that profile; all searches shows the aggregate. For
                    complete Docker output, open Portainer → Containers → marktplaats-ad-watcher → Logs.</p>
                    {_recent_logs_table(_diagnostic_entries(recent_logs.entries, selection))}
        </section>
        """
        return HTMLResponse(_page("Diagnostics", body))

    async def model_usage(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        selection = _select_profile(request, service)
        if isinstance(selection, HTMLResponse):
            return selection
        usage = service.model_usage()
        notice = request.query_params.get("notice")
        controls = (
            "<section class='panel'><p>All searches is read-only. Select one profile to change "
            "the shared model budget.</p></section>"
            if selection.is_all
            else f"""
            <section class="panel">
              <h2>Change daily limit</h2>
              <p>All production and manual AI calls share this UTC-daily budget. Only successful
              provider responses count as used; failed HTTP/network calls release their reservation.</p>
              <form class="model-limit-form" method="post"
                  action="/model-usage/limit{_token_query(request)}">
                <label>Requests per UTC day
                  <input type="number" name="limit" min="1" max="1000" value="{usage.limit}">
                </label>
                <button type="submit">Review limit change</button>
              </form>
              <p><a href="/model-usage/reset{_token_query(request)}">Reset today's usage…</a></p>
            </section>
            """
        )
        body = f"""
        {_navigation(request, current="usage", selection=selection)}
        {_profile_scope_heading(selection)}
        {_notice(notice)}
        {_model_usage_panel(usage, request, compact=True)}
        {controls}
        """
        return HTMLResponse(_page("Model request budget", body))

    async def model_usage_limit(request: Request) -> HTMLResponse | RedirectResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        selection = _select_profile(request, service, require_concrete=True)
        if isinstance(selection, HTMLResponse):
            return selection
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        try:
            new_limit = int(form.get("limit", [""])[0])
            if new_limit < 1 or new_limit > 1000:
                raise ValueError
        except ValueError:
            return HTMLResponse(
                _page("Invalid limit", "<p class='alert'>Enter a value from 1 to 1000.</p>"),
                status_code=400,
            )
        current = service.model_usage()
        if new_limit <= current.limit:
            updated = service.set_model_daily_limit(new_limit)
            notice = f"Daily model request limit changed to {updated.limit}."
            return RedirectResponse(
                f"/model-usage{_query_with_values(request, notice=notice)}",
                status_code=303,
            )
        body = f"""
        {_navigation(request, current="usage", selection=selection)}
        <section class="panel full-run-panel">
          <h2>Confirm increased model budget</h2>
          <p>Increase the daily limit from <strong>{current.limit}</strong> to
          <strong>{new_limit}</strong>? Usage is currently {current.used}; this immediately permits
          up to {max(0, new_limit - current.used)} more outbound request(s) today.</p>
          <form method="post" action="/model-usage/limit/apply{_token_query(request)}">
            <input type="hidden" name="limit" value="{new_limit}">
            <button class="warning-button" type="submit">Confirm increased limit</button>
            <a href="/model-usage{_token_query(request)}">Cancel</a>
          </form>
        </section>
        """
        return HTMLResponse(_page("Confirm model budget", body))

    async def model_usage_limit_apply(request: Request) -> RedirectResponse | HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        selection = _select_profile(request, service, require_concrete=True)
        if isinstance(selection, HTMLResponse):
            return selection
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        try:
            new_limit = int(form.get("limit", [""])[0])
            current = service.model_usage()
            if new_limit <= current.limit or new_limit > 1000:
                raise ValueError
        except ValueError:
            return HTMLResponse(
                _page("Invalid increase", "<p class='alert'>The increase is no longer valid.</p>"),
                status_code=400,
            )
        updated = service.set_model_daily_limit(new_limit)
        notice = f"Daily model request limit increased to {updated.limit} and is active now."
        return RedirectResponse(
            f"/model-usage{_query_with_values(request, notice=notice)}",
            status_code=303,
        )

    async def model_usage_reset(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        selection = _select_profile(request, service, require_concrete=True)
        if isinstance(selection, HTMLResponse):
            return selection
        usage = service.model_usage()
        body = f"""
        {_navigation(request, current="usage", selection=selection)}
        <section class="panel full-run-panel">
          <h2>Reset today's model usage?</h2>
          <p>This changes usage from <strong>{usage.used}</strong> to <strong>0</strong> and
          immediately restores the daily allowance. It does not change the limit.</p>
          <form method="post" action="/model-usage/reset/apply{_token_query(request)}">
            <button class="warning-button" type="submit">Confirm usage reset</button>
            <a href="/model-usage{_token_query(request)}">Cancel</a>
          </form>
        </section>
        """
        return HTMLResponse(_page("Confirm usage reset", body))

    async def model_usage_reset_apply(request: Request) -> RedirectResponse | HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        selection = _select_profile(request, service, require_concrete=True)
        if isinstance(selection, HTMLResponse):
            return selection
        updated = service.reset_model_usage_today()
        notice = f"Today's model usage was reset to {updated.used}/{updated.limit}."
        return RedirectResponse(
            f"/model-usage{_query_with_values(request, notice=notice)}",
            status_code=303,
        )

    async def config_get(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied

        selection = _select_profile(request, service)
        if isinstance(selection, HTMLResponse):
            return selection
        values = service.read_config()
        if selection.is_all:
            body = f"""
            {_navigation(request, current="config", selection=selection)}
            {_profile_scope_heading(selection)}
            <section class="panel"><p>All searches is read-only. Select one profile to edit the
            shared global configuration.</p></section>
            """
            return HTMLResponse(_page("Watcher configuration", body))
        token_query = _token_query(request)
        notify_review_checkbox = _checkbox(
            "NOTIFY_REVIEW_ACTIONS", values, "Send reviews to Telegram"
        )
        notify_ai_failure_checkbox = _checkbox(
            "NOTIFY_AI_FAILURES", values, "Send production AI-failure alerts to Telegram"
        )
        send_images_checkbox = _checkbox(
            "SEND_IMAGE_CONTENT_TO_MODEL", values, "Allow model to inspect listing images"
        )
        fallback_enabled_checkbox = _checkbox(
            "FALLBACK_MODEL_ENABLED", values, "Enable fallback model"
        )
        fallback_json_checkbox = _checkbox(
            "FALLBACK_MODEL_JSON_MODE", values, "Structured JSON output for fallback"
        )
        disable_previews_checkbox = _checkbox(
            "TELEGRAM_DISABLE_WEB_PAGE_PREVIEW", values, "Disable previews"
        )
        profile_settings = _profile_settings_config_panel(request, values, selection)
        body = f"""
        {_navigation(request, current="config", selection=selection)}
        {_profile_scope_heading(selection)}
        {_warning_for_missing_token(service)}
                <form class="config-form" method="post" action="/config{token_query}">
                    {profile_settings}

                    <fieldset>
                        <legend>Schedule and filtering</legend>
                        <div class="grid">
                              {_input("POLL_INTERVAL_SECONDS", values, label="Interval (seconds)")}
                            {_input("MAX_ADS_PER_POLL", values, label="Maximum ads per run")}
                        </div>
                        <div class="checks">
                            {_checkbox("BOOTSTRAP_EXISTING_ADS", values, "Bootstrap current ads")}
                            {_checkbox("EXCLUDE_ADMARKT_ADS", values, "Exclude Admarkt listings")}
                        </div>
                    </fieldset>

                    <fieldset>
                        <legend>Decision policy</legend>
                        <div class="grid">
                            {_input("NOTIFY_MIN_CONFIDENCE", values, label="Notify threshold")}
                            {_input("REVIEW_MIN_CONFIDENCE", values, label="Review threshold")}
                        </div>
                        <div class="checks">
                                                        {notify_review_checkbox}
                                                        {notify_ai_failure_checkbox}
                        </div>
                    </fieldset>

                    <fieldset>
                        <legend>Model provider</legend>
                        {_provider_select(values)}
                        <p id="provider-help" class="provider-note"></p>
                        <div class="grid">
                            {_secret("MODEL_API_KEY", values, label="API key")}
                            {_input("MODEL_NAME", values, label="Model")}
                        </div>
                        <details class="advanced">
                            <summary>Advanced model settings</summary>
                            <div class="grid advanced-grid">
                                {_input("MODEL_BASE_URL", values, label="Base URL")}
                                <div id="reasoning-field">{_reasoning_select(values)}</div>
                                <div id="temperature-field">
                                    {_input("MODEL_TEMPERATURE", values, label="Temperature")}
                                </div>
                                {_input("MODEL_MAX_TOKENS", values, label="Maximum output tokens")}
                                {_input("MAX_IMAGES_FOR_MODEL", values, label="Maximum images")}
                            </div>
                            <div class="checks">
                                {_checkbox("MODEL_JSON_MODE", values, "Structured JSON output")}
                                {send_images_checkbox}
                            </div>
                            <p class="hint">Off by default. When disabled, no image URLs or image
                            instructions are sent to the model.</p>
                        </details>
                        <details class="advanced">
                            <summary>Fallback model</summary>
                            <div class="checks">
                                {fallback_enabled_checkbox}
                            </div>
                            <div class="grid advanced-grid">
                                {_provider_select(
                                    values,
                                    field_name="FALLBACK_MODEL_PROVIDER",
                                    label="Fallback provider",
                                    element_id="fallback-provider",
                                )}
                                {_secret(
                                    "FALLBACK_MODEL_API_KEY",
                                    values,
                                    label="Fallback API key",
                                )}
                                {_input(
                                    "FALLBACK_MODEL_BASE_URL",
                                    values,
                                    label="Fallback base URL",
                                )}
                                {_input("FALLBACK_MODEL_NAME", values, label="Fallback model")}
                                {_input(
                                    "FALLBACK_MODEL_REASONING_EFFORT",
                                    values,
                                    label="Fallback reasoning effort",
                                )}
                                {_input(
                                    "FALLBACK_MODEL_TEMPERATURE",
                                    values,
                                    label="Fallback temperature",
                                )}
                                {_input(
                                    "FALLBACK_MODEL_MAX_TOKENS",
                                    values,
                                    label="Fallback output tokens",
                                )}
                            </div>
                            <div class="checks">
                                {fallback_json_checkbox}
                            </div>
                            <p class="hint">Use this only when the primary model fails. JSON mode
                            remains explicitly configurable because support varies by provider and
                            model.</p>
                        </details>
                        {_provider_defaults_script()}
                    </fieldset>

                    <fieldset>
                        <legend>Telegram</legend>
                        <div class="grid">
                            {_secret("TELEGRAM_BOT_TOKEN", values, label="Bot token")}
                            {_input("TELEGRAM_CHAT_ID", values, label="Chat ID")}
                        </div>
                        <div class="checks">
                                                        {disable_previews_checkbox}
                        </div>
                    </fieldset>

                    <fieldset>
                        <legend>Web access</legend>
                        {_secret("WEB_ADMIN_TOKEN", values, label="Admin token")}
                    </fieldset>

                    <details class="runtime-settings">
                        <summary>Runtime settings</summary>
                        <div class="grid advanced-grid">
                            {_input("STATE_FILE", values, label="Seen-ad state file")}
                            {_input("RESULTS_FILE", values, label="Evaluation results file")}
                            {_input("STATUS_FILE", values, label="Runtime status file")}
                              {_input("REQUEST_TIMEOUT_SECONDS", values, label="Timeout (seconds)")}
                            {_input("USER_AGENT", values, label="HTTP user agent")}
                        </div>
                    </details>

                    <div class="form-actions">
                        <button type="submit">Save configuration</button>
                        <a href="/{token_query}">Back to status</a>
                    </div>
        </form>
        """
        return HTMLResponse(_page("Watcher configuration", body))

    async def config_post(request: Request) -> RedirectResponse | HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied

        selection = _select_profile(request, service, require_concrete=True)
        if isinstance(selection, HTMLResponse):
            return selection
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        current = service.read_config()
        file_values = parse_dotenv(service.env_file)
        submitted_provider = form.get("MODEL_PROVIDER", [current.get("MODEL_PROVIDER", "")])[
            0
        ].strip().lower()
        submitted_fallback_provider = form.get(
            "FALLBACK_MODEL_PROVIDER", [current.get("FALLBACK_MODEL_PROVIDER", "")]
        )[0].strip().lower()
        current_provider = current.get("MODEL_PROVIDER", "").strip().lower()
        provider_changed = submitted_provider != current_provider
        try:
            preset = provider_preset(submitted_provider)
        except ValueError as error:
            return HTMLResponse(
                _page("Invalid configuration", f"<p>{escape(str(error))}</p>"),
                status_code=400,
            )
        fallback_preset = None
        if submitted_fallback_provider:
            try:
                fallback_preset = provider_preset(submitted_fallback_provider)
            except ValueError as error:
                return HTMLResponse(
                    _page("Invalid configuration", f"<p>{escape(str(error))}</p>"),
                    status_code=400,
                )
        submitted_reasoning = form.get("MODEL_REASONING_EFFORT", [""])[0].strip().lower()
        if submitted_reasoning and submitted_reasoning not in REASONING_EFFORTS:
            return HTMLResponse(
                _page(
                    "Invalid configuration",
                    f"<p>Unsupported reasoning effort: {escape(submitted_reasoning)}</p>",
                ),
                status_code=400,
            )
        updated: dict[str, str] = {}
        editable_keys = (
            [
                key
                for key in EDITABLE_KEYS
                if key not in {"MARKTPLAATS_SEARCH_URL", "MARKTPLAATS_USE_CASE"}
            ]
            if selection.registry is not None
            else EDITABLE_KEYS
        )
        for key in editable_keys:
            if key in BOOLEAN_KEYS:
                updated[key] = "true" if key in form else "false"
                continue
            if key == "MODEL_PROVIDER":
                updated[key] = submitted_provider
                continue
            if key == "FALLBACK_MODEL_PROVIDER":
                updated[key] = submitted_fallback_provider
                continue
            if key == "MODEL_REASONING_EFFORT":
                updated[key] = submitted_reasoning
                continue

            submitted = form.get(key, [""])[0].strip()
            if key in SECRET_KEYS and not submitted:
                if key == "MODEL_API_KEY" and provider_changed:
                    updated[key] = ""
                elif key == "FALLBACK_MODEL_API_KEY" and submitted_fallback_provider:
                    updated[key] = file_values.get(key, "")
                elif (
                    key == "MODEL_API_KEY"
                    and current_provider == "deepseek"
                    and "DEEPSEEK_API_KEY" in file_values
                ):
                    updated[key] = file_values["DEEPSEEK_API_KEY"]
                elif key in file_values:
                    updated[key] = file_values[key]
                continue
            updated[key] = submitted

        reasoning_effort = updated.get("MODEL_REASONING_EFFORT", "")
        if not preset.supports_reasoning_effort or (
            reasoning_effort and reasoning_effort not in preset.allowed_reasoning_efforts
        ):
            updated["MODEL_REASONING_EFFORT"] = ""
        reasoning_disabled = updated.get("MODEL_REASONING_EFFORT", "") in {"", "none"}
        if not preset.supports_temperature or (
            preset.temperature_requires_no_reasoning and not reasoning_disabled
        ):
            updated["MODEL_TEMPERATURE"] = "0"

        fallback_reasoning_effort = updated.get("FALLBACK_MODEL_REASONING_EFFORT", "")
        if fallback_preset is None or not fallback_preset.supports_reasoning_effort or (
            fallback_reasoning_effort
            and fallback_reasoning_effort not in fallback_preset.allowed_reasoning_efforts
        ):
            updated["FALLBACK_MODEL_REASONING_EFFORT"] = ""
        fallback_reasoning_disabled = updated.get("FALLBACK_MODEL_REASONING_EFFORT", "") in {
            "",
            "none",
        }
        if fallback_preset is None or not fallback_preset.supports_temperature or (
            fallback_preset.temperature_requires_no_reasoning and not fallback_reasoning_disabled
        ):
            updated["FALLBACK_MODEL_TEMPERATURE"] = "0"

        persisted = {
            key: value
            for key, value in file_values.items()
            if key not in LEGACY_MODEL_KEYS
        }
        write_dotenv(service.env_file, {**persisted, **updated})
        return RedirectResponse(f"/{_token_query(request)}", status_code=303)

    async def run_now(request: Request) -> RedirectResponse | HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied

        selection = _select_profile(request, service, require_concrete=True)
        if isinstance(selection, HTMLResponse):
            return selection
        queued = (
            service.queue_run_profile(selection.profile)
            if selection.profile is not None
            else service.queue_run_once()
        )
        message = "Full run queued." if queued else "A run is already in progress."
        return RedirectResponse(
            f"/tools{_query_with_values(request, notice=message)}",
            status_code=303,
        )

    async def profiles_get(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        selection = _select_profile(request, service)
        if isinstance(selection, HTMLResponse):
            return selection
        if selection.registry is None:
            return HTMLResponse(
                _page(
                    "Profiles unavailable",
                    "<p class='alert'>Configure a Marktplaats search URL and evaluation instructions "
                    "before creating profiles.</p>",
                ),
                status_code=400,
            )
        create_panel = ""
        if not selection.is_all:
            create_panel = f"""
            <section class="panel">
              <h2>Create profile</h2>
              {_profile_form(request, action="/profiles/create", profile=None)}
            </section>
            """
        body = f"""
        {_navigation(request, current="profiles", selection=selection)}
        <section class="panel">
          <h2>Saved searches</h2>
          <p>Profile IDs are storage keys: they are immutable after creation. Archiving keeps all
          seen ads, evaluations, status, and pipeline history; it never deletes data.</p>
          {_profiles_table(request, selection.registry, show_actions=not selection.is_all)}
        </section>
        {create_panel}
        """
        return HTMLResponse(_page("Profile management", body))

    async def profile_edit_get(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        selection = _select_profile(request, service, require_concrete=True)
        if isinstance(selection, HTMLResponse):
            return selection
        try:
            registry = service.activate_profile_registry()
            profile = registry.profile(request.path_params["profile_id"])
        except (ProfileConfigurationError, ValueError) as error:
            return HTMLResponse(_page("Invalid profile", _notice(str(error), error="")), status_code=400)
        if profile.archived:
            return HTMLResponse(
                _page("Archived profile", "<p class='alert'>Archived profiles cannot be edited.</p>"),
                status_code=400,
            )
        body = f"""
        {_navigation(request, current="profiles", selection=selection)}
        <section class="panel">
          <h2>Edit {escape(profile.name)}</h2>
          <p class="hint">ID: {escape(profile.id)} (immutable)</p>
          {_profile_form(request, action=f"/profiles/{profile.id}/edit", profile=profile)}
        </section>
        """
        return HTMLResponse(_page("Edit profile", body))

    async def profile_edit_post(request: Request) -> RedirectResponse | HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        selection = _select_profile(request, service, require_concrete=True)
        if isinstance(selection, HTMLResponse):
            return selection
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        try:
            settings = service._load_settings()
            store = ProfileRegistryStore(settings.data_root)
            current = store.load().profile(request.path_params["profile_id"])
            if current.archived:
                raise ProfileConfigurationError("Archived profiles cannot be edited.")
            updated = SearchProfile(
                id=current.id,
                name=_profile_form_value(form, "name"),
                search_url=_profile_form_value(form, "search_url"),
                use_case=_profile_form_value(form, "use_case"),
                enabled="enabled" in form,
                sort_order=current.sort_order,
                bootstrap_existing_ads=current.bootstrap_existing_ads,
                poll_interval_seconds=_profile_interval(form),
                archived=False,
            )
            store.update(updated)
        except (ProfileConfigurationError, ValueError) as error:
            return HTMLResponse(
                _page("Invalid profile", f"<p class='alert'>{escape(str(error))}</p>"),
                status_code=400,
            )
        return RedirectResponse(
            f"/profiles{_query_with_values(request, notice='Profile updated.')}", status_code=303
        )

    async def profile_create(request: Request) -> RedirectResponse | HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        selection = _select_profile(request, service, require_concrete=True)
        if isinstance(selection, HTMLResponse):
            return selection
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        try:
            settings = service._load_settings()
            store = ProfileRegistryStore(settings.data_root)
            profile = SearchProfile(
                id=_profile_form_value(form, "id"),
                name=_profile_form_value(form, "name"),
                search_url=_profile_form_value(form, "search_url"),
                use_case=_profile_form_value(form, "use_case"),
                enabled="enabled" in form,
                sort_order=store.next_sort_order(),
                bootstrap_existing_ads=False,
                poll_interval_seconds=_profile_interval(form),
            )
            store.create(profile)
        except (ProfileConfigurationError, ValueError) as error:
            return HTMLResponse(
                _page("Invalid profile", f"<p class='alert'>{escape(str(error))}</p>"),
                status_code=400,
            )
        return RedirectResponse(
            f"/profiles{_query_with_values(request, notice='Profile created.')}", status_code=303
        )

    async def profile_toggle(request: Request) -> RedirectResponse | HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        selection = _select_profile(request, service, require_concrete=True)
        if isinstance(selection, HTMLResponse):
            return selection
        try:
            settings = service._load_settings()
            store = ProfileRegistryStore(settings.data_root)
            profile = store.load().profile(request.path_params["profile_id"])
            store.set_enabled(profile.id, enabled=not profile.enabled)
            message = "Profile enabled." if not profile.enabled else "Profile paused."
        except (ProfileConfigurationError, ValueError) as error:
            return HTMLResponse(
                _page("Invalid profile", f"<p class='alert'>{escape(str(error))}</p>"),
                status_code=400,
            )
        return RedirectResponse(
            f"/profiles{_query_with_values(request, notice=message)}", status_code=303
        )

    async def profile_archive(request: Request) -> RedirectResponse | HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        selection = _select_profile(request, service, require_concrete=True)
        if isinstance(selection, HTMLResponse):
            return selection
        try:
            settings = service._load_settings()
            ProfileRegistryStore(settings.data_root).archive(request.path_params["profile_id"])
        except (ProfileConfigurationError, ValueError) as error:
            return HTMLResponse(
                _page("Invalid profile", f"<p class='alert'>{escape(str(error))}</p>"),
                status_code=400,
            )
        return RedirectResponse(
            f"/profiles{_query_with_values(request, notice='Profile archived; history was retained.')}",
            status_code=303,
        )

    return Starlette(
        routes=[
            Route("/", index),
            Route("/healthz", health),
            Route("/api/status", status_json),
            Route("/evaluations", evaluations),
            Route("/api/evaluations", evaluations_json),
            Route("/seen", seen_ads),
            Route("/tools", tools),
            Route("/tools/fetch", tools_fetch, methods=["POST"]),
            Route("/tools/test", tools_test, methods=["POST"]),
            Route("/tools/telegram", tools_telegram, methods=["POST"]),
            Route("/tools/telegram-test", tools_telegram_test, methods=["POST"]),
            Route("/tools/full-run", full_run_confirm),
            Route("/diagnostics", diagnostics),
            Route("/model-usage", model_usage),
            Route("/model-usage/limit", model_usage_limit, methods=["POST"]),
            Route("/model-usage/limit/apply", model_usage_limit_apply, methods=["POST"]),
            Route("/model-usage/reset", model_usage_reset),
            Route("/model-usage/reset/apply", model_usage_reset_apply, methods=["POST"]),
            Route("/config", config_get, methods=["GET"]),
            Route("/config", config_post, methods=["POST"]),
            Route("/profiles", profiles_get),
            Route("/profiles/create", profile_create, methods=["POST"]),
            Route("/profiles/{profile_id}/edit", profile_edit_get, methods=["GET"]),
            Route("/profiles/{profile_id}/edit", profile_edit_post, methods=["POST"]),
            Route("/profiles/{profile_id}/toggle", profile_toggle, methods=["POST"]),
            Route("/profiles/{profile_id}/archive", profile_archive, methods=["POST"]),
            Route("/run-now", run_now, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


def _deny_if_needed(request: Request, service: WatcherService) -> HTMLResponse | None:
    configured_token = service.admin_token()
    if not configured_token:
        return None

    request_token = request.query_params.get("token")
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        request_token = auth_header[7:].strip()

    if request_token == configured_token:
        return None

    return HTMLResponse(_page("Unauthorized", "<p>Unauthorized.</p>"), status_code=401)


def _token_query(request: Request) -> str:
    values: dict[str, str] = {}
    token = request.query_params.get("token")
    profile = request.query_params.get("profile")
    if token:
        values["token"] = token
    if profile:
        values["profile"] = profile
    return f"?{urlencode(values)}" if values else ""


def _query_with_values(request: Request, **values: str) -> str:
    token = request.query_params.get("token")
    profile = request.query_params.get("profile")
    if token:
        values["token"] = token
    if profile and "profile" not in values:
        values["profile"] = profile
    return f"?{urlencode(values)}" if values else ""


def _query_with_token(request: Request, *, action: str) -> str:
    return _query_with_values(request, action=action)


def _profile_query(request: Request, profile_id: str) -> str:
    values = {"profile": profile_id}
    token = request.query_params.get("token")
    if token:
        values["token"] = token
    return f"?{urlencode(values)}"


def _navigation(request: Request, *, current: str, selection: ProfileSelection) -> str:
    token_query = _token_query(request)
    items = [
        ("dashboard", "/", "Dashboard"),
        ("evaluations", "/evaluations", "Evaluations"),
        ("seen", "/seen", "Seen ads"),
        ("tools", "/tools", "Pipeline tools"),
        ("usage", "/model-usage", "Model budget"),
        ("diagnostics", "/diagnostics", "Diagnostics"),
        ("config", "/config", "Configuration"),
        ("profiles", "/profiles", "Profiles"),
    ]
    links = []
    for key, path, label in items:
        active = ' class="active" aria-current="page"' if key == current else ""
        links.append(f'<a{active} href="{path}{token_query}">{label}</a>')
    return (
        f"<nav class='main-nav' aria-label='Main navigation'>{''.join(links)}</nav>"
        f"{_profile_selector(request, selection)}"
    )


def _profile_selector(request: Request, selection: ProfileSelection) -> str:
    if selection.registry is None:
        return ""
    selected_id = "all" if selection.is_all else (
        selection.profile.id if selection.profile is not None else DEFAULT_PROFILE_ID
    )
    options = []
    for profile in selection.registry.active_profiles:
        paused = " · paused" if not profile.enabled else ""
        selected = " selected" if profile.id == selected_id else ""
        options.append(
            f"<option value='{escape(profile.id)}'{selected}>{escape(profile.name + paused)}</option>"
        )
    options.append(
        "<option value='all'"
        f"{' selected' if selected_id == 'all' else ''}>All searches (read-only)</option>"
    )
    token = request.query_params.get("token")
    token_input = f"<input type='hidden' name='token' value='{escape(token)}'>" if token else ""
    return f"""
    <form class="profile-selector" method="get" action="{escape(request.url.path)}">
      {token_input}
      <label>Search profile
        <select name="profile" onchange="this.form.submit()">{''.join(options)}</select>
      </label>
      <button type="submit">Switch</button>
    </form>
    """


def _profile_scope_heading(selection: ProfileSelection) -> str:
    if selection.is_all:
        return "<p class='profile-scope'><strong>Scope:</strong> All searches · read-only aggregate</p>"
    if selection.profile is None:
        return ""
    paused = " · archived (read-only)" if selection.profile.archived else ""
    if not paused and not selection.profile.enabled:
        paused = " · paused"
    return (
        "<p class='profile-scope'><strong>Scope:</strong> "
        f"{escape(selection.profile.name)} · {escape(selection.profile.id)}{paused}</p>"
    )


def _profile_statuses_panel(service: WatcherService, selection: ProfileSelection) -> str:
    if not selection.is_all or selection.registry is None:
        return ""
    rows = []
    for profile in selection.registry.active_profiles:
        status = service.status_store_for(profile).read()
        state = "Paused" if not profile.enabled else "Enabled"
        rows.append(
            "<tr>"
            f"<td>{escape(profile.name)} <span class='secondary'>{escape(profile.id)}</span></td>"
            f"<td>{escape(state)}</td><td>{_format_time(status.last_finished_at)}</td>"
            f"<td>{status.total_evaluated}</td><td>{status.total_errors}</td>"
            "</tr>"
        )
    return f"""
    <section class="panel">
      <h2>Search status by profile</h2>
      <div class="table-scroll"><table>
        <thead><tr><th>Profile</th><th>Schedule</th><th>Last completed</th>
        <th>Evaluated</th><th>Errors</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table></div>
    </section>
    """


def _evaluations_download_name(selection: ProfileSelection) -> str:
    if selection.is_all:
        return "evaluations-all.json"
    if selection.profile is None:
        return "evaluations.json"
    return f"evaluations-{selection.profile.id}.json"


def _profile_settings_config_panel(
    request: Request,
    values: Mapping[str, str],
    selection: ProfileSelection,
) -> str:
    if selection.registry is None:
        return f"""
        <fieldset>
          <legend>Watch criteria</legend>
          {_input("MARKTPLAATS_SEARCH_URL", values, label="Marktplaats search URL")}
          {_textarea("MARKTPLAATS_USE_CASE", values, label="Evaluation instructions")}
        </fieldset>
        """
    return f"""
    <fieldset>
      <legend>Search profiles</legend>
      <p>Search URL and evaluation instructions are profile-specific. Manage them from
    <a href="/profiles{_token_query(request)}">Profile management</a>.
      Saving this global configuration never overwrites an active profile.</p>
      <details>
        <summary>Legacy single-search settings (read-only)</summary>
        <p><strong>URL:</strong> {escape(values.get("MARKTPLAATS_SEARCH_URL", ""))}</p>
        <p><strong>Instructions:</strong> {escape(values.get("MARKTPLAATS_USE_CASE", ""))}</p>
      </details>
    </fieldset>
    """
def _profiles_table(
    request: Request,
    registry: ProfileRegistry,
    *,
    show_actions: bool = True,
) -> str:
    rows = []
    for profile in sorted(registry.profiles, key=lambda item: item.sort_order):
        state = "Archived" if profile.archived else ("Enabled" if profile.enabled else "Paused")
        interval = str(profile.poll_interval_seconds) if profile.poll_interval_seconds else "Global"
        if not show_actions or profile.archived:
            actions = "—"
            if profile.archived:
                actions = (
                    f"<a href='/evaluations{_profile_query(request, profile.id)}'>View history</a> "
                    "<span class='secondary'>History retained</span>"
                )
        else:
            toggle_label = "Pause" if profile.enabled else "Enable"
            actions = (
                f"<a href='/profiles/{escape(profile.id)}/edit{_token_query(request)}'>Edit</a> "
                f"<form class='inline-form' method='post' "
                f"action='/profiles/{escape(profile.id)}/toggle{_token_query(request)}'>"
                f"<button type='submit'>{toggle_label}</button></form>"
            )
            if profile.id != registry.default_profile_id:
                actions += (
                    f" <form class='inline-form' method='post' "
                    f"action='/profiles/{escape(profile.id)}/archive{_token_query(request)}'>"
                    "<button class='warning-button' type='submit'>Archive</button></form>"
                )
        rows.append(
            f"""
            <tr><td>{escape(profile.name)}<span class="secondary">{escape(profile.id)}</span></td>
              <td>{escape(state)}</td><td>{escape(interval)}</td>
              <td>{actions}</td></tr>
            """
        )
    return f"""
    <div class="table-scroll"><table>
      <thead><tr><th>Profile</th><th>State</th><th>Interval (seconds)</th><th>Actions</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table></div>
    """


def _profile_form(request: Request, *, action: str, profile: SearchProfile | None) -> str:
    values = {
        "id": profile.id if profile is not None else "",
        "name": profile.name if profile is not None else "",
        "search_url": profile.search_url if profile is not None else "",
        "use_case": profile.use_case if profile is not None else "",
        "poll_interval_seconds": (
            str(profile.poll_interval_seconds) if profile and profile.poll_interval_seconds else ""
        ),
    }
    id_input = (
        f"<label>Immutable ID<input name='id' required pattern='[a-z][a-z0-9-]{{0,62}}' "
        f"value='{escape(values['id'])}'></label>"
        if profile is None
        else ""
    )
    enabled = " checked" if profile is None or profile.enabled else ""
    return f"""
    <form class="config-form" method="post" action="{action}{_token_query(request)}">
      {_token_hidden_input(request)}
      {id_input}
      <label>Name<input name="name" required value="{escape(values['name'])}"></label>
      <label>Marktplaats search URL<input name="search_url" required value="{escape(values['search_url'])}"></label>
      <label>Evaluation instructions<textarea name="use_case" required>{escape(values['use_case'])}</textarea></label>
      <label>Interval override (seconds; blank uses global)
        <input type="number" name="poll_interval_seconds" min="1" value="{escape(values['poll_interval_seconds'])}"></label>
      <div class="checks"><label><input type="checkbox" name="enabled"{enabled}> Enabled</label></div>
      <div class="form-actions"><button type="submit">Save profile</button>
        <a href="/profiles{_token_query(request)}">Cancel</a></div>
    </form>
    """


def _profile_form_value(form: Mapping[str, list[str]], name: str) -> str:
    return form.get(name, [""])[0].strip()


def _profile_interval(form: Mapping[str, list[str]]) -> int | None:
    value = _profile_form_value(form, "poll_interval_seconds")
    return int(value) if value else None


def _status_panel(status: RuntimeStatus) -> str:
    has_evaluation_failures = bool(
        status.last_summary and status.last_summary.evaluation_failed_count
    )
    if status.last_error or has_evaluation_failures:
        state, css_class = "Needs attention", "status-error"
    elif status.is_running:
        state, css_class = "Running", "status-running"
    elif status.last_finished_at:
        state, css_class = "Scheduled", "status-ok"
    else:
        state, css_class = "Never run", "status-neutral"

    error = (
        f"<p class='alert' role='alert'><strong>Last error:</strong> "
        f"{escape(status.last_error)}</p>"
        if status.last_error
        else ""
    )
    return f"""
    <section class="panel">
      <div class="section-heading">
        <h2>Operating status</h2>
        <span class="status-badge {css_class}">{state}</span>
      </div>
      <dl class="status-list">
        <div><dt>Last completed</dt><dd>{_format_time(status.last_finished_at)}</dd></div>
        <div><dt>Next scheduled run</dt><dd>{_format_time(status.next_run_at)}</dd></div>
      </dl>
      {error}
    </section>
    """


def _last_run_panel(status: RuntimeStatus) -> str:
    summary = status.last_summary
    if summary is None:
        return """
        <section class="panel">
          <h2>Last run</h2>
          <p>No run has completed yet.</p>
        </section>
        """
    baseline = (
        f"<span class='mini-badge'>Baseline {summary.bootstrapped_count}</span>"
        if summary.bootstrapped_count
        else ""
    )
    failures = _evaluation_failures(summary, finished_at=status.last_finished_at)
    run_label = f"Run #{status.total_runs}" if status.total_runs else "Run before counter reset"
    return f"""
    <section class="panel">
      <h2>Latest completed run · {run_label} · {_format_time(status.last_finished_at)}</h2>
      <p class="pipeline-summary">
        <strong>{summary.fetched_count}</strong> fetched
        <span>→</span> <strong>{summary.kept_count}</strong> eligible
        <span>·</span> {summary.filtered_count} filtered
        <span>→</span> <strong>{summary.new_count}</strong> new
        <span>→</span> <strong>{summary.evaluated_count}</strong> evaluated
      </p>
      <div class="badge-row">
        <span class="mini-badge decision-notify">Notify {summary.notify_action_count}</span>
        <span class="mini-badge decision-review">Review {summary.review_count}</span>
        <span class="mini-badge decision-ignore">Ignore {summary.ignored_count}</span>
                <span class="mini-badge status-error">
                    AI failed {summary.evaluation_failed_count}
                </span>
        <span class="mini-badge">Telegram {summary.notified_count}</span>
        {baseline}
      </div>
        {failures}
    </section>
    """


def _evaluation_failures(
    summary: WatcherRunSummary,
    *,
    finished_at: datetime | None,
) -> str:
        if not summary.evaluation_failures:
                return ""
        items = "".join(
                f"<li><a href='{escape(failure.url)}' target='_blank' rel='noopener noreferrer'>"
                f"{escape(failure.title)}</a>: {escape(failure.error)}. "
                "The ad remains pending and will retry on the next production run.</li>"
                for failure in summary.evaluation_failures
        )
        return (
            "<div class='alert' role='alert'><strong>AI failures in this latest completed run "
            f"({_format_time(finished_at)})</strong>"
            f"<ul>{items}</ul></div>"
        )


def _model_usage_panel(
        usage: ModelUsageSnapshot,
        request: Request,
        *,
        compact: bool = False,
) -> str:
        heading = "Model request budget" if not compact else "Model budget"
        in_flight = (
            f" {usage.in_flight} request(s) currently in flight."
            if usage.in_flight
            else ""
        )
        return f"""
        <section class="panel usage-panel">
            <div class="section-heading">
                <h2>{heading}</h2>
                <strong>{usage.used} / {usage.limit}</strong>
            </div>
            <progress value="{usage.used}" max="{usage.limit}">
                {usage.used} of {usage.limit}
            </progress>
            <p>{usage.remaining} request(s) remaining.{in_flight} Resets
                {_format_time(usage.reset_at)}.
                <a href="/model-usage{_token_query(request)}">Manage limit</a>
            </p>
        </section>
        """


def _metric(label: str, value: int) -> str:
    return f"<div class='metric'><strong>{value}</strong><span>{escape(label)}</span></div>"


def _format_time(value: datetime | str | None) -> str:
    text, iso_value = _format_time_parts(value)
    if iso_value is None:
        return text
    return f"<time datetime='{escape(iso_value)}'>{text}</time>"


def _format_time_text(value: datetime | str | None) -> str:
    return _format_time_parts(value)[0]


def _format_time_parts(value: datetime | str | None) -> tuple[str, str | None]:
    if value is None:
        return "Not available", None
    parsed = value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return escape(value), None
    assert isinstance(parsed, datetime)
    display = parsed.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC")
    return display, parsed.isoformat()


def _notice(message: str | None, *, error: str | None = None) -> str:
    if error:
        return f"<p class='alert' role='alert'>{escape(error)}</p>"
    if message:
        return f"<p class='notice' role='status'>{escape(message)}</p>"
    return ""


def _safe_error(prefix: str, error: Exception) -> str:
    if isinstance(error, ValueError | ModelProviderError | ModelDailyLimitExceeded):
        return f"{prefix}: {error}"
    return f"{prefix}. Check the watcher logs for technical details and try again."


def _token_hidden_input(request: Request) -> str:
    token = request.query_params.get("token")
    profile = request.query_params.get("profile")
    inputs = []
    if token:
        inputs.append(f"<input type='hidden' name='token' value='{escape(token)}'>")
    if profile:
        inputs.append(f"<input type='hidden' name='profile' value='{escape(profile)}'>")
    return "".join(inputs)


def _evaluation_action(request: Request) -> str:
    action = request.query_params.get("action", "all").strip().lower()
    return action if action in {"all", "notify", "review", "ignore"} else "all"


def _seen_filter(request: Request) -> str:
    kind = request.query_params.get("kind", "all").strip().lower()
    return kind if kind in {"all", "baseline", "processed", "recorded"} else "all"


def _read_seen_ads(path: Path, *, kind: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    loaded = json.loads(path.read_text(encoding="utf-8"))
    raw_entries = loaded.get("seen_ads", {})
    if not isinstance(raw_entries, dict):
        raise ValueError("Seen-ad history has an unexpected format.")

    entries: list[dict[str, Any]] = []
    for ad_id, value in raw_entries.items():
        if not isinstance(value, dict):
            continue
        entry = {"id": str(ad_id), **value}
        entry["kind"] = _seen_kind(entry)
        if kind == "all" or entry["kind"] == kind:
            entries.append(entry)
    return sorted(entries, key=lambda entry: str(entry.get("first_seen_at", "")), reverse=True)


def _seen_kind(entry: Mapping[str, Any]) -> str:
    if entry.get("bootstrapped") is True:
        return "baseline"
    if isinstance(entry.get("evaluation"), dict):
        return "processed"
    return "recorded"


def _seen_filter_options(selected: str) -> str:
    options = [
        ("all", "All seen ads"),
        ("baseline", "Baseline"),
        ("processed", "Processed"),
        ("recorded", "Recorded"),
    ]
    return "".join(
        f"<option value='{value}'{' selected' if value == selected else ''}>{label}</option>"
        for value, label in options
    )


def _seen_ads_table(entries: list[dict[str, Any]], *, show_profile: bool = False) -> str:
    if not entries:
        return "<p>No seen ads match this filter yet.</p>"
    rows = []
    explanations = {
        "baseline": "Present when tracking started; skipped AI evaluation.",
        "processed": "Processed as a newly discovered ad by the normal pipeline.",
        "recorded": "Seen reason was not recorded by this version.",
    }
    for entry in entries:
        kind = str(entry["kind"])
        evaluation = entry.get("evaluation")
        decision = "—"
        if isinstance(evaluation, dict):
            action = escape(str(evaluation.get("next_action", "unknown")))
            confidence = evaluation.get("confidence")
            confidence_text = f" · {float(confidence):.0%}" if confidence is not None else ""
            decision = f"{action}{confidence_text}"
        rows.append(
            f"""
            <tr>
                            {f"<td>{escape(str(entry.get('profile_name', 'Freezers')))}<span class='secondary'>{escape(str(entry.get('profile_id', 'freezers')))}</span></td>" if show_profile else ''}
              <td><a href="{escape(str(entry.get('url', '')))}" rel="noopener noreferrer"
                target="_blank">{escape(str(entry.get('title', entry['id'])))}</a>
                <span class="secondary">{escape(str(entry['id']))}</span></td>
              <td><span class="seen-badge seen-{kind}">{kind}</span>
                <span class="secondary">{explanations[kind]}</span></td>
              <td>{_format_time(entry.get('first_seen_at'))}</td>
              <td>{decision}</td>
            </tr>
            """
        )
    return f"""
    <div class="table-scroll"><table class="history-table">
            <thead><tr>{'<th>Profile</th>' if show_profile else ''}<th>Ad</th><th>Seen reason</th><th>First seen</th><th>Decision</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table></div>
    """


def _preview_summary(service: WatcherService, profile: SearchProfile | None = None) -> str:
    fetched_at = service.preview_fetched_at_for(profile)
    if fetched_at is None:
        return "<p class='hint'>No preview fetched in this process yet.</p>"
    fetched, eligible, filtered = service.preview_counts_for(profile)
    return (
        f"<p><strong>{fetched}</strong> fetched · <strong>{eligible}</strong> eligible · "
        f"{filtered} filtered · fetched {_format_time(fetched_at)}</p>"
    )


def _preview_ads_form(
    request: Request,
    ads: list[Ad],
    seen_by_id: Mapping[str, Mapping[str, Any]],
    failures_by_id: Mapping[str, str],
) -> str:
    if not ads:
        return ""
    cards = []
    for index, ad in enumerate(ads):
        seen_entry = seen_by_id.get(ad.id)
        if seen_entry:
            kind = _seen_kind(seen_entry)
            state = f"<span class='seen-badge seen-{kind}'>{kind}</span>"
            state_detail = ""
        elif ad.id in failures_by_id:
            state = "<span class='seen-badge status-error'>AI failed · pending</span>"
            state_detail = (
                f'<span class="secondary failure-detail">{escape(failures_by_id[ad.id])}. '
                "The production watcher will retry this ad.</span>"
            )
        else:
            state = "<span class='seen-badge status-running'>new · pending</span>"
            state_detail = (
                "<span class='secondary'>Not in seen state; production evaluation has not "
                "completed yet.</span>"
            )
        details = " · ".join(value for value in [ad.price, ad.location, ad.seller] if value)
        display_details = escape(details) if details else "No price, location, or seller supplied."
        cards.append(
            f"""
            <label class="preview-card">
              <input type="radio" name="ad_id" value="{escape(ad.id)}"
                {'checked' if index == 0 else ''}>
                            <span class="preview-content">
                                <span class="preview-heading">
                                    <strong>{escape(ad.title)}</strong> {state}
                                </span>
                                <span>{display_details}</span>
                                <span class="secondary">
                                    ID {escape(ad.id)} · {len(ad.image_urls)} image(s) ·
                                    <a href="{escape(ad.url)}" target="_blank"
                                        rel="noopener noreferrer">Open ad</a>
                                </span>
                                {state_detail}
              </span>
            </label>
            """
        )
    return f"""
    <form method="post" action="/tools/test{_token_query(request)}">
      <fieldset class="preview-list">
        <legend>Select one eligible ad</legend>
        {''.join(cards)}
      </fieldset>
      <button type="submit">Test AI for selected ad</button>
    </form>
    """


def _pipeline_progress_cards(
    request: Request,
    records: list[PipelineProgressRecord],
    *,
    read_only: bool = False,
) -> str:
    del request
    if not records:
        return "<p class='hint'>No saved manual AI results yet. Fetch ads and test one.</p>"
    cards = []
    for record in records:
        if record.telegram_sent is True:
            telegram = f"Telegram sent {_format_time(record.telegram_sent_at)}"
        elif record.telegram_sent is False:
            telegram = "Telegram not sent"
        else:
            telegram = "Telegram delivery not tracked by test pipeline"
        source = "Manual AI test" if record.source == "manual_test" else "Production evaluation"
        profile_badge = ""
        if record.evaluated_ad.profile_name:
            profile_badge = (
                f"<span class='mini-badge'>"
                f"{escape(record.evaluated_ad.profile_name)} · "
                f"{escape(record.evaluated_ad.profile_id or '')}</span>"
            )
        cards.append(
            f"""
            <div class="pipeline-progress">
              <p class="badge-row">
                <span class="mini-badge status-ok">AI complete · saved</span>
                <span class="mini-badge">{source}</span>
                {profile_badge}
                <span class="mini-badge">{telegram}</span>
                <span class="hint">Tested {_format_time(record.tested_at)}</span>
              </p>
              {_evaluation_cards([record.evaluated_ad], show_profile=read_only)}
            </div>
            """
        )
    return "".join(cards)


def _pipeline_telegram_actions(
    request: Request,
    records: list[PipelineProgressRecord],
) -> str:
    if not records:
        return "<p class='hint'>No saved AI result is available for Telegram.</p>"
    forms = []
    for record in records:
        ad = record.evaluated_ad.ad
        label = "Send again" if record.telegram_sent is True else "Send result"
        forms.append(
            f"""
            <form class="telegram-result-action" method="post"
              action="/tools/telegram{_token_query(request)}">
              <input type="hidden" name="ad_id" value="{escape(ad.id)}">
              <span><strong>{escape(ad.title)}</strong>
                <span class="secondary">{escape(record.evaluated_ad.result.next_action)} ·
                {record.evaluated_ad.result.confidence:.0%}</span>
              </span>
              <button type="submit">{label} via Telegram</button>
            </form>
            """
        )
    return "<div class='telegram-result-list'>" + "".join(forms) + "</div>"


def _read_evaluations(path: Path, *, action: str) -> list[EvaluatedAd]:
    if not path.exists():
        return []

    evaluations: list[EvaluatedAd] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            evaluation = EvaluatedAd.model_validate_json(line)
        except ValueError:
            LOGGER.warning("Skipping an invalid evaluation record in %s.", path)
            continue
        if action == "all" or evaluation.result.next_action == action:
            evaluations.append(evaluation)

    return sorted(evaluations, key=lambda evaluation: evaluation.evaluated_at, reverse=True)


def _evaluation_filter_options(selected_action: str) -> str:
    options = [
        ("all", "All decisions"),
        ("notify", "Notify"),
        ("review", "Review"),
        ("ignore", "Ignore"),
    ]
    return "".join(
        f"<option value='{value}'{' selected' if value == selected_action else ''}>{label}</option>"
        for value, label in options
    )


def _evaluation_cards(
    evaluations: list[EvaluatedAd],
    *,
    test_only: bool = False,
    show_profile: bool = True,
) -> str:
    if not evaluations:
        return "<p>No evaluations match this filter yet.</p>"

    cards = []
    for evaluation in evaluations:
        ad = evaluation.ad
        result = evaluation.result
        metadata = " · ".join(value for value in [ad.price, ad.location, ad.seller] if value)
        signals = _evaluation_list("Signals", result.signals)
        concerns = _evaluation_list("Concerns", result.concerns)
        details = ""
        if signals or concerns or ad.description:
            description = (
                f"<p><strong>Description:</strong> {escape(ad.description)}</p>"
                if ad.description
                else ""
            )
            details = "<details><summary>Signals, concerns, and description</summary>"
            details += f"{signals}{concerns}{description}</details>"
        test_badge = "<span class='mini-badge'>Test only</span>" if test_only else ""
        profile_badge = ""
        if show_profile and evaluation.profile_name:
            profile_badge = (
                f"<span class='mini-badge'>"
                f"{escape(evaluation.profile_name)} · {escape(evaluation.profile_id or '')}</span>"
            )
        cards.append(
            f"""
            <article class="evaluation-card">
              <div class="evaluation-heading">
                <span class="decision decision-{escape(result.next_action)}">
                  {escape(result.next_action)}
                </span>
                <strong>{escape(ad.title)}</strong>
                {test_badge}
                {profile_badge}
              </div>
              <p><a href="{escape(ad.url)}" rel="noopener noreferrer" target="_blank">
                Open Marktplaats ad
              </a></p>
              <p>{escape(metadata) if metadata else 'No price, location, or seller supplied.'}</p>
              <p><strong>Confidence:</strong> {result.confidence:.0%}</p>
              <p><strong>Reason:</strong> {escape(result.reason)}</p>
              {details}
              <p class="hint">Evaluated {_format_time(evaluation.evaluated_at)}</p>
            </article>
            """
        )
    return "".join(cards)


def _evaluation_list(label: str, values: list[str]) -> str:
    if not values:
        return ""
    items = "".join(f"<li>{escape(value)}</li>" for value in values)
    return f"<p><strong>{escape(label)}:</strong></p><ul>{items}</ul>"


def _recent_logs_table(entries: list[dict[str, str]]) -> str:
    if not entries:
        return "<p>No log messages have been captured since this container started.</p>"
    rows = []
    for entry in reversed(entries):
        level = entry.get("level", "INFO").lower()
        detail = entry.get("detail", "")
        rendered_message = escape(entry.get("message", ""))
        if detail:
            rendered_message += (
                "<details class='diagnostic-detail'><summary>Show response</summary>"
                f"<pre>{escape(detail)}</pre></details>"
            )
        rows.append(
            f"""
            <tr>
              <td>{_format_time(entry.get('timestamp'))}</td>
              <td><span class="log-level log-{escape(level)}">
                {escape(level)}
              </span></td>
              <td>{escape(entry.get('logger', ''))}</td>
              <td class="log-message">{rendered_message}</td>
            </tr>
            """
        )
    return f"""
    <div class="table-scroll"><table class="log-table">
      <thead><tr><th>Time</th><th>Level</th><th>Logger</th><th>Message</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table></div>
    """


def _warning_for_missing_token(service: WatcherService) -> str:
    if service.admin_token():
        return ""

    return "<p class='warning'>WEB_ADMIN_TOKEN is not configured. Do not expose this page.</p>"


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
        * {{ box-sizing: border-box; }}
        body {{
            background: #f7f8fa;
            color: #222;
            font-family: system-ui, sans-serif;
            margin: 0 auto;
            max-width: 1280px;
            padding: 1.5rem;
            width: 100%;
        }}
        h1 {{ font-size: 1.65rem; margin: 0 0 1.25rem; }}
        h2 {{ font-size: 1.15rem; margin: 0 0 0.85rem; }}
        a {{ color: #245b8f; }}
        table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
        td, th {{
            border: 1px solid #ddd;
            padding: 0.55rem;
            text-align: left;
            vertical-align: top;
        }}
        th {{ background: #f3f5f7; }}
        input, textarea, select {{
            background: #fff;
            border: 1px solid #aaa;
            border-radius: 5px;
            box-sizing: border-box;
            color: inherit;
            font: inherit;
            margin-top: 0.3rem;
            padding: 0.5rem;
            width: 100%;
        }}
        input:focus, textarea:focus, select:focus {{
            border-color: #356aa0;
            outline: 2px solid #bed7ee;
            outline-offset: 1px;
        }}
    textarea {{ min-height: 12rem; }}
        label {{ display: block; margin: 0; }}
        button, .button-link {{
            background: #315f8c;
            border: 1px solid #274d72;
            border-radius: 5px;
            color: white;
            cursor: pointer;
            display: inline-block;
            font: inherit;
            min-height: 2.65rem;
            padding: 0.55rem 0.9rem;
            text-decoration: none;
        }}
        .main-nav {{
            display: flex;
            flex-wrap: wrap;
            gap: 0.35rem;
            margin: 0 0 1.25rem;
        }}
        .main-nav a {{
            border-radius: 5px;
            color: #315f8c;
            padding: 0.45rem 0.65rem;
            text-decoration: none;
        }}
        .main-nav a:hover, .main-nav a.active {{ background: #e5edf5; }}
        .profile-selector {{
            align-items: end;
            background: #eef3f8;
            border: 1px solid #d5e0eb;
            border-radius: 7px;
            display: flex;
            gap: 0.7rem;
            margin: -0.6rem 0 1.25rem;
            padding: 0.7rem;
        }}
        .profile-selector label {{ flex: 1 1 18rem; max-width: 28rem; }}
        .profile-selector button {{ flex: 0 0 auto; }}
        .panel {{
            background: white;
            border: 1px solid #dfe3e7;
            border-radius: 7px;
            margin: 0 0 1rem;
            padding: 1rem;
        }}
        .section-heading {{
            align-items: center;
            display: flex;
            gap: 0.75rem;
            justify-content: space-between;
        }}
        .status-badge, .mini-badge, .seen-badge {{
            border-radius: 1rem;
            display: inline-block;
            font-size: 0.78rem;
            font-weight: 700;
            padding: 0.22rem 0.55rem;
            text-transform: uppercase;
        }}
        .status-ok, .status-running, .seen-processed {{ background: #dcefe3; color: #145c31; }}
        .status-error {{ background: #f7dddd; color: #8a2020; }}
        .status-neutral, .seen-recorded {{ background: #e8eaed; color: #4f5358; }}
        .seen-baseline {{ background: #e4ecf6; color: #315f8c; }}
        .status-list {{
            display: grid;
            gap: 0.7rem;
            grid-template-columns: repeat(2, 1fr);
            margin: 0;
        }}
        .status-list div {{ background: #f7f8fa; border-radius: 5px; padding: 0.7rem; }}
        .status-list dt {{ color: #555; font-size: 0.82rem; }}
        .status-list dd {{ margin: 0.2rem 0 0; }}
        .pipeline-summary {{ align-items: center; display: flex; flex-wrap: wrap; gap: 0.45rem; }}
        .badge-row, .action-row {{
            align-items: center;
            display: flex;
            flex-wrap: wrap;
            gap: 0.55rem;
        }}
        .badge-row + .alert {{ margin-top: 1rem; }}
        .mini-badge {{ background: #e8eaed; color: #30343a; }}
        .metric-grid {{
            display: grid;
            gap: 0.7rem;
            grid-template-columns: repeat(auto-fit, minmax(8rem, 1fr));
        }}
        .metric {{ background: #f7f8fa; border-radius: 5px; padding: 0.8rem; }}
        .metric strong {{ display: block; font-size: 1.45rem; }}
        .metric span {{ color: #555; font-size: 0.85rem; }}
        fieldset, details.runtime-settings {{
            border: 1px solid #d5d5d5;
            border-radius: 6px;
            margin: 0;
            padding: 1rem;
        }}
        legend {{ font-weight: 650; padding: 0 0.35rem; }}
        summary {{ cursor: pointer; font-weight: 600; }}
        .config-form {{ display: grid; gap: 1rem; }}
        .grid {{
            display: grid;
            gap: 0.9rem;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
        }}
        fieldset > .grid, .advanced-grid {{ margin-top: 0.8rem; }}
        .advanced {{ border-top: 1px solid #e1e1e1; margin-top: 1rem; padding-top: 0.8rem; }}
        .checks {{ display: grid; gap: 0.5rem; margin-top: 0.9rem; }}
        .checks label {{ align-items: center; display: flex; gap: 0.5rem; }}
        .checks input {{ flex: 0 0 auto; margin: 0; width: auto; }}
        .provider-note {{
            background: #f5f7f9;
            border-left: 3px solid #7892aa;
            color: #444;
            font-size: 0.9rem;
            margin: 0.8rem 0;
            padding: 0.55rem 0.7rem;
        }}
        .form-actions {{
            align-items: center;
            border-top: 1px solid #ddd;
            display: flex;
            gap: 1rem;
            padding-top: 1rem;
        }}
        .inline-form {{ display: inline-block; margin: 0 0.35rem 0.35rem 0; }}
        .inline-form button {{ min-height: 2rem; padding: 0.3rem 0.55rem; }}
        .profile-scope {{ color: #454d55; font-size: 0.9rem; margin: -0.55rem 0 1rem; }}
        .filter-form {{ align-items: end; display: flex; flex-wrap: wrap; gap: 0.75rem; }}
        .filter-form label {{ flex: 0 1 18rem; min-width: 12rem; }}
        .filter-form select {{ max-width: 18rem; }}
        input[type="number"] {{ max-width: 10rem; }}
        .model-limit-form button {{ margin-top: 0.75rem; }}
        .evaluation-card {{
            border: 1px solid #d5d5d5;
            border-radius: 6px;
            margin: 1rem 0;
            padding: 1rem;
        }}
        .evaluation-card p {{ margin: 0.55rem 0; }}
        .evaluation-heading {{ align-items: center; display: flex; flex-wrap: wrap; gap: 0.6rem; }}
        .decision {{
            border-radius: 1rem;
            color: white;
            font-size: 0.8rem;
            font-weight: 700;
            padding: 0.2rem 0.55rem;
            text-transform: uppercase;
        }}
        .decision-notify {{ background: #16733c; }}
        .decision-review {{ background: #956500; }}
        .decision-ignore {{ background: #5a5a5a; }}
        .table-scroll {{ overflow-x: auto; }}
        .history-table {{ min-width: 720px; }}
        .log-table {{ min-width: 850px; }}
        .log-table th:first-child, .log-table td:first-child {{
            min-width: 12.5rem;
            white-space: nowrap;
        }}
        .log-message {{ font-family: ui-monospace, monospace; font-size: 0.82rem; }}
        .diagnostic-detail {{ margin-top: 0.45rem; }}
        .diagnostic-detail pre {{
            margin-bottom: 0;
            overflow-wrap: anywhere;
            white-space: pre-wrap;
        }}
        .log-level {{
            border-radius: 1rem;
            font-size: 0.72rem;
            font-weight: 700;
            padding: 0.18rem 0.45rem;
        }}
        .log-error, .log-critical {{ background: #f7dddd; color: #8a2020; }}
        .log-warning {{ background: #fff0ca; color: #765000; }}
        .log-info, .log-debug {{ background: #e8eaed; color: #4f5358; }}
        .secondary {{ color: #5f646a; display: block; font-size: 0.82rem; margin-top: 0.25rem; }}
        .failure-detail {{ color: #8a2020; overflow-wrap: anywhere; word-break: break-word; }}
        .usage-panel progress {{ height: 0.85rem; width: 100%; }}
        .usage-panel p {{ margin-bottom: 0; }}
        .preview-list {{ display: grid; gap: 0.55rem; margin: 1rem 0; }}
        .preview-card {{
            align-items: start;
            background: #fafbfc;
            border: 1px solid #dfe3e7;
            border-radius: 6px;
            display: grid;
            gap: 0.3rem;
            grid-template-columns: auto minmax(0, 1fr);
            padding: 0.75rem;
        }}
        .preview-card input {{ margin: 0.25rem 0.35rem 0 0; width: auto; }}
        .preview-content {{ display: grid; gap: 0.3rem; min-width: 0; }}
        .preview-heading {{ align-items: center; display: flex; flex-wrap: wrap; gap: 0.4rem; }}
        .pipeline-progress {{ border-top: 1px solid #dfe3e7; margin-top: 1rem; padding-top: 1rem; }}
        .telegram-result-list {{ display: grid; gap: 0.65rem; }}
        .telegram-result-action {{
            align-items: center;
            background: #fafbfc;
            border: 1px solid #dfe3e7;
            border-radius: 6px;
            display: flex;
            flex-wrap: wrap;
            gap: 0.75rem;
            justify-content: space-between;
            padding: 0.75rem;
        }}
        .full-run-panel {{ border-color: #d6ad58; }}
        .warning-button {{ background: #956500; border-color: #765000; }}
        .notice, .alert, .warning {{ border-radius: 5px; padding: 0.7rem; }}
        .notice {{ background: #e2f1e7; border: 1px solid #8ab49a; }}
        .alert {{ background: #f8e1e1; border: 1px solid #d49a9a; }}
        .warning {{
            background: #fff4ce;
            border: 1px solid #e0b100;
            border-radius: 5px;
            padding: 0.7rem;
        }}
        .hint {{ color: #555; font-size: 0.9rem; }}
        [hidden] {{ display: none !important; }}
        @media (max-width: 600px) {{
            body {{ padding: 1rem; }}
            .grid {{ grid-template-columns: 1fr; }}
            .metric-grid, .status-list {{ grid-template-columns: repeat(2, 1fr); }}
            .main-nav a {{ flex: 1 1 auto; text-align: center; }}
        }}
  </style>
</head>
<body>
  <h1>{escape(title)}</h1>
  {body}
</body>
</html>"""


def _row(label: str, value: object) -> str:
    return f"<tr><th>{escape(label)}</th><td>{escape(str(value))}</td></tr>"


def _input(key: str, values: Mapping[str, str], *, label: str) -> str:
    value = values.get(key, "")
    return f"<label>{escape(label)}<input name='{key}' value='{escape(value)}'></label>"


def _textarea(key: str, values: Mapping[str, str], *, label: str) -> str:
    value = values.get(key, "")
    return f"<label>{escape(label)}<textarea name='{key}'>{escape(value)}</textarea></label>"


def _secret(key: str, values: Mapping[str, str], *, label: str) -> str:
    configured = "configured" if _secret_is_configured(values.get(key, "")) else "not configured"
    return (
        f"<label>{escape(label)}<input type='password' name='{key}' "
        f"placeholder='Leave blank to keep current ({configured})'></label>"
    )


def _secret_is_configured(value: str) -> bool:
    stripped = value.strip()
    return bool(stripped) and stripped.lower() not in {"replace-me", "changeme", "none", "null"}


def _provider_select(
    values: Mapping[str, str],
    *,
    field_name: str = "MODEL_PROVIDER",
    label: str = "Provider",
    element_id: str = "model-provider",
) -> str:
    options = [(provider.id, provider.label) for provider in PROVIDER_PRESETS.values()]
    return _select(
        field_name,
        values,
        label=label,
        options=options,
        element_id=element_id,
    )


def _reasoning_select(values: Mapping[str, str]) -> str:
    options = [("", "Provider default")]
    options.extend((effort, effort.capitalize()) for effort in sorted(REASONING_EFFORTS))
    return _select(
        "MODEL_REASONING_EFFORT",
        values,
        label="Reasoning effort",
        options=options,
    )


def _select(
    key: str,
    values: Mapping[str, str],
    *,
    label: str,
    options: list[tuple[str, str]],
    element_id: str | None = None,
) -> str:
    selected_value = values.get(key, "")
    rendered_options = []
    for value, text in options:
        selected = " selected" if value == selected_value else ""
        rendered_options.append(
            f"<option value='{escape(value)}'{selected}>{escape(text)}</option>"
        )
    id_attribute = f" id='{escape(element_id)}'" if element_id else ""
    return (
        f"<label>{escape(label)}<select name='{key}'{id_attribute}>"
        f"{''.join(rendered_options)}</select></label>"
    )


def _provider_defaults_script() -> str:
    defaults = {
        provider.id: {
            "baseUrl": provider.base_url,
            "helpText": provider.help_text,
            "reasoningSupported": provider.supports_reasoning_effort,
            "model": provider.model,
            "reasoning": provider.reasoning_effort or "",
            "jsonMode": provider.json_mode,
            "reasoningEfforts": sorted(provider.allowed_reasoning_efforts),
            "temperatureRequiresNoReasoning": provider.temperature_requires_no_reasoning,
            "temperatureSupported": provider.supports_temperature,
        }
        for provider in PROVIDER_PRESETS.values()
    }
    encoded_defaults = json.dumps(defaults).replace("<", "\\u003c")
    return f"""
    <script>
      const providerDefaults = {encoded_defaults};
            const providerSelect = document.getElementById("model-provider");
            const apiKeyInput = document.querySelector('[name="MODEL_API_KEY"]');
            const baseUrlInput = document.querySelector('[name="MODEL_BASE_URL"]');
            const modelInput = document.querySelector('[name="MODEL_NAME"]');
            const reasoningSelect = document.querySelector('[name="MODEL_REASONING_EFFORT"]');
            const reasoningField = document.getElementById("reasoning-field");
            const temperatureInput = document.querySelector('[name="MODEL_TEMPERATURE"]');
            const temperatureField = document.getElementById("temperature-field");
            const jsonModeInput = document.querySelector('[name="MODEL_JSON_MODE"]');
            const providerHelp = document.getElementById("provider-help");
            const advancedSettings = document.querySelector("details.advanced");

            function applyProvider(resetDefaults) {{
                const defaults = providerDefaults[providerSelect.value];
        if (!defaults) return;
                if (resetDefaults) {{
                    baseUrlInput.value = defaults.baseUrl;
                    modelInput.value = defaults.model;
                    reasoningSelect.value = defaults.reasoning;
                    temperatureInput.value = "0";
                    jsonModeInput.checked = defaults.jsonMode;
                    apiKeyInput.value = "";
                    apiKeyInput.placeholder = "Enter a key for the selected provider";
                }}

                providerHelp.textContent = defaults.helpText;
                baseUrlInput.required = providerSelect.value === "openai-compatible";
                reasoningField.hidden = !defaults.reasoningSupported;
                reasoningSelect.disabled = !defaults.reasoningSupported;
                for (const option of reasoningSelect.options) {{
                    const supported = option.value === ""
                        || defaults.reasoningEfforts.includes(option.value);
                    option.hidden = !supported;
                    option.disabled = !supported;
                }}
                if (!defaults.reasoningSupported
                    || !defaults.reasoningEfforts.includes(reasoningSelect.value)) {{
                    reasoningSelect.value = "";
                }}

                const reasoningDisabled = ["", "none"].includes(reasoningSelect.value);
                const showTemperature = defaults.temperatureSupported
                    && (!defaults.temperatureRequiresNoReasoning || reasoningDisabled);
                temperatureField.hidden = !showTemperature;
                temperatureInput.disabled = !showTemperature;
                if (!showTemperature) temperatureInput.value = "0";
                if (providerSelect.value === "openai-compatible" && resetDefaults) {{
                    advancedSettings.open = true;
                }}
            }}

            providerSelect.addEventListener("change", () => applyProvider(true));
            reasoningSelect.addEventListener("change", () => applyProvider(false));
            applyProvider(false);
    </script>
    """


def _checkbox(key: str, values: Mapping[str, str], label: str) -> str:
    checked = " checked" if values.get(key, "").lower() in {"1", "true", "yes", "on"} else ""
    return f"<label><input type='checkbox' name='{key}'{checked}> {escape(label)}</label>"