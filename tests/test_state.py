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
