from __future__ import annotations

from marktplaats_ad_watcher.models import Ad, EvaluatedAd, EvaluationResult
from marktplaats_ad_watcher.telegram import _format_message


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