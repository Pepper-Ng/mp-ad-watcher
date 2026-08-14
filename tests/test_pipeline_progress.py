from __future__ import annotations

from pathlib import Path

from marktplaats_ad_watcher.models import Ad, EvaluatedAd, EvaluationResult
from marktplaats_ad_watcher.pipeline_progress import PipelineProgressStore


def _evaluated_ad() -> EvaluatedAd:
    return EvaluatedAd(
        ad=Ad(
            id="m1",
            title="Freezer chest",
            url="https://www.marktplaats.nl/v/m1",
        ),
        result=EvaluationResult(
            relevant=True,
            confidence=0.8,
            reason="Promising size.",
            next_action="review",
        ),
    )


def test_pipeline_progress_persists_ai_result_and_telegram_delivery(tmp_path: Path) -> None:
    path = tmp_path / "pipeline_progress.json"
    store = PipelineProgressStore(path)

    saved = store.save_ai_result(_evaluated_ad())
    reloaded = PipelineProgressStore(path).get("m1")
    sent = store.mark_telegram_sent("m1", message_id=42)

    assert saved.telegram_sent is False
    assert reloaded is not None
    assert reloaded.evaluated_ad.result.next_action == "review"
    assert sent.telegram_sent is True
    assert sent.telegram_message_id == 42
    assert sent.telegram_sent_at is not None


def test_saving_new_ai_result_resets_telegram_progress(tmp_path: Path) -> None:
    path = tmp_path / "pipeline_progress.json"
    store = PipelineProgressStore(path)
    store.save_ai_result(_evaluated_ad())
    store.mark_telegram_sent("m1", message_id=42)

    replaced = store.save_ai_result(_evaluated_ad())

    assert replaced.telegram_sent is False
    assert replaced.telegram_message_id is None


def test_pipeline_progress_imports_production_evaluations(tmp_path: Path) -> None:
    progress_path = tmp_path / "pipeline_progress.json"
    evaluations_path = tmp_path / "evaluations.jsonl"
    evaluations_path.write_text(_evaluated_ad().model_dump_json() + "\n", encoding="utf-8")

    records = PipelineProgressStore(progress_path).sync_evaluations(evaluations_path)

    assert len(records) == 1
    assert records[0].source == "production"
    assert records[0].telegram_sent is None
    assert records[0].evaluated_ad.ad.id == "m1"
