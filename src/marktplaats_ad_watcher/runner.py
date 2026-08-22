from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, Field

from marktplaats_ad_watcher.config import Settings
from marktplaats_ad_watcher.evaluation import Evaluator
from marktplaats_ad_watcher.models import (
    Ad,
    EvaluatedAd,
    EvaluationFailure,
    EvaluationResult,
    TelegramSendResult,
    WatcherRunSummary,
)
from marktplaats_ad_watcher.profiles import (
    ProfileRegistry,
    SearchProfile,
    ensure_profile_registry,
)
from marktplaats_ad_watcher.state import SeenStore
from marktplaats_ad_watcher.status import RuntimeStatusStore

LOGGER = logging.getLogger(__name__)
MAX_MODEL_RETRIES = 2


class AdFetcher(Protocol):
    async def fetch_ads(self, search_url: str, /, *, limit: int) -> list[Ad]: ...

    async def enrich_ad(self, ad: Ad, /) -> Ad: ...


class Notifier(Protocol):
    async def send(self, evaluated_ad: EvaluatedAd, /) -> TelegramSendResult: ...


class RunnableWatcher(Protocol):
    async def run_once(self) -> WatcherRunSummary: ...


class ProfileRunResult(BaseModel):
    """Observable outcome for one profile without allowing one failure to stop others."""

    profile_id: str
    profile_name: str
    summary: WatcherRunSummary | None = None
    error: str | None = None
    skipped_reason: str | None = None
    next_run_at: datetime | None = None


class ProfileExecutionSummary(BaseModel):
    """The ordered outcomes of one selected or multi-profile execution pass."""

    profiles: list[ProfileRunResult] = Field(default_factory=list)

    @property
    def failures(self) -> list[ProfileRunResult]:
        return [result for result in self.profiles if result.error is not None]


WatcherBuilder = Callable[[Settings, RuntimeStatusStore], RunnableWatcher]


class ProfileOrchestrator:
    """Sequentially run independently persisted profiles with isolated failure handling."""

    def __init__(self, *, settings: Settings, watcher_builder: WatcherBuilder) -> None:
        self._settings = settings
        self._watcher_builder = watcher_builder

    async def run_profile(
        self,
        profile_id: str | None = None,
        *,
        due_only: bool = False,
    ) -> ProfileExecutionSummary:
        registry = self._activate_registry()
        profile = (
            registry.default_profile
            if profile_id is None
            else registry.profile(profile_id)
        )
        return ProfileExecutionSummary(
            profiles=[await self._run_profile(profile, due_only=due_only)]
        )

    async def run_all_enabled(
        self,
        *,
        due_only: bool = False,
    ) -> ProfileExecutionSummary:
        registry = self._activate_registry()
        outcomes: list[ProfileRunResult] = []
        for profile in sorted(registry.profiles, key=lambda item: item.sort_order):
            outcomes.append(await self._run_profile(profile, due_only=due_only))
        return ProfileExecutionSummary(profiles=outcomes)

    async def run_loop(
        self,
        *,
        profile_id: str | None = None,
        all_profiles: bool = False,
    ) -> None:
        while True:
            if all_profiles:
                execution = await self.run_all_enabled(due_only=True)
            else:
                execution = await self.run_profile(profile_id, due_only=True)
            LOGGER.info("Profile execution summary: %s", execution.model_dump())
            await asyncio.sleep(self._seconds_until_next_run(execution))

    def _activate_registry(self) -> ProfileRegistry:
        return ensure_profile_registry(self._settings).registry

    async def _run_profile(self, profile: SearchProfile, *, due_only: bool) -> ProfileRunResult:
        if not profile.enabled:
            return ProfileRunResult(
                profile_id=profile.id,
                profile_name=profile.name,
                skipped_reason="disabled",
            )

        profile_settings = self._settings.for_profile(profile)
        status_store = RuntimeStatusStore(profile_settings.status_file)
        if due_only and not _is_due(status_store.read().next_run_at):
            return ProfileRunResult(
                profile_id=profile.id,
                profile_name=profile.name,
                skipped_reason="not_due",
                next_run_at=status_store.read().next_run_at,
            )

        next_run_at = datetime.now(UTC) + timedelta(
            seconds=profile_settings.poll_interval_seconds
        )
        watcher: RunnableWatcher | None = None
        try:
            watcher = self._watcher_builder(profile_settings, status_store)
            summary = await watcher.run_once()
        except Exception as error:
            LOGGER.exception("Profile %s (%s) failed.", profile.name, profile.id)
            if watcher is None:
                status_store.mark_failed(error)
            return ProfileRunResult(
                profile_id=profile.id,
                profile_name=profile.name,
                error=_profile_error_message(error),
                next_run_at=_set_next_run_at(status_store, next_run_at),
            )

        return ProfileRunResult(
            profile_id=profile.id,
            profile_name=profile.name,
            summary=summary,
            next_run_at=_set_next_run_at(status_store, next_run_at),
        )

    def _seconds_until_next_run(self, execution: ProfileExecutionSummary) -> float:
        scheduled = [
            result.next_run_at
            for result in execution.profiles
            if result.next_run_at is not None
        ]
        if not scheduled:
            return float(self._settings.poll_interval_seconds)
        return max(0.0, (min(scheduled) - datetime.now(UTC)).total_seconds())


class Watcher:
    def __init__(
        self,
        *,
        settings: Settings,
        marktplaats_client: AdFetcher,
        evaluator: Evaluator,
        notifier: Notifier,
        store: SeenStore,
        status_store: RuntimeStatusStore | None = None,
    ) -> None:
        self._settings = settings
        self._marktplaats_client = marktplaats_client
        self._evaluator = evaluator
        self._notifier = notifier
        self._store = store
        self._status_store = status_store
        self._profile_log_context = _watcher_profile_log_context(settings)

    async def run_once(self) -> WatcherRunSummary:
        if self._status_store is not None:
            self._status_store.mark_started()

        try:
            summary = await self._run_once()
        except Exception as error:
            if self._status_store is not None:
                self._status_store.mark_failed(error)
            raise

        if self._status_store is not None:
            self._status_store.mark_finished(summary)

        await self._notify_production_ai_failures(summary)

        return summary

    async def _run_once(self) -> WatcherRunSummary:
        fetched_ads = await self._marktplaats_client.fetch_ads(
            self._settings.marktplaats_search_url,
            limit=self._settings.max_ads_per_poll,
        )
        LOGGER.info(
            "%sFetched %s ads from Marktplaats.",
            self._profile_log_context,
            len(fetched_ads),
        )

        ads = _filter_ads(fetched_ads, exclude_admarkt_ads=self._settings.exclude_admarkt_ads)
        filtered_count = len(fetched_ads) - len(ads)
        LOGGER.info(
            "%sKept %s ads after local filters.",
            self._profile_log_context,
            len(ads),
        )

        if (
            self._store.is_empty
            and self._settings.bootstrap_existing_ads
            and not self._settings.dry_run
        ):
            self._store.mark_many_seen(ads)
            LOGGER.info(
                "%sBootstrapped %s existing ads as already seen.",
                self._profile_log_context,
                len(ads),
            )
            return WatcherRunSummary(
                fetched_count=len(fetched_ads),
                kept_count=len(ads),
                filtered_count=filtered_count,
                new_count=0,
                evaluated_count=0,
                notified_count=0,
                bootstrapped_count=len(ads),
            )

        new_ads = [ad for ad in ads if not self._store.has_seen(ad.id)]
        LOGGER.info("%sFound %s new ads.", self._profile_log_context, len(new_ads))

        evaluated_count = 0
        notified_count = 0
        ignored_count = 0
        review_count = 0
        notify_action_count = 0
        evaluation_failures: list[EvaluationFailure] = []

        for ad in new_ads:
            try:
                enriched_ad = await self._enrich_ad(ad)
            except Exception as error:
                LOGGER.exception(
                    "%sFailed to load full listing details for ad %s (%s).",
                    self._profile_log_context,
                    ad.id,
                    ad.title,
                )
                evaluation_failures.append(
                    EvaluationFailure(
                        ad_id=ad.id,
                        title=ad.title,
                        url=ad.url,
                        error=_evaluation_failure_message(error),
                        stage="listing_details",
                    )
                )
                continue

            try:
                result = await self._evaluator.evaluate(enriched_ad)
            except Exception as error:
                failure_message = _evaluation_failure_message(error)
                attempt_number = self._store.model_failure_attempts(enriched_ad.id) + 1
                LOGGER.warning(
                    (
                        "%sModel evaluation failed for ad %s (%s), attempt %s of %s."
                    ),
                    self._profile_log_context,
                    enriched_ad.id,
                    enriched_ad.title,
                    attempt_number,
                    MAX_MODEL_RETRIES + 1,
                )
                LOGGER.exception(
                    "%sFailed to evaluate ad %s (%s).",
                    self._profile_log_context,
                    enriched_ad.id,
                    enriched_ad.title,
                )
                evaluation_failures.append(
                    EvaluationFailure(
                        ad_id=enriched_ad.id,
                        title=enriched_ad.title,
                        url=enriched_ad.url,
                        error=failure_message,
                    )
                )

                if not self._settings.dry_run:
                    failed_attempts, exhausted = self._store.mark_model_failure(
                        enriched_ad,
                        error=failure_message,
                        max_retries=MAX_MODEL_RETRIES,
                    )
                    if exhausted:
                        LOGGER.warning(
                            (
                                "%sGiving up on ad %s after %s failed model attempts; "
                                "it will not be retried again."
                            ),
                            self._profile_log_context,
                            enriched_ad.id,
                            failed_attempts,
                        )
                continue

            evaluated_ad = EvaluatedAd(
                ad=enriched_ad,
                result=result,
                profile_id=self._settings.active_profile_id,
                profile_name=self._settings.active_profile_name,
            )
            evaluated_count += 1
            if result.next_action == "ignore":
                ignored_count += 1
            elif result.next_action == "review":
                review_count += 1
            elif result.next_action == "notify":
                notify_action_count += 1
            LOGGER.info(
                "%sEvaluation for %s: relevant=%s confidence=%.2f action=%s",
                self._profile_log_context,
                ad.id,
                result.relevant,
                result.confidence,
                result.next_action,
            )

            if not self._settings.dry_run:
                self._store.append_result(self._settings.results_file, evaluated_ad)

            if _should_notify(
                result,
                notify_minimum_confidence=self._settings.notify_min_confidence,
                review_minimum_confidence=self._settings.review_min_confidence,
                notify_review_actions=self._settings.notify_review_actions,
            ):
                if self._settings.dry_run:
                    LOGGER.info(
                        "%sDry run: would notify for ad %s.",
                        self._profile_log_context,
                        ad.id,
                    )
                else:
                    try:
                        send_result = await self._notifier.send(evaluated_ad)
                    except Exception:
                        LOGGER.exception(
                            (
                                "%sTelegram notification failed for ad %s; "
                                "the evaluation was retained."
                            ),
                            self._profile_log_context,
                            ad.id,
                        )
                    else:
                        if send_result.sent:
                            notified_count += 1
                            LOGGER.info(
                                "%sSent Telegram notification for ad %s.",
                                self._profile_log_context,
                                ad.id,
                            )
                        else:
                            LOGGER.info(
                                "%sSkipped Telegram notification for ad %s: %s.",
                                self._profile_log_context,
                                ad.id,
                                send_result.reason,
                            )

            if not self._settings.dry_run:
                self._store.mark_seen(enriched_ad, result)

        return WatcherRunSummary(
            fetched_count=len(fetched_ads),
            kept_count=len(ads),
            filtered_count=filtered_count,
            new_count=len(new_ads),
            evaluated_count=evaluated_count,
            notified_count=notified_count,
            ignored_count=ignored_count,
            review_count=review_count,
            notify_action_count=notify_action_count,
            evaluation_failed_count=len(evaluation_failures),
            evaluation_failures=evaluation_failures,
        )

    async def _enrich_ad(self, ad: Ad) -> Ad:
        return await self._marktplaats_client.enrich_ad(ad)

    async def _notify_production_ai_failures(self, summary: WatcherRunSummary) -> None:
        if (
            self._settings.dry_run
            or not self._settings.notify_ai_failures
            or self._status_store is None
            or not self._status_store.should_send_ai_failure_alert(summary)
        ):
            return

        failures = [failure for failure in summary.evaluation_failures if failure.stage == "model"]
        sender = getattr(self._notifier, "send_ai_failure_alert", None)
        if not callable(sender):
            LOGGER.warning(
                "%sNotifier does not support production AI-failure alerts.",
                self._profile_log_context,
            )
            return

        try:
            send_result = await sender(failures)
        except Exception:
            LOGGER.exception(
                "%sTelegram AI-failure alert could not be sent; it will retry on the next run.",
                self._profile_log_context,
            )
            return

        if send_result.sent:
            self._status_store.mark_ai_failure_alert_sent(summary)
            LOGGER.info(
                "%sSent Telegram AI-failure alert for %s pending listing(s).",
                self._profile_log_context,
                len(failures),
            )
        else:
            LOGGER.info(
                "%sSkipped Telegram AI-failure alert: %s.",
                self._profile_log_context,
                send_result.reason,
            )

    async def run_loop(self) -> None:
        while True:
            try:
                summary = await self.run_once()
                LOGGER.info(
                    "%sRun summary: %s",
                    self._profile_log_context,
                    summary.model_dump(),
                )
            except Exception:
                LOGGER.exception("%sWatcher run failed.", self._profile_log_context)

            await asyncio.sleep(self._settings.poll_interval_seconds)


def _should_notify(
    result: EvaluationResult,
    *,
    notify_minimum_confidence: float,
    review_minimum_confidence: float,
    notify_review_actions: bool,
) -> bool:
    if result.next_action == "notify":
        return result.relevant and result.confidence >= notify_minimum_confidence

    if result.next_action == "review":
        return notify_review_actions and result.confidence >= review_minimum_confidence

    return False


def _filter_ads(ads: list[Ad], *, exclude_admarkt_ads: bool) -> list[Ad]:
    if not exclude_admarkt_ads:
        return ads

    return [ad for ad in ads if not ad.id.lower().startswith("a")]


def _evaluation_failure_message(error: Exception) -> str:
    message = str(error).strip() or "No error details were supplied."
    return f"{type(error).__name__}: {message}"[:600]


def _is_due(next_run_at: datetime | None) -> bool:
    return next_run_at is None or next_run_at <= datetime.now(UTC)


def _set_next_run_at(status_store: RuntimeStatusStore, value: datetime) -> datetime:
    status_store.set_next_run_at(value)
    return value


def _profile_error_message(error: Exception) -> str:
    message = str(error).strip() or "No error details were supplied."
    return f"{type(error).__name__}: {message}"[:600]


def _watcher_profile_log_context(settings: Settings) -> str:
    profile_name = settings.active_profile_name
    profile_id = settings.active_profile_id
    if profile_name and profile_id:
        return f"[{profile_name} · {profile_id}] "
    if profile_name:
        return f"[{profile_name}] "
    if profile_id:
        return f"[{profile_id}] "
    return ""
