from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from marktplaats_ad_watcher.config import Settings, parse_dotenv, write_dotenv
from marktplaats_ad_watcher.web import WatcherService, create_web_app


def test_fallback_can_inherit_the_primary_provider_connection() -> None:
    settings = Settings.from_environment(
        {
            "MARKTPLAATS_SEARCH_URL": "https://www.marktplaats.nl/lrp/api/search?query=test",
            "MARKTPLAATS_USE_CASE": "Find relevant listings.",
            "MODEL_PROVIDER": "openai-compatible",
            "MODEL_API_KEY": "primary-key",
            "MODEL_BASE_URL": "https://router.example.test/v1",
            "MODEL_NAME": "cheap-model",
            "FALLBACK_MODEL_ENABLED": "true",
            "FALLBACK_MODEL_USE_BASE_PROVIDER": "true",
            "FALLBACK_MODEL_NAME": "reliable-model",
        }
    )

    assert settings.fallback_model_use_base_provider is True
    assert settings.fallback_model_provider == "openai-compatible"
    assert settings.fallback_model_api_key == "primary-key"
    assert settings.fallback_model_base_url == "https://router.example.test/v1"
    assert settings.fallback_model_name == "reliable-model"


@pytest.mark.asyncio
async def test_fallback_base_provider_mode_hides_independent_connection_fields(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "settings.env"
    write_dotenv(
        env_file,
        {
            "MARKTPLAATS_SEARCH_URL": "https://www.marktplaats.nl/lrp/api/search?query=test",
            "MARKTPLAATS_USE_CASE": "Find relevant listings.",
            "MODEL_PROVIDER": "openai-compatible",
            "MODEL_API_KEY": "primary-key",
            "MODEL_BASE_URL": "https://router.example.test/v1",
            "MODEL_NAME": "cheap-model",
        },
    )
    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/config")
        response = await client.post(
            "/config",
            data={
                "MODEL_PROVIDER": "openai-compatible",
                "MODEL_API_KEY": "",
                "MODEL_BASE_URL": "https://router.example.test/v1",
                "MODEL_NAME": "cheap-model",
                "MODEL_MAX_TOKENS": "700",
                "MODEL_TEMPERATURE": "0",
                "FALLBACK_MODEL_ENABLED": "on",
                "FALLBACK_MODEL_USE_BASE_PROVIDER": "on",
                "FALLBACK_MODEL_NAME": "reliable-model",
                "FALLBACK_MODEL_MAX_TOKENS": "900",
                "FALLBACK_MODEL_TEMPERATURE": "0",
            },
            follow_redirects=False,
        )

    saved = parse_dotenv(env_file)
    settings = WatcherService(env_file=env_file, dry_run=True)._load_settings()

    assert page.status_code == 200
    assert "id='fallback-use-base-provider'" in page.text
    assert 'id="fallback-provider-settings"' in page.text
    assert "applyFallbackProviderMode" in page.text
    assert 'id="fallback-reasoning-field"' in page.text
    assert 'id="fallback-temperature-field"' in page.text
    assert "applyFallbackProviderCapabilities" in page.text
    assert "fallbackReasoningField.hidden = !defaults.reasoningSupported" in page.text
    provider_settings_start = page.text.index('id="fallback-provider-settings"')
    provider_settings_end = page.text.index("</div>", provider_settings_start)
    provider_settings = page.text[provider_settings_start:provider_settings_end]
    assert "FALLBACK_MODEL_NAME" not in provider_settings
    assert "FALLBACK_MODEL_REASONING_EFFORT" not in provider_settings
    assert "FALLBACK_MODEL_TEMPERATURE" not in provider_settings
    assert "FALLBACK_MODEL_MAX_TOKENS" not in provider_settings
    assert response.status_code == 303
    assert saved["FALLBACK_MODEL_USE_BASE_PROVIDER"] == "true"
    assert settings.fallback_model_provider == "openai-compatible"
    assert settings.fallback_model_api_key == "primary-key"
    assert settings.fallback_model_base_url == "https://router.example.test/v1"
    assert settings.fallback_model_name == "reliable-model"
