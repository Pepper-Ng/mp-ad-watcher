from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from marktplaats_ad_watcher.config import write_dotenv
from marktplaats_ad_watcher.models import Ad, EvaluatedAd, EvaluationResult
from marktplaats_ad_watcher.pipeline_progress import PipelineProgressStore
from marktplaats_ad_watcher.runner import Watcher
from marktplaats_ad_watcher.state import SeenStore
from marktplaats_ad_watcher.web import WatcherService, create_web_app
from tests.test_runner import FakeMarktplaatsClient, FakeNotifier, RecordingEvaluator, _settings


@pytest.mark.asyncio
async def test_due_pending_failure_retries_without_reappearing_in_search(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ad = Ad(id="m-retry", title="Pending freezer", url="https://example.test/m-retry")
    store = SeenStore(settings.state_file)
    store.mark_model_failure(ad, error="ModelOutputError: invalid JSON", max_retries=2)
    data = json.loads(settings.state_file.read_text(encoding="utf-8"))
    data["model_failures"][ad.id]["next_retry_at"] = (
        datetime.now(UTC) - timedelta(minutes=1)
    ).isoformat()
    settings.state_file.write_text(json.dumps(data), encoding="utf-8")

    class AvailableClient(FakeMarktplaatsClient):
        async def is_ad_available(self, checked_ad: Ad) -> bool:
            return checked_ad.id == ad.id

    evaluator = RecordingEvaluator()
    watcher = Watcher(
        settings=settings,
        marktplaats_client=AvailableClient([]),
        evaluator=evaluator,
        notifier=FakeNotifier(),
        store=SeenStore(settings.state_file),
    )

    await watcher.run_once()

    assert evaluator.evaluated_ids == [ad.id]
    assert SeenStore(settings.state_file).model_failure(ad.id) is None


@pytest.mark.asyncio
async def test_manual_retry_clears_failure_and_seen_page_labels_stopped_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_file = tmp_path / "seen_ads.json"
    results_file = tmp_path / "evaluations.jsonl"
    env_file = tmp_path / "settings.env"
    write_dotenv(
        env_file,
        {
            "WEB_ADMIN_TOKEN": "admin-token",
            "STATE_FILE": str(state_file),
            "RESULTS_FILE": str(results_file),
        },
    )
    ad = Ad(id="m-stopped", title="Stopped freezer", url="https://example.test/m-stopped")
    store = SeenStore(state_file)
    for _ in range(3):
        store.mark_model_failure(ad, error="ModelOutputError: invalid JSON", max_retries=2)

    app = create_web_app(env_file=env_file, dry_run=False)
    monkeypatch.setattr(
        "marktplaats_ad_watcher.web.MarktplaatsClient.enrich_ad",
        AsyncMock(return_value=ad),
    )

    class SuccessfulEvaluator:
        async def evaluate(self, evaluated_ad: Ad) -> EvaluationResult:
            assert evaluated_ad.id == ad.id
            return EvaluationResult(
                relevant=True,
                confidence=0.9,
                reason="Retry succeeded.",
                next_action="notify",
            )

    monkeypatch.setattr(
        "marktplaats_ad_watcher.web.build_model_evaluator",
        lambda _: SuccessfulEvaluator(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        seen = await client.get("/seen?token=admin-token")
        confirmation = await client.get(
            "/diagnostics/failures/m-stopped/retry?token=admin-token"
        )
        retried = await client.post(
            "/diagnostics/failures/m-stopped/retry?token=admin-token",
            follow_redirects=False,
        )

    assert "AI retries stopped" in seen.text
    assert "Retry AI" in seen.text
    assert "Retry AI evaluation?" in confirmation.text
    assert retried.status_code == 303
    assert SeenStore(state_file).model_failure(ad.id) is None


@pytest.mark.asyncio
async def test_tools_shows_manual_tests_but_not_production_history(tmp_path: Path) -> None:
    env_file = tmp_path / "settings.env"
    results_file = tmp_path / "evaluations.jsonl"
    write_dotenv(env_file, {"RESULTS_FILE": str(results_file)})
    progress = PipelineProgressStore(tmp_path / "pipeline_progress.json")
    manual = EvaluatedAd(
        ad=Ad(id="manual", title="Manual test result", url="https://example.test/manual"),
        result=EvaluationResult(relevant=False, confidence=0.2, reason="Manual.", next_action="ignore"),
    )
    production = EvaluatedAd(
        ad=Ad(id="production", title="Production result", url="https://example.test/production"),
        result=EvaluationResult(relevant=False, confidence=0.2, reason="Production.", next_action="ignore"),
    )
    progress.save_ai_result(manual)
    SeenStore(tmp_path / "seen_ads.json").append_result(results_file, production)

    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/tools")

    assert "Manual test result" in page.text
    assert "Production result" not in page.text
