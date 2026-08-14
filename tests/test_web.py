from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from marktplaats_ad_watcher.config import Settings, parse_dotenv, write_dotenv
from marktplaats_ad_watcher.models import (
    Ad,
    EvaluatedAd,
    EvaluationFailure,
    EvaluationResult,
    TelegramSendResult,
    WatcherRunSummary,
)
from marktplaats_ad_watcher.pipeline_progress import PipelineProgressStore
from marktplaats_ad_watcher.profiles import (
    LEGACY_MIGRATION_NAME,
    migrate_legacy_single_search,
)
from marktplaats_ad_watcher.state import SeenStore
from marktplaats_ad_watcher.status import RuntimeStatus, RuntimeStatusStore
from marktplaats_ad_watcher.usage import ModelUsageStore
from marktplaats_ad_watcher.web import (
    ERROR_RETRY_SECONDS,
    RecentLogBuffer,
    WatcherService,
    create_web_app,
)


@pytest.mark.asyncio
async def test_service_loop_retries_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "settings.env"
    write_dotenv(
        env_file,
        {
            "MARKTPLAATS_SEARCH_URL": "https://www.marktplaats.nl/lrp/api/search?query=test",
            "MARKTPLAATS_USE_CASE": "Find relevant listings.",
            "STATUS_FILE": str(tmp_path / "status.json"),
        },
    )
    service = WatcherService(env_file=env_file, dry_run=True)
    service._stop_event = asyncio.Event()
    run_once = AsyncMock(side_effect=[RuntimeError("temporary failure"), None])
    monkeypatch.setattr(service, "run_once", run_once)

    delays: list[float] = []

    async def fake_wait_for(awaitable: object, timeout: float) -> None:
        delays.append(timeout)
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()
        if len(delays) == 1:
            raise TimeoutError
        assert service._stop_event is not None
        service._stop_event.set()

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    await service._run_forever()

    assert run_once.await_count == 2
    assert delays == [ERROR_RETRY_SECONDS, 600]


@pytest.mark.asyncio
async def test_configuration_error_is_recorded_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_file = tmp_path / "status.json"
    env_file = tmp_path / "settings.env"
    write_dotenv(env_file, {"STATUS_FILE": str(status_file)})
    monkeypatch.delenv("MARKTPLAATS_SEARCH_URL", raising=False)
    monkeypatch.delenv("MARKTPLAATS_USE_CASE", raising=False)
    service = WatcherService(env_file=env_file, dry_run=True)

    with pytest.raises(ValueError, match="MARKTPLAATS_SEARCH_URL"):
        await service.run_once()

    status = service.status_store().read()
    assert status.total_errors == 1
    assert status.last_error is not None
    assert "MARKTPLAATS_SEARCH_URL" in status.last_error


@pytest.mark.asyncio
async def test_service_shutdown_drains_queued_manual_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WatcherService(env_file=tmp_path / "settings.env", dry_run=True)
    release_run = asyncio.Event()
    run_started = asyncio.Event()

    async def delayed_run() -> None:
        run_started.set()
        await release_run.wait()

    monkeypatch.setattr(service, "run_once", delayed_run)

    assert service.queue_run_once()
    assert not service.queue_run_once()
    await run_started.wait()

    stop_task = asyncio.create_task(service.stop())
    await asyncio.sleep(0)
    assert not stop_task.done()
    assert not service.queue_run_once()

    release_run.set()
    await stop_task
    assert not service._manual_tasks


def test_read_config_migrates_legacy_deepseek_fields_for_the_ui(tmp_path: Path) -> None:
    env_file = tmp_path / "settings.env"
    write_dotenv(
        env_file,
        {
            "DEEPSEEK_API_KEY": "legacy-key",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
            "DEEPSEEK_MODEL": "deepseek-v4-flash",
        },
    )

    values = WatcherService(env_file=env_file, dry_run=True).read_config()

    assert values["MODEL_PROVIDER"] == "deepseek"
    assert values["MODEL_API_KEY"] == "legacy-key"
    assert values["MODEL_NAME"] == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_unchanged_deepseek_provider_migrates_and_preserves_legacy_key(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "settings.env"
    write_dotenv(env_file, {"DEEPSEEK_API_KEY": "legacy-key"})
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/config",
            data={
                "MODEL_PROVIDER": "deepseek",
                "MODEL_API_KEY": "",
                "MODEL_BASE_URL": "https://api.deepseek.com/v1",
                "MODEL_NAME": "deepseek-v4-flash",
                "MODEL_MAX_TOKENS": "700",
                "MODEL_TEMPERATURE": "0",
                "MODEL_JSON_MODE": "true",
            },
        )

    saved = parse_dotenv(env_file)
    assert saved["MODEL_API_KEY"] == "legacy-key"
    assert "DEEPSEEK_API_KEY" not in saved


@pytest.mark.asyncio
async def test_provider_ui_and_switch_clear_the_previous_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "settings.env"
    write_dotenv(env_file, {"DEEPSEEK_API_KEY": "legacy-key"})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "stale-process-key")
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)
    required = {
        "MARKTPLAATS_SEARCH_URL": "https://www.marktplaats.nl/lrp/api/search?query=test",
        "MARKTPLAATS_USE_CASE": "Find relevant listings.",
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/config")
        response = await client.post(
            "/config",
            data={
                **required,
                "MODEL_PROVIDER": "openai",
                "MODEL_API_KEY": "",
                "MODEL_BASE_URL": "https://api.openai.com/v1",
                "MODEL_NAME": "gpt-5.6-luna",
                "MODEL_REASONING_EFFORT": "medium",
                "MODEL_MAX_TOKENS": "700",
                "MODEL_TEMPERATURE": "0",
                "MODEL_JSON_MODE": "true",
            },
        )

    assert page.status_code == 200
    assert "Custom OpenAI-compatible API" in page.text
    assert "Anthropic Claude" in page.text
    assert "Watch criteria" in page.text
    assert "Schedule and filtering" in page.text
    assert "Decision policy" in page.text
    assert "Advanced model settings" in page.text
    assert "Runtime settings" in page.text
    assert "reasoningSupported" in page.text
    assert response.status_code == 303
    saved = parse_dotenv(env_file)
    assert saved["MODEL_PROVIDER"] == "openai"
    assert saved["MODEL_API_KEY"] == ""
    assert "DEEPSEEK_API_KEY" not in saved

    service = WatcherService(env_file=env_file, dry_run=True)
    assert service._load_settings().model_api_key is None

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/config",
            data={
                **required,
                "MODEL_PROVIDER": "deepseek",
                "MODEL_API_KEY": "",
                "MODEL_BASE_URL": "https://api.deepseek.com/v1",
                "MODEL_NAME": "deepseek-v4-flash",
                "MODEL_MAX_TOKENS": "700",
                "MODEL_TEMPERATURE": "0",
                "MODEL_JSON_MODE": "true",
            },
        )

    assert service._load_settings().model_api_key is None


@pytest.mark.asyncio
async def test_web_does_not_import_or_persist_process_model_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "settings.env"
    write_dotenv(
        env_file,
        {
            "MARKTPLAATS_SEARCH_URL": "https://www.marktplaats.nl/lrp/api/search?query=test",
            "MARKTPLAATS_USE_CASE": "Find relevant listings.",
            "MODEL_PROVIDER": "deepseek",
        },
    )
    monkeypatch.setenv("MODEL_API_KEY", "stale-generic-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "stale-legacy-key")
    service = WatcherService(env_file=env_file, dry_run=True)

    assert service.read_config().get("MODEL_API_KEY", "") == ""
    assert service._load_settings().model_api_key is None

    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/config",
            data={
                "MARKTPLAATS_SEARCH_URL": "https://www.marktplaats.nl/lrp/api/search?query=test",
                "MARKTPLAATS_USE_CASE": "Find relevant listings.",
                "MODEL_PROVIDER": "deepseek",
                "MODEL_API_KEY": "",
                "MODEL_BASE_URL": "https://api.deepseek.com/v1",
                "MODEL_NAME": "deepseek-v4-flash",
                "MODEL_MAX_TOKENS": "700",
                "MODEL_TEMPERATURE": "0",
                "MODEL_JSON_MODE": "true",
            },
        )

    assert "MODEL_API_KEY" not in parse_dotenv(env_file)


@pytest.mark.asyncio
async def test_provider_comparison_is_case_insensitive_when_preserving_key(tmp_path: Path) -> None:
    env_file = tmp_path / "settings.env"
    write_dotenv(
        env_file,
        {
            "MODEL_PROVIDER": "OpenAI",
            "MODEL_API_KEY": "openai-key",
        },
    )
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/config",
            data={
                "MODEL_PROVIDER": "openai",
                "MODEL_API_KEY": "",
                "MODEL_BASE_URL": "https://api.openai.com/v1",
                "MODEL_NAME": "gpt-5.6-luna",
                "MODEL_REASONING_EFFORT": "medium",
                "MODEL_MAX_TOKENS": "700",
                "MODEL_TEMPERATURE": "0",
                "MODEL_JSON_MODE": "true",
            },
        )

    saved = parse_dotenv(env_file)
    assert saved["MODEL_PROVIDER"] == "openai"
    assert saved["MODEL_API_KEY"] == "openai-key"


@pytest.mark.asyncio
async def test_environment_admin_token_authenticates_without_being_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "settings.env"
    monkeypatch.setenv("WEB_ADMIN_TOKEN", "portainer-secret")
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/config")
        allowed = await client.get("/config?token=portainer-secret")
        saved_response = await client.post(
            "/config?token=portainer-secret",
            data={"WEB_ADMIN_TOKEN": ""},
        )
        allowed_after_save = await client.get("/config?token=portainer-secret")

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert saved_response.status_code == 303
    assert allowed_after_save.status_code == 200
    assert "WEB_ADMIN_TOKEN" not in parse_dotenv(env_file)


@pytest.mark.asyncio
async def test_evaluations_page_filters_and_downloads_json(tmp_path: Path) -> None:
    env_file = tmp_path / "settings.env"
    results_file = tmp_path / "evaluations.jsonl"
    write_dotenv(
        env_file,
        {
            "WEB_ADMIN_TOKEN": "admin-token",
            "RESULTS_FILE": str(results_file),
        },
    )
    store = SeenStore(tmp_path / "seen_ads.json")
    store.append_result(
        results_file,
        EvaluatedAd(
            ad=Ad(
                id="m1",
                title="Suitable freezer chest",
                url="https://www.marktplaats.nl/v/m1",
                price="EUR 125.00",
                location="Eindhoven",
            ),
            result=EvaluationResult(
                relevant=True,
                confidence=0.9,
                reason="Size matches.",
                signals=["200 litre capacity"],
                concerns=["Confirm pickup."],
                next_action="notify",
            ),
        ),
    )
    store.append_result(
        results_file,
        EvaluatedAd(
            ad=Ad(id="m2", title="Too small", url="https://www.marktplaats.nl/v/m2"),
            result=EvaluationResult(
                relevant=False,
                confidence=0.95,
                reason="Capacity is too small.",
                next_action="ignore",
            ),
        ),
    )
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/evaluations")
        filtered_page = await client.get("/evaluations?token=admin-token&action=notify")
        downloaded = await client.get("/api/evaluations?token=admin-token&action=notify")

    assert denied.status_code == 401
    assert filtered_page.status_code == 200
    assert "Suitable freezer chest" in filtered_page.text
    assert "Size matches." in filtered_page.text
    assert "200 litre capacity" in filtered_page.text
    assert "Too small" not in filtered_page.text
    assert downloaded.status_code == 200
    assert downloaded.json()[0]["result"]["next_action"] == "notify"
    assert len(downloaded.json()) == 1


@pytest.mark.asyncio
async def test_dashboard_formats_last_summary_as_pipeline(tmp_path: Path) -> None:
    env_file = tmp_path / "settings.env"
    status_file = tmp_path / "status.json"
    write_dotenv(
        env_file,
        {
            "WEB_ADMIN_TOKEN": "admin-token",
            "STATUS_FILE": str(status_file),
        },
    )
    status = RuntimeStatus(
        last_finished_at=datetime.now(UTC),
        last_summary=WatcherRunSummary(
            fetched_count=17,
            kept_count=12,
            filtered_count=5,
            new_count=3,
            evaluated_count=3,
            notified_count=1,
            ignored_count=1,
            review_count=1,
            notify_action_count=1,
        ),
    )
    status_file.write_text(status.model_dump_json(indent=2), encoding="utf-8")
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/?token=admin-token")

    assert page.status_code == 200
    assert "17</strong> fetched" in page.text
    assert "12</strong> eligible" in page.text
    assert "3</strong> evaluated" in page.text
    assert "last_summary" not in page.text
    assert "Pipeline tools" in page.text


@pytest.mark.asyncio
async def test_dashboard_explains_failed_ai_attempt(tmp_path: Path) -> None:
    env_file = tmp_path / "settings.env"
    status_file = tmp_path / "status.json"
    write_dotenv(
        env_file,
        {"WEB_ADMIN_TOKEN": "admin-token", "STATUS_FILE": str(status_file)},
    )
    status = RuntimeStatus(
        last_finished_at=datetime.now(UTC),
        last_summary=WatcherRunSummary(
            fetched_count=1,
            kept_count=1,
            filtered_count=0,
            new_count=1,
            evaluated_count=0,
            notified_count=0,
            evaluation_failed_count=1,
            evaluation_failures=[
                EvaluationFailure(
                    ad_id="m1",
                    title="Pending freezer",
                    url="https://www.marktplaats.nl/v/m1",
                    error="ModelProviderError: HTTP 429 (free_rate_limited)",
                )
            ],
        ),
    )
    status_file.write_text(status.model_dump_json(indent=2), encoding="utf-8")
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/?token=admin-token")
        diagnostics_denied = await client.get("/diagnostics")
        diagnostics = await client.get("/diagnostics?token=admin-token")

    assert "Needs attention" in page.text
    assert "AI failed 1" in page.text
    assert "Pending freezer" in page.text
    assert "remains pending" in page.text
    assert diagnostics_denied.status_code == 401
    assert diagnostics.status_code == 200
    assert "Recent watcher logs" in diagnostics.text


def test_recent_log_buffer_redacts_admin_tokens() -> None:
    buffer = RecentLogBuffer()
    logger = logging.getLogger("test.recent.logs")
    logger.addHandler(buffer)
    logger.setLevel(logging.INFO)
    try:
        logger.info('GET /tools?token=very-secret-token&action=all HTTP/1.1')
    finally:
        logger.removeHandler(buffer)

    assert len(buffer.entries) == 1
    assert "very-secret-token" not in buffer.entries[0]["message"]
    assert "token=[REDACTED]" in buffer.entries[0]["message"]


def test_recent_log_buffer_retains_redacted_response_detail() -> None:
    buffer = RecentLogBuffer()
    logger = logging.getLogger("test.model.response")
    logger.addHandler(buffer)
    logger.setLevel(logging.INFO)
    try:
        logger.info(
            "Model response received.",
            extra={"diagnostic_detail": '{"result":"ok"} token=secret'},
        )
    finally:
        logger.removeHandler(buffer)

    assert len(buffer.entries) == 1
    assert '"result":"ok"' in buffer.entries[0]["detail"]
    assert "secret" not in buffer.entries[0]["detail"]


@pytest.mark.asyncio
async def test_model_budget_increase_requires_confirmation_and_applies_immediately(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "settings.env"
    results_file = tmp_path / "evaluations.jsonl"
    usage_file = tmp_path / "model_usage.json"
    write_dotenv(
        env_file,
        {
            "WEB_ADMIN_TOKEN": "admin-token",
            "RESULTS_FILE": str(results_file),
        },
    )
    ModelUsageStore(usage_file).reserve()
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/model-usage")
        page = await client.get("/model-usage?token=admin-token")
        confirmation = await client.post(
            "/model-usage/limit?token=admin-token",
            data={"limit": "40"},
        )
        before_apply = ModelUsageStore(usage_file).snapshot()
        applied = await client.post(
            "/model-usage/limit/apply?token=admin-token",
            data={"limit": "40"},
            follow_redirects=False,
        )
        reset_confirmation = await client.get("/model-usage/reset?token=admin-token")
        reset = await client.post(
            "/model-usage/reset/apply?token=admin-token",
            follow_redirects=False,
        )

    assert denied.status_code == 401
    assert page.status_code == 200
    assert "1 / 30" in page.text
    assert "Confirm increased model budget" in confirmation.text
    assert before_apply.limit == 30
    assert applied.status_code == 303
    assert "Reset today's model usage?" in reset_confirmation.text
    assert reset.status_code == 303
    final_usage = ModelUsageStore(usage_file).snapshot()
    assert final_usage.limit == 40
    assert final_usage.used == 0


@pytest.mark.asyncio
async def test_seen_ads_page_explains_and_filters_baseline_ads(tmp_path: Path) -> None:
    env_file = tmp_path / "settings.env"
    state_file = tmp_path / "seen_ads.json"
    write_dotenv(
        env_file,
        {
            "WEB_ADMIN_TOKEN": "admin-token",
            "STATE_FILE": str(state_file),
        },
    )
    state_file.write_text(
        """{
  "seen_ads": {
    "m1": {
      "title": "Baseline freezer",
      "url": "https://www.marktplaats.nl/v/m1",
      "first_seen_at": "2026-08-14T00:00:00+00:00",
      "bootstrapped": true
    },
    "m2": {
      "title": "Processed freezer",
      "url": "https://www.marktplaats.nl/v/m2",
      "first_seen_at": "2026-08-14T01:00:00+00:00",
      "evaluation": {"next_action": "notify", "confidence": 0.9}
    }
  }
}
""",
        encoding="utf-8",
    )
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/seen")
        baseline = await client.get("/seen?token=admin-token&kind=baseline")

    assert denied.status_code == 401
    assert baseline.status_code == 200
    assert "Baseline freezer" in baseline.text
    assert "Present when tracking started; skipped AI evaluation." in baseline.text
    assert "Processed freezer" not in baseline.text


@pytest.mark.asyncio
async def test_pipeline_ai_phase_persists_and_advances_without_telegram(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "settings.env"
    state_file = tmp_path / "seen_ads.json"
    results_file = tmp_path / "evaluations.jsonl"
    status_file = tmp_path / "status.json"
    write_dotenv(
        env_file,
        {
            "WEB_ADMIN_TOKEN": "admin-token",
            "MARKTPLAATS_SEARCH_URL": "https://www.marktplaats.nl/lrp/api/search?query=test",
            "MARKTPLAATS_USE_CASE": "Find freezer chests.",
            "STATE_FILE": str(state_file),
            "RESULTS_FILE": str(results_file),
            "STATUS_FILE": str(status_file),
        },
    )
    preview_ad = Ad(
        id="m-preview",
        title="Preview freezer",
        url="https://www.marktplaats.nl/v/m-preview",
        price="EUR 100.00",
        location="Weert",
    )
    status_file.write_text(
        RuntimeStatus(
            last_summary=WatcherRunSummary(
                fetched_count=1,
                kept_count=1,
                filtered_count=0,
                new_count=1,
                evaluated_count=0,
                notified_count=0,
                evaluation_failed_count=1,
                evaluation_failures=[
                    EvaluationFailure(
                        ad_id=preview_ad.id,
                        title=preview_ad.title,
                        url=preview_ad.url,
                        error="ModelProviderError: free_rate_limited",
                    )
                ],
            )
        ).model_dump_json(),
        encoding="utf-8",
    )

    async def fake_fetch_preview(self: WatcherService) -> list[Ad]:
        self._preview_ads = {preview_ad.id: preview_ad}
        self._preview_fetched_at = datetime.now(UTC)
        self._preview_counts = (2, 1, 1)
        return [preview_ad]

    class FakeEvaluator:
        async def evaluate(self, ad: Ad) -> EvaluationResult:
            assert ad.id == preview_ad.id
            assert ad.description == "Full listing description."
            assert ad.listing_facts == {"Capacity": "458 L"}
            return EvaluationResult(
                relevant=True,
                confidence=0.8,
                reason="Promising dimensions.",
                signals=["Chest freezer"],
                next_action="review",
            )

    monkeypatch.setattr(WatcherService, "fetch_preview", fake_fetch_preview)
    async def fake_enrich_ad(_: object, ad: Ad) -> Ad:
        return ad.model_copy(
            update={
                "description": "Full listing description.",
                "listing_facts": {"Capacity": "458 L"},
            }
        )

    monkeypatch.setattr(
        "marktplaats_ad_watcher.web.MarktplaatsClient.enrich_ad",
        fake_enrich_ad,
    )
    monkeypatch.setattr(
        "marktplaats_ad_watcher.web.build_model_evaluator",
        lambda _: FakeEvaluator(),
    )
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/tools")
        fetched = await client.post("/tools/fetch?token=admin-token")
        tested = await client.post(
            "/tools/test?token=admin-token",
            data={"ad_id": preview_ad.id},
        )

    assert denied.status_code == 401
    assert fetched.status_code == 200
    assert "Preview freezer" in fetched.text
    assert "2</strong> fetched" in fetched.text
    assert "AI failed · pending" in fetched.text
    assert "free_rate_limited" in fetched.text
    assert 'class="preview-content"' in fetched.text
    assert 'class="secondary failure-detail"' in fetched.text
    assert "grid-template-columns: auto minmax(0, 1fr)" in fetched.text
    assert "overflow-wrap: anywhere" in fetched.text
    assert "Send result via Telegram" not in fetched.text
    assert tested.status_code == 200
    assert "AI phase completed for Preview freezer" in tested.text
    assert "AI complete · saved" in tested.text
    assert "Telegram not sent" in tested.text
    assert "Send result via Telegram" in tested.text
    assert "Promising dimensions." in tested.text
    profile_directory = tmp_path / "profiles" / "freezers"
    assert SeenStore(profile_directory / "seen_ads.json").has_seen(preview_ad.id)
    assert '"reason":"Promising dimensions."' in (
        profile_directory / "evaluations.jsonl"
    ).read_text(encoding="utf-8")
    progress = PipelineProgressStore(
        profile_directory / "pipeline_progress.json"
    ).get(preview_ad.id)
    assert progress is not None
    assert progress.telegram_sent is False
    summary = RuntimeStatusStore(profile_directory / "runtime_status.json").read().last_summary
    assert summary is not None
    assert summary.evaluation_failed_count == 0


@pytest.mark.asyncio
async def test_pipeline_telegram_phases_are_explicit_and_record_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "settings.env"
    results_file = tmp_path / "evaluations.jsonl"
    write_dotenv(
        env_file,
        {
            "WEB_ADMIN_TOKEN": "admin-token",
            "MARKTPLAATS_SEARCH_URL": "https://www.marktplaats.nl/lrp/api/search?query=test",
            "MARKTPLAATS_USE_CASE": "Find freezer chests.",
            "RESULTS_FILE": str(results_file),
        },
    )
    evaluated = EvaluatedAd(
        ad=Ad(id="m1", title="Saved freezer", url="https://www.marktplaats.nl/v/m1"),
        result=EvaluationResult(
            relevant=True,
            confidence=0.9,
            reason="Good size.",
            next_action="notify",
        ),
    )
    progress_store = PipelineProgressStore(tmp_path / "pipeline_progress.json")
    progress_store.save_ai_result(evaluated)
    sent_ids: list[str] = []
    standalone_calls = 0

    async def fake_send(notifier: object, value: EvaluatedAd) -> TelegramSendResult:
        del notifier
        sent_ids.append(value.ad.id)
        return TelegramSendResult(sent=True, message_id=42)

    async def fake_standalone(notifier: object) -> TelegramSendResult:
        nonlocal standalone_calls
        del notifier
        standalone_calls += 1
        return TelegramSendResult(sent=True, message_id=43)

    monkeypatch.setattr("marktplaats_ad_watcher.web.TelegramNotifier.send", fake_send)
    monkeypatch.setattr(
        "marktplaats_ad_watcher.web.TelegramNotifier.send_test_message",
        fake_standalone,
    )
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        result_send = await client.post(
            "/tools/telegram?token=admin-token",
            data={"ad_id": "m1"},
        )
        standalone = await client.post("/tools/telegram-test?token=admin-token")

    assert result_send.status_code == 200
    assert "delivery was recorded" in result_send.text
    assert standalone.status_code == 200
    assert "Standalone Telegram test message sent" in standalone.text
    assert sent_ids == ["m1"]
    assert standalone_calls == 1
    progress = PipelineProgressStore(
        tmp_path / "profiles" / "freezers" / "pipeline_progress.json"
    ).get("m1")
    assert progress is not None
    assert progress.telegram_sent is True
    assert progress.telegram_message_id == 42


@pytest.mark.asyncio
async def test_standalone_telegram_test_uses_active_profile_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "settings.env"
    write_dotenv(
        env_file,
        {
            "WEB_ADMIN_TOKEN": "admin-token",
            "MARKTPLAATS_SEARCH_URL": "https://www.marktplaats.nl/lrp/api/search?query=test",
            "MARKTPLAATS_USE_CASE": "Find freezer chests.",
        },
    )
    captured: list[str] = []

    async def fake_send_text(_: object, text: str) -> TelegramSendResult:
        captured.append(text)
        return TelegramSendResult(sent=True, message_id=44)

    monkeypatch.setattr(
        "marktplaats_ad_watcher.web.TelegramNotifier._send_text",
        fake_send_text,
    )

    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/tools/telegram-test?token=admin-token")

    assert response.status_code == 200
    assert captured
    assert "<b>[Freezers · freezers] Marktplaats Ad Watcher test</b>" in captured[0]


@pytest.mark.asyncio
async def test_manual_telegram_send_persists_profile_identity_for_imported_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "settings.env"
    results_file = tmp_path / "evaluations.jsonl"
    write_dotenv(
        env_file,
        {
            "WEB_ADMIN_TOKEN": "admin-token",
            "MARKTPLAATS_SEARCH_URL": "https://www.marktplaats.nl/lrp/api/search?query=test",
            "MARKTPLAATS_USE_CASE": "Find freezer chests.",
            "RESULTS_FILE": str(results_file),
            "STATE_FILE": str(tmp_path / "seen_ads.json"),
            "STATUS_FILE": str(tmp_path / "runtime_status.json"),
        },
    )
    legacy_result = EvaluatedAd(
        ad=Ad(id="m1", title="Legacy freezer", url="https://www.marktplaats.nl/v/m1"),
        result=EvaluationResult(
            relevant=True,
            confidence=0.88,
            reason="Looks suitable.",
            next_action="notify",
        ),
    )
    results_file.write_text(legacy_result.model_dump_json() + "\n", encoding="utf-8")
    sent_profiles: list[tuple[str | None, str | None]] = []

    async def fake_send(_: object, value: EvaluatedAd) -> TelegramSendResult:
        sent_profiles.append((value.profile_id, value.profile_name))
        return TelegramSendResult(sent=True, message_id=42)

    monkeypatch.setattr("marktplaats_ad_watcher.web.TelegramNotifier.send", fake_send)

    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/tools/telegram?token=admin-token",
            data={"ad_id": "m1"},
        )

    assert response.status_code == 200
    assert sent_profiles == [("freezers", "Freezers")]
    record = PipelineProgressStore(
        tmp_path / "profiles" / "freezers" / "pipeline_progress.json"
    ).get("m1")
    assert record is not None
    assert record.telegram_sent is True
    assert record.evaluated_ad.profile_id == "freezers"
    assert record.evaluated_ad.profile_name == "Freezers"


@pytest.mark.asyncio
async def test_healthz_is_paused_but_ok_when_search_settings_are_missing(tmp_path: Path) -> None:
    app = create_web_app(env_file=tmp_path / "settings.env", dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert "paused" in response.text


@pytest.mark.asyncio
async def test_healthz_is_not_ok_when_profile_migration_is_inconsistent(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "settings.env"
    write_dotenv(
        env_file,
        {
            "MARKTPLAATS_SEARCH_URL": "https://www.marktplaats.nl/lrp/api/search?query=test",
            "MARKTPLAATS_USE_CASE": "Find freezer chests.",
            "STATE_FILE": str(tmp_path / "seen_ads.json"),
            "RESULTS_FILE": str(tmp_path / "evaluations.jsonl"),
            "STATUS_FILE": str(tmp_path / "runtime_status.json"),
        },
    )
    (tmp_path / "seen_ads.json").write_text('{"seen_ads":{}}\n', encoding="utf-8")
    (tmp_path / "evaluations.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "runtime_status.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "pipeline_progress.json").write_text('{"records":{}}\n', encoding="utf-8")
    settings = Settings.from_environment(parse_dotenv(env_file))
    migrate_legacy_single_search(settings)

    backup_path = (
        tmp_path
        / "profile-migration-backups"
        / LEGACY_MIGRATION_NAME
        / "seen_ads.json"
    )
    backup_path.write_text('{"seen_ads":{"tampered":{"title":"changed"}}}\n', encoding="utf-8")

    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 503
    assert "profile migration inconsistent" in response.text


@pytest.mark.asyncio
async def test_production_evaluation_is_available_for_explicit_telegram_phase(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "settings.env"
    results_file = tmp_path / "evaluations.jsonl"
    write_dotenv(
        env_file,
        {
            "WEB_ADMIN_TOKEN": "admin-token",
            "RESULTS_FILE": str(results_file),
        },
    )
    evaluated = EvaluatedAd(
        ad=Ad(id="m1", title="Production freezer", url="https://www.marktplaats.nl/v/m1"),
        result=EvaluationResult(
            relevant=False,
            confidence=0.6,
            reason="Needs dimensions.",
            next_action="review",
        ),
    )
    results_file.write_text(evaluated.model_dump_json() + "\n", encoding="utf-8")
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/tools?token=admin-token")

    assert page.status_code == 200
    assert "Production freezer" in page.text
    assert "Production evaluation" in page.text
    assert "Telegram delivery not tracked by test pipeline" in page.text
    assert "Send result via Telegram" in page.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "reasoning", "temperature", "saved_reasoning", "saved_temperature"),
    [
        ("deepseek", "medium", "0.4", "", "0.4"),
        ("anthropic", "high", "0.7", "", "0"),
        ("openai", "medium", "0.4", "medium", "0"),
        ("openai", "none", "0.4", "none", "0.4"),
        ("gemini", "xhigh", "0.4", "", "0.4"),
    ],
)
async def test_provider_field_applicability_is_enforced_on_save(
    tmp_path: Path,
    provider: str,
    reasoning: str,
    temperature: str,
    saved_reasoning: str,
    saved_temperature: str,
) -> None:
    env_file = tmp_path / "settings.env"
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)
    provider_values = {
        "deepseek": ("https://api.deepseek.com/v1", "deepseek-v4-flash"),
        "anthropic": ("https://api.anthropic.com", "claude-haiku-4-5"),
        "openai": ("https://api.openai.com/v1", "gpt-5.6-luna"),
        "gemini": (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "gemini-3.5-flash-lite",
        ),
    }
    base_url, model = provider_values[provider]

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/config",
            data={
                "MODEL_PROVIDER": provider,
                "MODEL_BASE_URL": base_url,
                "MODEL_NAME": model,
                "MODEL_REASONING_EFFORT": reasoning,
                "MODEL_TEMPERATURE": temperature,
                "MODEL_MAX_TOKENS": "700",
            },
        )

    saved = parse_dotenv(env_file)
    assert saved["MODEL_REASONING_EFFORT"] == saved_reasoning
    assert saved["MODEL_TEMPERATURE"] == saved_temperature


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "reasoning", "expected_message"),
    [
        ("", "medium", "MODEL_PROVIDER"),
        ("unknown", "medium", "MODEL_PROVIDER"),
        ("deepseek", "enormous", "Unsupported reasoning effort"),
    ],
)
async def test_malformed_provider_submissions_return_400(
    tmp_path: Path,
    provider: str,
    reasoning: str,
    expected_message: str,
) -> None:
    env_file = tmp_path / "settings.env"
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/config",
            data={
                "MODEL_PROVIDER": provider,
                "MODEL_REASONING_EFFORT": reasoning,
            },
        )

    assert response.status_code == 400
    assert expected_message in response.text
    assert not env_file.exists()


@pytest.mark.asyncio
async def test_provider_and_reasoning_submissions_are_canonicalized(tmp_path: Path) -> None:
    env_file = tmp_path / "settings.env"
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/config",
            data={
                "MODEL_PROVIDER": " OpenAI ",
                "MODEL_REASONING_EFFORT": " MEDIUM ",
                "MODEL_BASE_URL": "https://api.openai.com/v1",
                "MODEL_NAME": "gpt-5.6-luna",
                "MODEL_TEMPERATURE": "0.4",
                "MODEL_MAX_TOKENS": "700",
            },
        )

    assert response.status_code == 303
    saved = parse_dotenv(env_file)
    assert saved["MODEL_PROVIDER"] == "openai"
    assert saved["MODEL_REASONING_EFFORT"] == "medium"
    assert saved["MODEL_TEMPERATURE"] == "0"