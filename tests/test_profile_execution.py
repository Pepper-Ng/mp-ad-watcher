from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from marktplaats_ad_watcher.config import Settings
from marktplaats_ad_watcher.models import (
    Ad,
    EvaluatedAd,
    EvaluationResult,
    TelegramSendResult,
    WatcherRunSummary,
)
from marktplaats_ad_watcher.profiles import (
    DEFAULT_PROFILE_ID,
    ProfileRegistry,
    ProfileRegistryStore,
    SearchProfile,
    migrate_legacy_single_search,
)
from marktplaats_ad_watcher.runner import ProfileOrchestrator, Watcher
from marktplaats_ad_watcher.state import SeenStore
from marktplaats_ad_watcher.status import RuntimeStatusStore
from marktplaats_ad_watcher.usage import ModelDailyLimitExceeded, ModelUsageStore


def _settings(data_root: Path) -> Settings:
    return Settings.from_environment(
        {
            "MARKTPLAATS_SEARCH_URL": "https://www.marktplaats.nl/lrp/api/search?query=freezer",
            "MARKTPLAATS_USE_CASE": "Find useful freezer chests.",
            "POLL_INTERVAL_SECONDS": "60",
            "STATE_FILE": str(data_root / "seen_ads.json"),
            "RESULTS_FILE": str(data_root / "evaluations.jsonl"),
            "STATUS_FILE": str(data_root / "runtime_status.json"),
        }
    )


def _profile(
    profile_id: str,
    *,
    name: str | None = None,
    enabled: bool = True,
    sort_order: int,
    poll_interval_seconds: int | None = None,
) -> SearchProfile:
    return SearchProfile(
        id=profile_id,
        name=name or profile_id.title(),
        search_url=f"https://www.marktplaats.nl/lrp/api/search?query={profile_id}",
        use_case=f"Find relevant {profile_id} listings.",
        enabled=enabled,
        sort_order=sort_order,
        bootstrap_existing_ads=False,
        poll_interval_seconds=poll_interval_seconds,
    )


def _registry(settings: Settings, *profiles: SearchProfile) -> None:
    ProfileRegistryStore(settings.data_root).save_new(
        ProfileRegistry(default_profile_id=DEFAULT_PROFILE_ID, profiles=profiles)
    )


def _summary() -> WatcherRunSummary:
    return WatcherRunSummary(
        fetched_count=1,
        kept_count=1,
        filtered_count=0,
        new_count=0,
        evaluated_count=0,
        notified_count=0,
    )


class RecordingWatcher:
    def __init__(
        self,
        *,
        settings: Settings,
        status_store: RuntimeStatusStore,
        calls: list[str],
        failing_ids: set[str],
    ) -> None:
        self._settings = settings
        self._status_store = status_store
        self._calls = calls
        self._failing_ids = failing_ids

    async def run_once(self) -> WatcherRunSummary:
        self._calls.append(self._settings.active_profile_id or "legacy")
        self._status_store.mark_started()
        if self._settings.active_profile_id in self._failing_ids:
            error = RuntimeError(f"{self._settings.active_profile_id} is unavailable")
            self._status_store.mark_failed(error)
            raise error
        summary = _summary()
        self._status_store.mark_finished(summary)
        return summary


def _orchestrator(
    settings: Settings,
    calls: list[str],
    *,
    failing_ids: set[str] | None = None,
) -> ProfileOrchestrator:
    failures = failing_ids or set()

    def build(settings: Settings, status_store: RuntimeStatusStore) -> RecordingWatcher:
        return RecordingWatcher(
            settings=settings,
            status_store=status_store,
            calls=calls,
            failing_ids=failures,
        )

    return ProfileOrchestrator(settings=settings, watcher_builder=build)


@pytest.mark.asyncio
async def test_profile_orchestrator_runs_selected_default_and_enabled_profiles_in_order(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "data")
    freezers = _profile("freezers", name="Freezers", sort_order=0)
    bicycles = _profile("bicycles", name="Bicycles", sort_order=1, poll_interval_seconds=5)
    paused = _profile("pools", name="Kids pools", enabled=False, sort_order=2)
    _registry(settings, freezers, bicycles, paused)
    calls: list[str] = []
    orchestrator = _orchestrator(settings, calls)

    default_result = await orchestrator.run_profile()
    selected_result = await orchestrator.run_profile("bicycles")
    calls.clear()
    all_result = await orchestrator.run_all_enabled()

    assert [result.profile_id for result in default_result.profiles] == ["freezers"]
    assert [result.profile_id for result in selected_result.profiles] == ["bicycles"]
    assert calls == ["freezers", "bicycles"]
    assert [result.profile_id for result in all_result.profiles] == [
        "freezers",
        "bicycles",
        "pools",
    ]
    assert all_result.profiles[2].skipped_reason == "disabled"

    freezer_settings = settings.for_profile(freezers)
    bicycle_settings = settings.for_profile(bicycles)
    assert freezer_settings.poll_interval_seconds == 60
    assert bicycle_settings.poll_interval_seconds == 5
    assert bicycle_settings.pipeline_progress_file == (
        tmp_path / "data" / "profiles" / "bicycles" / "pipeline_progress.json"
    )
    assert freezer_settings.global_model_usage_file == bicycle_settings.global_model_usage_file
    assert RuntimeStatusStore(freezer_settings.status_file).read().next_run_at is not None
    assert RuntimeStatusStore(bicycle_settings.status_file).read().next_run_at is not None

    calls.clear()
    due_result = await orchestrator.run_all_enabled(due_only=True)
    assert calls == []
    assert [result.skipped_reason for result in due_result.profiles] == [
        "not_due",
        "not_due",
        "disabled",
    ]


@pytest.mark.asyncio
async def test_profile_failure_isolated_and_persisted_without_stopping_later_profiles(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "data")
    freezers = _profile("freezers", sort_order=0)
    bicycles = _profile("bicycles", sort_order=1)
    _registry(settings, freezers, bicycles)
    calls: list[str] = []

    result = await _orchestrator(settings, calls, failing_ids={"freezers"}).run_all_enabled()

    assert calls == ["freezers", "bicycles"]
    assert result.profiles[0].error == "RuntimeError: freezers is unavailable"
    assert result.profiles[1].summary is not None
    freezer_status = RuntimeStatusStore(settings.for_profile(freezers).status_file).read()
    assert freezer_status.total_errors == 1
    assert freezer_status.next_run_at is not None


def test_profile_settings_share_the_global_model_quota(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "data")
    freezer_settings = settings.for_profile(_profile("freezers", sort_order=0))
    bicycle_settings = settings.for_profile(_profile("bicycles", sort_order=1))
    freezer_usage = ModelUsageStore(freezer_settings.global_model_usage_file)
    bicycle_usage = ModelUsageStore(bicycle_settings.global_model_usage_file)

    freezer_usage.set_limit(1)
    freezer_usage.reserve()

    with pytest.raises(ModelDailyLimitExceeded):
        bicycle_usage.reserve()


@pytest.mark.asyncio
async def test_profile_evaluation_output_records_profile_identity_and_legacy_records_still_parse(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "data")
    profile = _profile("freezers", name="Freezers", sort_order=0)
    profile_settings = settings.for_profile(profile)

    class Client:
        async def fetch_ads(self, search_url: str, *, limit: int) -> list[Ad]:
            del search_url, limit
            return [Ad(id="m1", title="Freezer", url="https://example.test/m1")]

        async def enrich_ad(self, ad: Ad) -> Ad:
            return ad

    class Evaluator:
        async def evaluate(self, ad: Ad) -> EvaluationResult:
            del ad
            return EvaluationResult(
                relevant=False,
                confidence=0.1,
                reason="Not a match.",
                next_action="ignore",
            )

    class Notifier:
        async def send(self, evaluated_ad: Any) -> TelegramSendResult:
            del evaluated_ad
            return TelegramSendResult(sent=False, reason="not needed")

    watcher = Watcher(
        settings=profile_settings,
        marktplaats_client=Client(),
        evaluator=Evaluator(),
        notifier=Notifier(),
        store=SeenStore(profile_settings.state_file),
    )

    await watcher.run_once()

    persisted = json.loads(profile_settings.results_file.read_text(encoding="utf-8"))
    assert persisted["profile_id"] == "freezers"
    assert persisted["profile_name"] == "Freezers"

    legacy_record = {
        "ad": {"id": "m-legacy", "title": "Legacy", "url": "https://example.test/legacy"},
        "result": {
            "relevant": False,
            "confidence": 0.2,
            "reason": "Legacy result.",
            "next_action": "ignore",
        },
        "evaluated_at": datetime.now(UTC).isoformat(),
    }
    parsed = EvaluatedAd.model_validate(legacy_record)
    assert parsed.profile_id is None
    assert parsed.profile_name is None


@pytest.mark.asyncio
async def test_migrated_seen_ad_is_not_evaluated_again(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "seen_ads.json").write_text(
        json.dumps(
            {
                "seen_ads": {
                    "m1": {
                        "title": "Existing freezer",
                        "url": "https://example.test/m1",
                        "first_seen_at": "2026-08-14T00:00:00+00:00",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    settings = _settings(data_root)
    migrated = migrate_legacy_single_search(settings)
    profile_settings = settings.for_profile(migrated.registry.default_profile)
    evaluated_ids: list[str] = []

    class Client:
        async def fetch_ads(self, search_url: str, *, limit: int) -> list[Ad]:
            del search_url, limit
            return [Ad(id="m1", title="Existing freezer", url="https://example.test/m1")]

        async def enrich_ad(self, ad: Ad) -> Ad:
            return ad

    class Evaluator:
        async def evaluate(self, ad: Ad) -> EvaluationResult:
            evaluated_ids.append(ad.id)
            return EvaluationResult(
                relevant=False,
                confidence=0.1,
                reason="Not a match.",
                next_action="ignore",
            )

    class Notifier:
        async def send(self, evaluated_ad: Any) -> TelegramSendResult:
            del evaluated_ad
            return TelegramSendResult(sent=False, reason="not needed")

    summary = await Watcher(
        settings=profile_settings,
        marktplaats_client=Client(),
        evaluator=Evaluator(),
        notifier=Notifier(),
        store=SeenStore(profile_settings.state_file),
    ).run_once()

    assert profile_settings.bootstrap_existing_ads is False
    assert summary.new_count == 0
    assert evaluated_ids == []


@pytest.mark.asyncio
async def test_profile_schedule_runs_after_its_persisted_next_run_time(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "data")
    profile = _profile("freezers", sort_order=0)
    _registry(settings, profile)
    profile_settings = settings.for_profile(profile)
    status_store = RuntimeStatusStore(profile_settings.status_file)
    status_store.set_next_run_at(datetime.now(UTC) - timedelta(seconds=1))
    calls: list[str] = []

    result = await _orchestrator(settings, calls).run_profile(due_only=True)

    assert calls == ["freezers"]
    assert result.profiles[0].summary is not None
