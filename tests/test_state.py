from __future__ import annotations

from marktplaats_ad_watcher.config import parse_dotenv, write_dotenv
from marktplaats_ad_watcher.models import Ad, EvaluationResult
from marktplaats_ad_watcher.state import SeenStore


def test_seen_store_persists_marked_ads(tmp_path) -> None:
    state_path = tmp_path / "seen_ads.json"
    ad = Ad(id="m1", title="Useful item", url="https://www.marktplaats.nl/v/m1")
    result = EvaluationResult(
        relevant=True,
        confidence=0.9,
        reason="Matches the use case.",
        signals=["complete listing"],
        concerns=[],
        next_action="notify",
    )

    store = SeenStore(state_path)
    assert store.is_empty
    assert not store.has_seen("m1")

    store.mark_seen(ad, result)

    reloaded = SeenStore(state_path)
    assert not reloaded.is_empty
    assert reloaded.has_seen("m1")


def test_dotenv_roundtrip_preserves_multiline_prompt_and_quotes(tmp_path) -> None:
    path = tmp_path / "settings.env"
    prompt = "Line one\nLine two with \"quotes\""

    write_dotenv(path, {"MARKTPLAATS_USE_CASE": prompt})

    assert parse_dotenv(path)["MARKTPLAATS_USE_CASE"] == prompt


def test_budget_retry_repair_restores_incorrectly_exhausted_ads(tmp_path) -> None:
        state_path = tmp_path / "seen_ads.json"
        state_path.write_text(
                """
{
    "seen_ads": {
        "m1": {
            "title": "Freezer",
            "url": "https://example.test/m1",
            "model_retry_exhausted": true,
            "model_last_error": "ModelDailyLimitExceeded: Daily model request limit reached (30/30)."
        },
        "m2": {
            "title": "Real exhausted",
            "url": "https://example.test/m2",
            "model_retry_exhausted": true,
            "model_last_error": "ValueError: Model returned no final assistant content."
        }
    },
    "model_failures": {
        "m1": {"last_error": "ModelDailyLimitExceeded: Daily model request limit reached (30/30).", "failure_count": 3},
        "m2": {"last_error": "ValueError: Model returned no final assistant content.", "failure_count": 3}
    }
}
""".strip(),
                encoding="utf-8",
        )

        repaired, backup_path = SeenStore(state_path).repair_budget_retry_exhaustion()

        assert repaired == 1
        assert backup_path is not None and backup_path.exists()
        reloaded = SeenStore(state_path)
        assert not reloaded.has_seen("m1")
        assert reloaded.has_seen("m2")
