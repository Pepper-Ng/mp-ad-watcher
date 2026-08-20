from __future__ import annotations

import pytest

from marktplaats_ad_watcher.evaluation import parse_evaluation


def test_parse_evaluation_repairs_unquoted_model_property_names() -> None:
    result = parse_evaluation(
        """
        {
          "relevant": false,
          "confidence": 0.85,
          "reason": "The freezer is explicitly broken.",
          signals: ["broken"],
          "concerns": ["not working"],
          "next_action": "ignore"
        }
        """
    )

    assert result.relevant is False
    assert result.confidence == 0.85
    assert result.signals == ["broken"]
    assert result.next_action == "ignore"


def test_parse_evaluation_still_rejects_response_missing_required_fields() -> None:
    with pytest.raises(ValueError):
        parse_evaluation('{relevant: true, confidence: 0.9}')
