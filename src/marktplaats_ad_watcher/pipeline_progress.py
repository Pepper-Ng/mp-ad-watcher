from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from marktplaats_ad_watcher.models import EvaluatedAd

_PROGRESS_LOCK = threading.Lock()


class PipelineProgressRecord(BaseModel):
    evaluated_ad: EvaluatedAd
    tested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: Literal["manual_test", "production"] = "manual_test"
    telegram_sent: bool | None = False
    telegram_sent_at: datetime | None = None
    telegram_message_id: int | None = None


class PipelineProgressStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def records(self) -> list[PipelineProgressRecord]:
        with _PROGRESS_LOCK:
            records = self._load()
        return sorted(records.values(), key=lambda record: record.tested_at, reverse=True)

    def get(self, ad_id: str) -> PipelineProgressRecord | None:
        with _PROGRESS_LOCK:
            return self._load().get(ad_id)

    def save_ai_result(self, evaluated_ad: EvaluatedAd) -> PipelineProgressRecord:
        with _PROGRESS_LOCK:
            records = self._load()
            record = PipelineProgressRecord(evaluated_ad=evaluated_ad, source="manual_test")
            records[evaluated_ad.ad.id] = record
            self._save(records)
            return record

    def mark_telegram_sent(
        self,
        ad_id: str,
        *,
        message_id: int | None,
        profile_id: str | None = None,
        profile_name: str | None = None,
    ) -> PipelineProgressRecord:
        with _PROGRESS_LOCK:
            records = self._load()
            record = records.get(ad_id)
            if record is None:
                raise ValueError("No saved AI result exists for this ad.")
            evaluated_ad = record.evaluated_ad
            profile_updates: dict[str, str] = {}
            if evaluated_ad.profile_id is None and profile_id is not None:
                profile_updates["profile_id"] = profile_id
            if evaluated_ad.profile_name is None and profile_name is not None:
                profile_updates["profile_name"] = profile_name
            if profile_updates:
                evaluated_ad = evaluated_ad.model_copy(update=profile_updates)
            updated = record.model_copy(
                update={
                    "telegram_sent": True,
                    "telegram_sent_at": datetime.now(UTC),
                    "telegram_message_id": message_id,
                    "evaluated_ad": evaluated_ad,
                }
            )
            records[ad_id] = updated
            self._save(records)
            return updated

    def sync_evaluations(self, path: Path) -> list[PipelineProgressRecord]:
        if not path.exists():
            return self.records()
        with _PROGRESS_LOCK:
            records = self._load()
            changed = False
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    evaluated_ad = EvaluatedAd.model_validate_json(line)
                except ValueError:
                    continue
                if evaluated_ad.ad.id in records:
                    continue
                records[evaluated_ad.ad.id] = PipelineProgressRecord(
                    evaluated_ad=evaluated_ad,
                    tested_at=evaluated_ad.evaluated_at,
                    source="production",
                    telegram_sent=None,
                )
                changed = True
            if changed:
                self._save(records)
        return sorted(records.values(), key=lambda record: record.tested_at, reverse=True)

    def _load(self) -> dict[str, PipelineProgressRecord]:
        if not self._path.exists():
            return {}
        try:
            loaded: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(loaded, dict):
            return {}

        raw_records = loaded.get("records", {})
        if not isinstance(raw_records, dict):
            return {}
        records: dict[str, PipelineProgressRecord] = {}
        for ad_id, value in raw_records.items():
            try:
                records[str(ad_id)] = PipelineProgressRecord.model_validate(value)
            except (TypeError, ValueError):
                continue
        return records

    def _save(self, records: dict[str, PipelineProgressRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "records": {
                ad_id: record.model_dump(mode="json") for ad_id, record in records.items()
            }
        }
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._path)
