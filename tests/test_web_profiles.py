from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from marktplaats_ad_watcher.config import Settings, parse_dotenv, write_dotenv
from marktplaats_ad_watcher.models import Ad, EvaluatedAd, EvaluationResult
from marktplaats_ad_watcher.profiles import ProfileRegistryStore, SearchProfile
from marktplaats_ad_watcher.runner import ProfileExecutionSummary
from marktplaats_ad_watcher.state import SeenStore
from marktplaats_ad_watcher.web import WatcherService, create_web_app


def _write_settings(tmp_path: Path) -> Path:
    env_file = tmp_path / "settings.env"
    write_dotenv(
        env_file,
        {
            "WEB_ADMIN_TOKEN": "admin-token",
            "MARKTPLAATS_SEARCH_URL": "https://www.marktplaats.nl/lrp/api/search?query=freezer",
            "MARKTPLAATS_USE_CASE": "Find useful freezer chests.",
            "STATE_FILE": str(tmp_path / "seen_ads.json"),
            "RESULTS_FILE": str(tmp_path / "evaluations.jsonl"),
            "STATUS_FILE": str(tmp_path / "runtime_status.json"),
        },
    )
    return env_file


def _settings(env_file: Path) -> Settings:
    return Settings.from_environment(parse_dotenv(env_file))


def _evaluated_ad(ad_id: str, title: str, profile_id: str, profile_name: str) -> EvaluatedAd:
    return EvaluatedAd(
        ad=Ad(id=ad_id, title=title, url=f"https://www.marktplaats.nl/v/{ad_id}"),
        result=EvaluationResult(
            relevant=True,
            confidence=0.9,
            reason="Matches the profile.",
            next_action="notify",
        ),
        evaluated_at=datetime.now(UTC),
        profile_id=profile_id,
        profile_name=profile_name,
    )


@pytest.mark.asyncio
async def test_scheduled_web_service_uses_all_enabled_profile_orchestration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = _write_settings(tmp_path)
    orchestrator = type(
        "Orchestrator",
        (),
        {"run_all_enabled": AsyncMock(return_value=ProfileExecutionSummary())},
    )()
    monkeypatch.setattr(
        "marktplaats_ad_watcher.web.build_profile_orchestrator",
        lambda _: orchestrator,
    )

    await WatcherService(env_file=env_file, dry_run=True).run_once()

    orchestrator.run_all_enabled.assert_awaited_once()


@pytest.mark.asyncio
async def test_profile_management_and_global_config_preserve_profile_criteria(
    tmp_path: Path,
) -> None:
    env_file = _write_settings(tmp_path)
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/profiles")
        page = await client.get("/profiles?token=admin-token")
        created = await client.post(
            "/profiles/create?token=admin-token",
            data={
                "id": "bicycles",
                "name": "Bicycles",
                "search_url": "https://www.marktplaats.nl/lrp/api/search?query=bicycle",
                "use_case": "Find reliable city bicycles.",
                "poll_interval_seconds": "120",
                "enabled": "on",
            },
            follow_redirects=False,
        )
        invalid_id = await client.post(
            "/profiles/create?token=admin-token",
            data={
                "id": "Bicycles/unsafe",
                "name": "Invalid",
                "search_url": "https://www.marktplaats.nl/lrp/api/search?query=bicycle",
                "use_case": "Find bicycles.",
            },
        )
        edited = await client.post(
            "/profiles/bicycles/edit?token=admin-token&profile=bicycles",
            data={
                "name": "City bicycles",
                "search_url": "https://www.marktplaats.nl/lrp/api/search?query=city-bike",
                "use_case": "Find maintained city bicycles.",
                "poll_interval_seconds": "300",
                "enabled": "on",
            },
            follow_redirects=False,
        )
        saved_global = await client.post(
            "/config?token=admin-token&profile=bicycles",
            data={
                "MODEL_PROVIDER": "deepseek",
                "MODEL_BASE_URL": "https://api.deepseek.com/v1",
                "MODEL_NAME": "deepseek-v4-flash",
                "MODEL_MAX_TOKENS": "700",
                "MODEL_TEMPERATURE": "0",
                "MODEL_JSON_MODE": "true",
            },
            follow_redirects=False,
        )

    registry = ProfileRegistryStore(tmp_path).load()
    bicycles = registry.profile("bicycles")
    persisted = parse_dotenv(env_file)

    assert denied.status_code == 401
    assert page.status_code == 200
    assert "Freezers" in page.text
    assert created.status_code == 303
    assert invalid_id.status_code == 400
    assert edited.status_code == 303
    assert saved_global.status_code == 303
    assert bicycles.id == "bicycles"
    assert bicycles.name == "City bicycles"
    assert bicycles.search_url.endswith("query=city-bike")
    assert bicycles.use_case == "Find maintained city bicycles."
    assert bicycles.poll_interval_seconds == 300
    assert persisted["MARKTPLAATS_SEARCH_URL"].endswith("query=freezer")
    assert persisted["MARKTPLAATS_USE_CASE"] == "Find useful freezer chests."


@pytest.mark.asyncio
async def test_profile_scope_aggregate_read_only_and_archived_history(tmp_path: Path) -> None:
    env_file = _write_settings(tmp_path)
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/profiles?token=admin-token")
        created = await client.post(
            "/profiles/create?token=admin-token",
            data={
                "id": "bicycles",
                "name": "Bicycles",
                "search_url": "https://www.marktplaats.nl/lrp/api/search?query=bicycle",
                "use_case": "Find reliable city bicycles.",
                "enabled": "on",
            },
            follow_redirects=False,
        )
    assert created.status_code == 303

    settings = _settings(env_file)
    registry = ProfileRegistryStore(tmp_path).load()
    freezers = settings.for_profile(registry.profile("freezers"))
    bicycles = settings.for_profile(registry.profile("bicycles"))
    SeenStore(freezers.state_file).append_result(
        freezers.results_file,
        _evaluated_ad("m-freezer", "Freezer result", "freezers", "Freezers"),
    )
    SeenStore(bicycles.state_file).append_result(
        bicycles.results_file,
        _evaluated_ad("m-bike", "Bicycle result", "bicycles", "Bicycles"),
    )
    SeenStore(bicycles.state_file).mark_seen(
        Ad(id="m-bike", title="Bicycle result", url="https://www.marktplaats.nl/v/m-bike")
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        bicycle_page = await client.get("/evaluations?token=admin-token&profile=bicycles")
        bicycle_download = await client.get(
            "/api/evaluations?token=admin-token&profile=bicycles"
        )
        all_page = await client.get("/evaluations?token=admin-token&profile=all")
        all_download = await client.get("/api/evaluations?token=admin-token&profile=all")
        seen = await client.get("/seen?token=admin-token&profile=bicycles")
        rejected_test = await client.post(
            "/tools/test?token=admin-token&profile=all",
            data={"ad_id": "m-bike"},
        )
        rejected_run = await client.post("/run-now?token=admin-token&profile=all")
        archived = await client.post(
            "/profiles/bicycles/archive?token=admin-token&profile=freezers",
            follow_redirects=False,
        )
        archived_history = await client.get(
            "/evaluations?token=admin-token&profile=bicycles"
        )
        rejected_archived_toggle = await client.post(
            "/profiles/bicycles/toggle?token=admin-token&profile=bicycles"
        )
        rejected_default_archive = await client.post(
            "/profiles/freezers/archive?token=admin-token&profile=freezers"
        )

    registry = ProfileRegistryStore(tmp_path).load()
    archived_profile = registry.profile("bicycles")

    assert "Bicycle result" in bicycle_page.text
    assert "Freezer result" not in bicycle_page.text
    assert bicycle_download.headers["content-disposition"] == (
        'attachment; filename="evaluations-bicycles.json"'
    )
    assert [record["ad"]["id"] for record in bicycle_download.json()] == ["m-bike"]
    assert "Bicycle result" in all_page.text
    assert "Freezer result" in all_page.text
    assert "Bicycles · bicycles" in all_page.text
    assert "Freezers · freezers" in all_page.text
    assert all_download.headers["content-disposition"] == (
        'attachment; filename="evaluations-all.json"'
    )
    assert {record["ad"]["id"] for record in all_download.json()} == {"m-bike", "m-freezer"}
    assert "Bicycle result" in seen.text
    assert rejected_test.status_code == 400
    assert rejected_run.status_code == 400
    assert archived.status_code == 303
    assert archived_profile.archived is True
    assert archived_profile.enabled is False
    assert "Bicycle result" in archived_history.text
    assert rejected_archived_toggle.status_code == 400
    assert rejected_default_archive.status_code == 400


@pytest.mark.asyncio
async def test_preview_cache_and_pipeline_actions_are_profile_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = _write_settings(tmp_path)
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/profiles?token=admin-token")
        await client.post(
            "/profiles/create?token=admin-token",
            data={
                "id": "bicycles",
                "name": "Bicycles",
                "search_url": "https://www.marktplaats.nl/lrp/api/search?query=bicycle",
                "use_case": "Find reliable city bicycles.",
                "enabled": "on",
            },
            follow_redirects=False,
        )

        async def fake_fetch_preview(
            service: WatcherService,
            profile: SearchProfile | None = None,
        ) -> list[Ad]:
            assert profile is not None
            ad = Ad(id="m-bike", title="Cached bicycle", url="https://www.marktplaats.nl/v/m-bike")
            profile_id = profile.id
            service._profile_preview_ads[profile_id] = {ad.id: ad}
            service._profile_preview_fetched_at[profile_id] = datetime.now(UTC)
            service._profile_preview_counts[profile_id] = (1, 1, 0)
            return [ad]

        monkeypatch.setattr(WatcherService, "fetch_preview", fake_fetch_preview)
        fetched = await client.post("/tools/fetch?token=admin-token&profile=bicycles")
        bicycle_tools = await client.get("/tools?token=admin-token&profile=bicycles")
        freezer_tools = await client.get("/tools?token=admin-token&profile=freezers")

    assert fetched.status_code == 200
    assert "Cached bicycle" in bicycle_tools.text
    assert "Cached bicycle" not in freezer_tools.text
