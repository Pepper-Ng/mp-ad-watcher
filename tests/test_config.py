from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from marktplaats_ad_watcher.config import Settings


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "MARKTPLAATS_SEARCH_URL": "https://www.marktplaats.nl/lrp/api/search?query=test",
        "MARKTPLAATS_USE_CASE": "Find relevant listings.",
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("POLL_INTERVAL_SECONDS", "0"),
        ("MAX_ADS_PER_POLL", "101"),
        ("NOTIFY_MIN_CONFIDENCE", "1.1"),
        ("REVIEW_MIN_CONFIDENCE", "-0.1"),
        ("REQUEST_TIMEOUT_SECONDS", "0"),
    ],
)
def test_settings_reject_out_of_range_values(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name):
        Settings.from_environment(_environment(**{name: value}))


def test_settings_reject_invalid_urls() -> None:
    with pytest.raises(ValueError, match="MARKTPLAATS_SEARCH_URL"):
        Settings.from_environment(_environment(MARKTPLAATS_SEARCH_URL="not-a-url"))


def test_deepseek_remains_the_backwards_compatible_default() -> None:
    settings = Settings.from_environment(_environment(DEEPSEEK_API_KEY="legacy-key"))

    assert settings.model_provider == "deepseek"
    assert settings.model_api_key == "legacy-key"
    assert settings.model_base_url == "https://api.deepseek.com/v1"
    assert settings.model_name == "deepseek-v4-flash"
    assert settings.notify_ai_failures is True


def test_ai_failure_telegram_alerts_can_be_disabled() -> None:
    settings = Settings.from_environment(_environment(NOTIFY_AI_FAILURES="false"))

    assert settings.notify_ai_failures is False


def test_legacy_deepseek_values_are_migrated() -> None:
    settings = Settings.from_environment(
        _environment(
            DEEPSEEK_API_KEY="legacy-key",
            DEEPSEEK_BASE_URL="https://legacy.example.test/v1",
            DEEPSEEK_MODEL="legacy-model",
            DEEPSEEK_TEMPERATURE="0.2",
            DEEPSEEK_MAX_TOKENS="800",
        )
    )

    assert settings.model_api_key == "legacy-key"
    assert settings.model_base_url == "https://legacy.example.test/v1"
    assert settings.model_name == "legacy-model"
    assert settings.model_temperature == 0.2
    assert settings.model_max_tokens == 800


def test_openai_provider_supplies_responses_api_defaults() -> None:
    settings = Settings.from_environment(
        _environment(MODEL_PROVIDER="openai", MODEL_API_KEY="openai-key")
    )

    assert settings.model_base_url == "https://api.openai.com/v1"
    assert settings.model_name == "gpt-5.6-luna"
    assert settings.model_reasoning_effort == "medium"


def test_custom_openai_provider_requires_endpoint_and_model() -> None:
    with pytest.raises(ValueError, match="MODEL_BASE_URL"):
        Settings.from_environment(
            _environment(MODEL_PROVIDER="openai-compatible", MODEL_API_KEY="custom-key")
        )


def test_settings_reject_unknown_provider_and_reasoning_effort() -> None:
    with pytest.raises(ValueError, match="MODEL_PROVIDER"):
        Settings.from_environment(_environment(MODEL_PROVIDER="unknown"))

    with pytest.raises(ValueError, match="MODEL_REASONING_EFFORT"):
        Settings.from_environment(_environment(MODEL_REASONING_EFFORT="enormous"))


def test_marktplaats_search_url_defaults_ui_sort_when_missing() -> None:
    settings = Settings.from_environment(
        _environment(
            MARKTPLAATS_SEARCH_URL=(
                "https://www.marktplaats.nl/lrp/api/search?"
                "limit=30&offset=0&query=vrieskist&postcode=6005JW&"
                "distanceMeters=30000&searchInTitleAndDescription=true"
            )
        )
    )

    parsed = urlsplit(settings.marktplaats_search_url)
    params = parse_qs(parsed.query)
    assert params["sortBy"] == ["SORT_INDEX"]
    assert params["sortOrder"] == ["DECREASING"]


def test_marktplaats_query_route_is_converted_to_search_endpoint() -> None:
    settings = Settings.from_environment(
        _environment(
            MARKTPLAATS_SEARCH_URL=(
                "https://www.marktplaats.nl/q/vrieskist/"
                "#sortBy:SORT_INDEX|sortOrder:DECREASING|distanceMeters:30000|postcode:6005JW"
            )
        )
    )

    parsed = urlsplit(settings.marktplaats_search_url)
    params = parse_qs(parsed.query)
    assert parsed.path == "/lrp/api/search"
    assert params["query"] == ["vrieskist"]
    assert params["distanceMeters"] == ["30000"]
    assert params["postcode"] == ["6005JW"]
    assert params["searchInTitleAndDescription"] == ["true"]