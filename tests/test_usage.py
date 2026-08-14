from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from marktplaats_ad_watcher.usage import ModelDailyLimitExceeded, ModelUsageStore


def test_model_usage_defaults_to_thirty_and_persists_reservations(tmp_path: Path) -> None:
    path = tmp_path / "model_usage.json"
    store = ModelUsageStore(path)

    initial = store.snapshot()
    reserved = store.reserve()
    reloaded = ModelUsageStore(path).snapshot()

    assert initial.used == 0
    assert initial.limit == 30
    assert reserved.used == 1
    assert reloaded.used == 1
    assert reloaded.remaining == 29


def test_model_usage_limit_is_enforced_and_increase_applies_immediately(tmp_path: Path) -> None:
    path = tmp_path / "model_usage.json"
    existing_evaluator_store = ModelUsageStore(path, default_limit=1)
    existing_evaluator_store.reserve()

    with pytest.raises(ModelDailyLimitExceeded):
        existing_evaluator_store.reserve()

    ModelUsageStore(path).set_limit(2)
    after_increase = existing_evaluator_store.reserve()

    assert after_increase.used == 2
    assert after_increase.limit == 2


def test_failed_model_reservation_releases_without_counting(tmp_path: Path) -> None:
    path = tmp_path / "model_usage.json"
    store = ModelUsageStore(path)

    reservation = store.acquire()
    during = store.snapshot()
    after = reservation.release()

    assert during.used == 0
    assert during.in_flight == 1
    assert after.used == 0
    assert after.in_flight == 0


def test_model_usage_can_be_reset_for_current_day(tmp_path: Path) -> None:
    path = tmp_path / "model_usage.json"
    store = ModelUsageStore(path)
    store.reserve()
    store.reserve()

    reset = store.reset_today()

    assert reset.used == 0
    assert reset.limit == 30


def test_model_usage_reservations_are_structurally_serialized(tmp_path: Path) -> None:
    path = tmp_path / "model_usage.json"
    ModelUsageStore(path).set_limit(5)

    def reserve() -> bool:
        try:
            ModelUsageStore(path).reserve()
        except ModelDailyLimitExceeded:
            return False
        return True

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(lambda _: reserve(), range(20)))

    assert sum(results) == 5
    assert ModelUsageStore(path).snapshot().used == 5


def test_model_usage_resets_when_stored_day_is_old(tmp_path: Path) -> None:
    path = tmp_path / "model_usage.json"
    yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    path.write_text(
        json.dumps({"day": yesterday, "used": 20, "limit": 40}),
        encoding="utf-8",
    )

    snapshot = ModelUsageStore(path).snapshot()

    assert snapshot.used == 0
    assert snapshot.limit == 40
    assert snapshot.day == datetime.now(UTC).date()
