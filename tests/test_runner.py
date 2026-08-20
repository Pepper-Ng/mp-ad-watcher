from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from marktplaats_ad_watcher.config import Settings
from marktplaats_ad_watcher.models import (
    Ad,
    EvaluatedAd,
    EvaluationFailure,
    EvaluationResult,
    TelegramSendResult,
)
from marktplaats_ad_watcher.runner import Watcher
from marktplaats_ad_watcher.state import SeenStore
from marktplaats_ad_watcher.status import RuntimeStatusStore


class FakeMarktplaatsClient:
    def __init__(self, ads: list[Ad]) -> None:
        self._ads = ads
        self.enriched_ids: list[str] = []

    async def fetch_ads(self, search_url: str, *, limit: int) -> list[Ad]:
        del search_url
        return self._ads[:limit]

    async def enrich_ad(self, ad: Ad) -> Ad:
        self.enriched_ids.append(ad.id)
        return ad.model_copy(
            update={
                "description": "Full detail-page description.",
                "listing_facts": {"Capacity": "458 L"},
            }
        )


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

    async def send_ai_failure_alert(
        self,
        failures: list[EvaluationFailure],
    ) -> TelegramSendResult:
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
async def test_production_notification_payload_includes_profile_metadata(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        active_profile_id="freezers",
        active_profile_name="Freezers",
    )
    evaluator = RecordingEvaluator(
        EvaluationResult(
            relevant=True,
            confidence=0.91,
            reason="Matches criteria.",
            next_action="notify",
        )
    )

    class CapturingNotifier(FakeNotifier):
        def __init__(self) -> None:
            super().__init__()
            self.sent: list[EvaluatedAd] = []

        async def send(self, evaluated_ad: Any) -> TelegramSendResult:
            self.sent.append(evaluated_ad)
            return TelegramSendResult(sent=True, message_id=7)

    notifier = CapturingNotifier()
    watcher = Watcher(
        settings=settings,
        marktplaats_client=FakeMarktplaatsClient(
            [Ad(id="m321", title="Private freezer", url="https://example.test/m321")]
        ),
        evaluator=evaluator,
        notifier=notifier,
        store=SeenStore(settings.state_file),
    )

    summary = await watcher.run_once()

    assert summary.notified_count == 1
    assert len(notifier.sent) == 1
    assert notifier.sent[0].profile_id == "freezers"
    assert notifier.sent[0].profile_name == "Freezers"


@pytest.mark.asyncio
async def test_new_ads_are_enriched_before_model_evaluation(tmp_path: Path) -> None:
    class CapturingEvaluator(RecordingEvaluator):
        def __init__(self) -> None:
            super().__init__()
            self.evaluated_ads: list[Ad] = []

        async def evaluate(self, ad: Ad) -> EvaluationResult:
            self.evaluated_ads.append(ad)
            return await super().evaluate(ad)

    client = FakeMarktplaatsClient(
        [Ad(id="m123", title="Freezer chest", url="https://example.test/m123")]
    )
    evaluator = CapturingEvaluator()
    settings = _settings(tmp_path)
    watcher = Watcher(
        settings=settings,
        marktplaats_client=client,
        evaluator=evaluator,
        notifier=FakeNotifier(),
        store=SeenStore(settings.state_file),
    )

    await watcher.run_once()

    assert client.enriched_ids == ["m123"]
    assert evaluator.evaluated_ads[0].description == "Full detail-page description."
    assert evaluator.evaluated_ads[0].listing_facts == {"Capacity": "458 L"}


@pytest.mark.asyncio
async def test_detail_page_failure_leaves_ad_pending_without_model_evaluation(
    tmp_path: Path,
) -> None:
    class FailingDetailClient(FakeMarktplaatsClient):
        async def enrich_ad(self, ad: Ad) -> Ad:
            del ad
            raise RuntimeError("Marktplaats detail page is temporarily unavailable.")

    client = FailingDetailClient(
        [Ad(id="m123", title="Freezer chest", url="https://example.test/m123")]
    )
    evaluator = RecordingEvaluator()
    settings = _settings(tmp_path)
    store = SeenStore(settings.state_file)
    watcher = Watcher(
        settings=settings,
        marktplaats_client=client,
        evaluator=evaluator,
        notifier=FakeNotifier(),
        store=store,
    )

    summary = await watcher.run_once()

    assert summary.evaluation_failed_count == 1
    assert "detail page is temporarily unavailable" in summary.evaluation_failures[0].error
    assert evaluator.evaluated_ids == []
    assert not store.has_seen("m123")


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

    class FailingNotifier(FakeNotifier):
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
async def test_evaluation_failure_remains_pending_and_is_reported(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    class FailingEvaluator:
        async def evaluate(self, ad: Ad) -> EvaluationResult:
            del ad
            raise RuntimeError("Model provider returned HTTP 429 (free_rate_limited).")

    status_store = RuntimeStatusStore(settings.status_file)
    store = SeenStore(settings.state_file)
    watcher = Watcher(
        settings=settings,
        marktplaats_client=FakeMarktplaatsClient(
            [Ad(id="m-pending", title="Pending freezer", url="https://example.test/pending")]
        ),
        evaluator=FailingEvaluator(),
        notifier=FakeNotifier(),
        store=store,
        status_store=status_store,
    )

    summary = await watcher.run_once()

    assert summary.new_count == 1
    assert summary.evaluated_count == 0
    assert summary.evaluation_failed_count == 1
    assert summary.evaluation_failures[0].ad_id == "m-pending"
    assert "free_rate_limited" in summary.evaluation_failures[0].error
    assert not store.has_seen("m-pending")
    status = RuntimeStatusStore(settings.status_file).read()
    assert status.total_evaluation_failed == 1


@pytest.mark.asyncio
async def test_production_model_failure_sends_one_deduplicated_telegram_alert(
    tmp_path: Path,
) -> None:
    class FailingEvaluator:
        async def evaluate(self, ad: Ad) -> EvaluationResult:
            del ad
            raise RuntimeError("Model provider returned HTTP 503 (request id: changing-value).")

    settings = _settings(tmp_path)
    status_store = RuntimeStatusStore(settings.status_file)
    notifier = FakeNotifier()
    watcher = Watcher(
        settings=settings,
        marktplaats_client=FakeMarktplaatsClient(
            [Ad(id="m-pending", title="Pending freezer", url="https://example.test/pending")]
        ),
        evaluator=FailingEvaluator(),
        notifier=notifier,
        store=SeenStore(settings.state_file),
        status_store=status_store,
    )

    await watcher.run_once()
    await watcher.run_once()

    assert len(notifier.ai_failure_alerts) == 1
    assert notifier.ai_failure_alerts[0][0].ad_id == "m-pending"
    assert notifier.ai_failure_alerts[0][0].stage == "model"
    assert RuntimeStatusStore(settings.status_file).read().last_ai_failure_alert_at is not None


@pytest.mark.asyncio
async def test_disabled_or_non_model_failure_does_not_send_ai_failure_alert(tmp_path: Path) -> None:
    class FailingEvaluator:
        async def evaluate(self, ad: Ad) -> EvaluationResult:
            del ad
            raise RuntimeError("Provider unavailable")

    disabled_settings = _settings(tmp_path / "disabled", notify_ai_failures=False)
    disabled_notifier = FakeNotifier()
    disabled_watcher = Watcher(
        settings=disabled_settings,
        marktplaats_client=FakeMarktplaatsClient(
            [Ad(id="m1", title="Pending", url="https://example.test/m1")]
        ),
        evaluator=FailingEvaluator(),
        notifier=disabled_notifier,
        store=SeenStore(disabled_settings.state_file),
        status_store=RuntimeStatusStore(disabled_settings.status_file),
    )

    await disabled_watcher.run_once()

    assert disabled_notifier.ai_failure_alerts == []

    detail_settings = _settings(tmp_path / "detail")
    detail_notifier = FakeNotifier()

    class DetailFailureClient(FakeMarktplaatsClient):
        async def enrich_ad(self, ad: Ad) -> Ad:
            del ad
            raise RuntimeError("Listing page unavailable")

    detail_watcher = Watcher(
        settings=detail_settings,
        marktplaats_client=DetailFailureClient(
            [Ad(id="m2", title="Details fail", url="https://example.test/m2")]
        ),
        evaluator=RecordingEvaluator(),
        notifier=detail_notifier,
        store=SeenStore(detail_settings.state_file),
        status_store=RuntimeStatusStore(detail_settings.status_file),
    )

    await detail_watcher.run_once()

    assert detail_notifier.ai_failure_alerts == []