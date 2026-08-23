from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol, cast

from marktplaats_ad_watcher.config import Settings
from marktplaats_ad_watcher.evaluation import Evaluator
from marktplaats_ad_watcher.model_providers import ModelEvaluationError
from marktplaats_ad_watcher.models import (
    Ad,
    EvaluatedAd,
    EvaluationFailure,
    EvaluationResult,
    TelegramSendResult,
    WatcherRunSummary,
)
from marktplaats_ad_watcher.state import SeenStore
from marktplaats_ad_watcher.status import RuntimeStatusStore
from marktplaats_ad_watcher.usage import ModelDailyLimitExceeded

LOGGER = logging.getLogger(__name__)
MAX_MODEL_RETRIES = 2


class AdFetcher(Protocol):
    async def fetch_ads(self, search_url: str, /, *, limit: int) -> list[Ad]: ...


class Notifier(Protocol):
    async def send(self, evaluated_ad: EvaluatedAd, /) -> TelegramSendResult: ...


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
        LOGGER.info("Fetched %s ads from Marktplaats.", len(fetched_ads))

        ads = _filter_ads(fetched_ads, exclude_admarkt_ads=self._settings.exclude_admarkt_ads)
        filtered_count = len(fetched_ads) - len(ads)
        LOGGER.info("Kept %s ads after local filters.", len(ads))

        if (
            self._store.is_empty
            and self._settings.bootstrap_existing_ads
            and not self._settings.dry_run
        ):
            self._store.mark_many_seen(ads)
            LOGGER.info("Bootstrapped %s existing ads as already seen.", len(ads))
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
        LOGGER.info("Found %s new ads.", len(new_ads))

        evaluated_count = 0
        notified_count = 0
        ignored_count = 0
        review_count = 0
        notify_action_count = 0
        evaluation_failures: list[EvaluationFailure] = []

        for ad in new_ads:
            try:
                result = await self._evaluator.evaluate(ad)
            except ModelDailyLimitExceeded as error:
                LOGGER.info(
                    (
                        "Model budget is currently exhausted; leaving ad %s (%s) "
                        "pending without consuming retries."
                    ),
                    ad.id,
                    ad.title,
                )
                LOGGER.debug("Budget exhaustion details for ad %s: %s", ad.id, error)
                continue
            except Exception as error:
                LOGGER.exception("Failed to evaluate ad %s (%s).", ad.id, ad.title)
                failure_message = _evaluation_failure_message(error)
                evaluation_failures.append(
                    EvaluationFailure(
                        ad_id=ad.id,
                        title=ad.title,
                        url=ad.url,
                        error=failure_message,
                    )
                )
                if not self._settings.dry_run and _failure_consumes_retry(error):
                    failed_attempts, exhausted = self._store.mark_model_failure(
                        ad,
                        error=failure_message,
                        max_retries=MAX_MODEL_RETRIES,
                    )
                    if exhausted:
                        LOGGER.warning(
                            (
                                "Giving up on ad %s after %s failed model attempts; "
                                "it will not be retried again."
                            ),
                            ad.id,
                            failed_attempts,
                        )
                continue

            evaluated_ad = EvaluatedAd(ad=ad, result=result)
            evaluated_count += 1
            if result.next_action == "ignore":
                ignored_count += 1
            elif result.next_action == "review":
                review_count += 1
            elif result.next_action == "notify":
                notify_action_count += 1
            LOGGER.info(
                "Evaluation for %s: relevant=%s confidence=%.2f action=%s",
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
                    LOGGER.info("Dry run: would notify for ad %s.", ad.id)
                else:
                    try:
                        send_result = await self._notifier.send(evaluated_ad)
                    except Exception:
                        LOGGER.exception(
                            "Telegram notification failed for ad %s; the evaluation was retained.",
                            ad.id,
                        )
                    else:
                        if send_result.sent:
                            notified_count += 1
                            LOGGER.info("Sent Telegram notification for ad %s.", ad.id)
                        else:
                            LOGGER.info(
                                "Skipped Telegram notification for ad %s: %s.",
                                ad.id,
                                send_result.reason,
                            )

            if not self._settings.dry_run:
                self._store.mark_seen(ad, result)

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

    async def _notify_production_ai_failures(self, summary: WatcherRunSummary) -> None:
        if (
            self._settings.dry_run
            or not self._settings.notify_ai_failures
            or self._status_store is None
            or not self._status_store.should_send_ai_failure_alert(summary)
        ):
            return

        sender = getattr(self._notifier, "send_ai_failure_alert", None)
        if not callable(sender):
            return
        send_alert = cast(
            Callable[[list[EvaluationFailure]], Awaitable[TelegramSendResult]],
            sender,
        )

        try:
            send_result = await send_alert(summary.evaluation_failures)
        except Exception:
            LOGGER.exception("Telegram AI-failure alert could not be sent.")
            return

        if send_result.sent:
            self._status_store.mark_ai_failure_alert_sent(summary)
            LOGGER.info(
                "Sent Telegram AI-failure alert for %s pending listing(s).",
                len(summary.evaluation_failures),
            )

    async def run_loop(self) -> None:
        while True:
            try:
                summary = await self.run_once()
                LOGGER.info("Run summary: %s", summary.model_dump())
            except Exception:
                LOGGER.exception("Watcher run failed.")

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


def _failure_consumes_retry(error: Exception) -> bool:
    if isinstance(error, ModelDailyLimitExceeded):
        return False
    if isinstance(error, ModelEvaluationError):
        return bool(getattr(error, "attempt_consumed", True))
    return True
