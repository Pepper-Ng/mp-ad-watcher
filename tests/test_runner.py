from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from marktplaats_ad_watcher.config import Settings
from marktplaats_ad_watcher.model_providers import ModelTransportError
from marktplaats_ad_watcher.models import (
    Ad,
    EvaluationFailure,
    EvaluationResult,
    TelegramSendResult,
)
from marktplaats_ad_watcher.runner import Watcher
from marktplaats_ad_watcher.state import SeenStore
from marktplaats_ad_watcher.status import RuntimeStatusStore
from marktplaats_ad_watcher.usage import ModelDailyLimitExceeded


class FakeMarktplaatsClient:
    def __init__(self, ads: list[Ad]) -> None:
        self._ads = ads

    async def fetch_ads(self, search_url: str, *, limit: int) -> list[Ad]:
        del search_url
        return self._ads[:limit]


class RecordingEvaluator:
    def __init__(self, result: EvaluationResult | None = None) -> None:
        self.evaluated_ids: list[str] = []
        self._result = result or EvaluationResult(
            relevant=False,
            confidence=0.4,
            reason="The ad does not meet the use case.",
            signals=[],
            concerns=["Not enough matching evidence."],
            next_action="ignore",
        )

    async def evaluate(self, ad: Ad) -> EvaluationResult:
        self.evaluated_ids.append(ad.id)
        return self._result


class FakeNotifier:
    def __init__(self) -> None:
        self.sent_ads: list[str] = []
        self.ai_failure_alerts: list[list[EvaluationFailure]] = []

    async def send(self, evaluated_ad: Any) -> TelegramSendResult:
        self.sent_ads.append(evaluated_ad.ad.id)
        return TelegramSendResult(sent=True, message_id=1)

    async def send_ai_failure_alert(self, failures: list[EvaluationFailure]) -> TelegramSendResult:
        self.ai_failure_alerts.append(failures)
        return TelegramSendResult(sent=True, message_id=2)


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    defaults = Settings(
        marktplaats_search_url="https://www.marktplaats.nl/lrp/api/search?query=vrieskist",
        marktplaats_use_case="Find useful freezer chests.",
        poll_interval_seconds=600,
        max_ads_per_poll=30,
        bootstrap_existing_ads=False,
        exclude_admarkt_ads=True,
        notify_min_confidence=0.65,
        review_min_confidence=0.0,
        notify_review_actions=True,
        model_provider="deepseek",
        model_api_key=None,
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
        state_file=tmp_path / "seen_ads.json",
        results_file=tmp_path / "evaluations.jsonl",
        status_file=tmp_path / "runtime_status.json",
        request_timeout_seconds=20,
        user_agent="test",
        web_admin_token=None,
    )
    return replace(defaults, **overrides)


@pytest.mark.asyncio
async def test_admarkt_ads_are_not_evaluated_when_filter_is_enabled(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    evaluator = RecordingEvaluator()
    notifier = FakeNotifier()
    watcher = Watcher(
        settings=settings,
        marktplaats_client=FakeMarktplaatsClient(
            [
                Ad(id="a123", title="Commercial freezer", url="https://example.test/a123"),
                Ad(id="m123", title="Private freezer", url="https://example.test/m123"),
            ]
        ),
        evaluator=evaluator,
        notifier=notifier,
        store=SeenStore(settings.state_file),
    )

    summary = await watcher.run_once()

    assert summary.fetched_count == 2
    assert summary.kept_count == 1
    assert summary.filtered_count == 1
    assert summary.new_count == 1
    assert evaluator.evaluated_ids == ["m123"]
    assert notifier.sent_ads == []


@pytest.mark.asyncio
async def test_review_actions_are_stored_and_sent_to_telegram(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    evaluator = RecordingEvaluator(
        EvaluationResult(
            relevant=False,
            confidence=0.42,
            reason="Looks plausible but dimensions are missing.",
            signals=["normal freezer chest"],
            concerns=["missing depth"],
            next_action="review",
        )
    )
    notifier = FakeNotifier()
    watcher = Watcher(
        settings=settings,
        marktplaats_client=FakeMarktplaatsClient(
            [Ad(id="m123", title="Private freezer", url="https://example.test/m123")]
        ),
        evaluator=evaluator,
        notifier=notifier,
        store=SeenStore(settings.state_file),
    )

    summary = await watcher.run_once()

    assert summary.evaluated_count == 1
    assert summary.review_count == 1
    assert summary.notified_count == 1
    assert notifier.sent_ads == ["m123"]

    reloaded = SeenStore(settings.state_file)
    assert reloaded.has_seen("m123")
    assert '"next_action":"review"' in settings.results_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_notification_failure_does_not_repeat_model_evaluation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    evaluator = RecordingEvaluator(
        EvaluationResult(
            relevant=True,
            confidence=0.9,
            reason="Matches the use case.",
            next_action="notify",
        )
    )

    class FailingNotifier:
        async def send(self, evaluated_ad: Any) -> TelegramSendResult:
            del evaluated_ad
            raise RuntimeError("Telegram unavailable")

    watcher = Watcher(
        settings=settings,
        marktplaats_client=FakeMarktplaatsClient(
            [Ad(id="m123", title="Private freezer", url="https://example.test/m123")]
        ),
        evaluator=evaluator,
        notifier=FailingNotifier(),
        store=SeenStore(settings.state_file),
    )

    first_summary = await watcher.run_once()
    second_summary = await watcher.run_once()

    assert first_summary.evaluated_count == 1
    assert first_summary.notified_count == 0
    assert second_summary.new_count == 0
    assert evaluator.evaluated_ids == ["m123"]


@pytest.mark.asyncio
async def test_budget_exhaustion_leaves_ad_pending_without_consuming_retries(
    tmp_path: Path,
) -> None:
    class BudgetLimitedEvaluator:
        async def evaluate(self, ad: Ad) -> EvaluationResult:
            del ad
            raise ModelDailyLimitExceeded(used=30, limit=30, reset_at=datetime.now(UTC))

    settings = _settings(tmp_path)
    store = SeenStore(settings.state_file)
    watcher = Watcher(
        settings=settings,
        marktplaats_client=FakeMarktplaatsClient(
            [Ad(id="m123", title="Pending freezer", url="https://example.test/m123")]
        ),
        evaluator=BudgetLimitedEvaluator(),
        notifier=FakeNotifier(),
        store=store,
        status_store=RuntimeStatusStore(settings.status_file),
    )

    first = await watcher.run_once()
    second = await watcher.run_once()

    assert first.evaluation_failed_count == 0
    assert second.evaluation_failed_count == 0
    assert not store.has_seen("m123")
    assert store.model_failure_attempts("m123") == 0


@pytest.mark.asyncio
async def test_model_failure_alert_is_sent_only_after_retries_are_exhausted(
    tmp_path: Path,
) -> None:
    class FailingEvaluator:
        async def evaluate(self, ad: Ad) -> EvaluationResult:
            del ad
            raise RuntimeError("Model provider returned HTTP 503.")

    settings = _settings(tmp_path, notify_ai_failures=True)
    notifier = FakeNotifier()
    watcher = Watcher(
        settings=settings,
        marktplaats_client=FakeMarktplaatsClient(
            [Ad(id="m123", title="Pending freezer", url="https://example.test/m123")]
        ),
        evaluator=FailingEvaluator(),
        notifier=notifier,
        store=SeenStore(settings.state_file),
        status_store=RuntimeStatusStore(settings.status_file),
    )

    first = await watcher.run_once()
    second = await watcher.run_once()

    assert first.evaluation_failures[0].retry_exhausted is False
    assert second.evaluation_failures[0].retry_exhausted is False
    assert notifier.ai_failure_alerts == []

    third = await watcher.run_once()

    assert len(notifier.ai_failure_alerts) == 1
    assert third.evaluation_failures[0].retry_exhausted is True


@pytest.mark.asyncio
async def test_transient_transport_failures_do_not_send_ai_failure_alerts(
    tmp_path: Path,
) -> None:
    class TransportFailingEvaluator:
        async def evaluate(self, ad: Ad) -> EvaluationResult:
            del ad
            raise ModelTransportError("ReadTimeout while calling router.example.test")

    settings = _settings(tmp_path, notify_ai_failures=True)
    notifier = FakeNotifier()
    watcher = Watcher(
        settings=settings,
        marktplaats_client=FakeMarktplaatsClient(
            [Ad(id="m123", title="Pending freezer", url="https://example.test/m123")]
        ),
        evaluator=TransportFailingEvaluator(),
        notifier=notifier,
        store=SeenStore(settings.state_file),
        status_store=RuntimeStatusStore(settings.status_file),
    )

    for _ in range(3):
        summary = await watcher.run_once()
        assert summary.evaluation_failures[0].retry_exhausted is False

    assert notifier.ai_failure_alerts == []