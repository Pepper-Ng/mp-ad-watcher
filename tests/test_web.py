from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from marktplaats_ad_watcher.config import parse_dotenv, write_dotenv
from marktplaats_ad_watcher.web import ERROR_RETRY_SECONDS, WatcherService, create_web_app


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