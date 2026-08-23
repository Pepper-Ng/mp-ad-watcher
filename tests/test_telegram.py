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
from marktplaats_ad_watcher.telegram import (
    TelegramNotifier,
    _format_ai_failure_alert,
    _format_message,
)


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    defaults = Settings(
        marktplaats_search_url="https://www.marktplaats.nl/lrp/api/search?query=test",
        marktplaats_use_case="Find relevant listings.",
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
        request_timeout_seconds=20.0,
        user_agent="test",
        web_admin_token=None,
    )
    return replace(defaults, **overrides)


def test_review_message_uses_result_fields() -> None:
    evaluated_ad = EvaluatedAd(
        ad=Ad(
            id="m1",
            title="Beko vrieskist",
            url="https://www.marktplaats.nl/v/m1",
            price="EUR 100.00",
            location="Weert",
            seller="Sam",
        ),
        result=EvaluationResult(
            relevant=False,
            confidence=0.42,
            reason="Lijkt bruikbaar, maar diepte ontbreekt.",
            signals=["205 liter", "vrieskist"],
            concerns=["diepte ontbreekt"],
            next_action="review",
        ),
    )

    message = _format_message(evaluated_ad)

    assert "Check Marktplaats specs" in message
    assert "Beko vrieskist" in message
    assert "EUR 100.00" in message
    assert "Lijkt bruikbaar" in message
    assert "205 liter" in message
    assert "diepte ontbreekt" in message


def test_message_heading_includes_profile_label_when_metadata_exists() -> None:
    evaluated_ad = EvaluatedAd(
        ad=Ad(id="m2", title="Freezer", url="https://www.marktplaats.nl/v/m2"),
        result=EvaluationResult(
            relevant=True,
            confidence=0.93,
            reason="Looks suitable.",
            next_action="notify",
        ),
        profile_id="freezers",
        profile_name="Freezers",
    )

    message = _format_message(evaluated_ad)

    assert "<b>[Freezers · freezers] Likely Marktplaats match</b>" in message


def test_message_heading_escapes_html_in_profile_label() -> None:
    evaluated_ad = EvaluatedAd(
        ad=Ad(id="m3", title="Freezer", url="https://www.marktplaats.nl/v/m3"),
        result=EvaluationResult(
            relevant=False,
            confidence=0.4,
            reason="Needs manual review.",
            next_action="review",
        ),
        profile_id="frozen&safe",
        profile_name='Freezers <script>alert("x")</script>',
    )

    message = _format_message(evaluated_ad)

    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in message
    assert "frozen&amp;safe" in message
    assert "<script>" not in message


def test_message_heading_uses_fallback_profile_when_metadata_is_missing() -> None:
    evaluated_ad = EvaluatedAd(
        ad=Ad(id="m4", title="Freezer", url="https://www.marktplaats.nl/v/m4"),
        result=EvaluationResult(
            relevant=True,
            confidence=0.75,
            reason="Likely a match.",
            next_action="notify",
        ),
    )

    message = _format_message(
        evaluated_ad,
        fallback_profile_id="freezers",
        fallback_profile_name="Freezers",
    )

    assert "<b>[Freezers · freezers] Likely Marktplaats match</b>" in message


@pytest.mark.asyncio
async def test_standalone_message_uses_active_profile_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notifier = TelegramNotifier(
        _settings(
            tmp_path,
            active_profile_id="freezers",
            active_profile_name="Freezers",
        )
    )
    captured: dict[str, str] = {}

    async def fake_send_text(text: str) -> TelegramSendResult:
        captured["text"] = text
        return TelegramSendResult(sent=True, message_id=1)

    monkeypatch.setattr(notifier, "_send_text", fake_send_text)

    result = await notifier.send_test_message()

    assert result.sent is True
    assert "<b>[Freezers · freezers] Marktplaats Ad Watcher test</b>" in captured["text"]


def test_ai_failure_alert_labels_and_escapes_listing_data() -> None:
    message = _format_ai_failure_alert(
        [
            EvaluationFailure(
                ad_id="m1",
                title="Freezer <broken>",
                url="https://example.test/?a=1&b=2",
                error="Model <unavailable>",
            )
        ],
        profile_id="freezers",
        profile_name="Freezers & keezer",
    )

    assert "[Freezers &amp; keezer · freezers] AI evaluation needs attention" in message
    assert "Freezer &lt;broken&gt;" in message
    assert "Model &lt;unavailable&gt;" in message
    assert "These are not recommendations" in message