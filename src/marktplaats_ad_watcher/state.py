from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from marktplaats_ad_watcher.models import Ad, EvaluatedAd, EvaluationResult


class SeenStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._data = self._load()

    @property
    def is_empty(self) -> bool:
        return len(self._data["seen_ads"]) == 0

    def has_seen(self, ad_id: str) -> bool:
        return ad_id in self._data["seen_ads"]

    def model_failure_attempts(self, ad_id: str) -> int:
        failures = self._data.get("model_failures", {})
        record = failures.get(ad_id)
        if not isinstance(record, dict):
            return 0
        value = record.get("failure_count", 0)
        return value if isinstance(value, int) and value > 0 else 0

    def model_failures(self) -> list[dict[str, Any]]:
        failures = self._data.get("model_failures", {})
        if not isinstance(failures, dict):
            return []

        records = [
            {"ad_id": str(ad_id), **record}
            for ad_id, record in failures.items()
            if isinstance(record, dict)
        ]
        return sorted(
            records,
            key=lambda record: str(record.get("last_failed_at", "")),
            reverse=True,
        )

    def model_failure(self, ad_id: str) -> dict[str, Any] | None:
        record = self._data.get("model_failures", {}).get(ad_id)
        return {"ad_id": ad_id, **record} if isinstance(record, dict) else None

    def pending_model_failure_ads(self, *, interval: timedelta) -> list[Ad]:
        now = datetime.now(UTC)
        candidates: list[Ad] = []
        for ad_id, record in self._data.get("model_failures", {}).items():
            if not isinstance(record, dict) or record.get("exhausted"):
                continue
            next_retry = _parse_timestamp(record.get("next_retry_at"))
            last_failed = _parse_timestamp(record.get("last_failed_at"))
            if next_retry is not None and now < next_retry:
                continue
            if next_retry is None and last_failed is not None and now - last_failed < interval:
                continue
            title = record.get("title")
            url = record.get("url")
            if isinstance(title, str) and title.strip() and isinstance(url, str) and url.strip():
                candidates.append(Ad(id=str(ad_id), title=title, url=url))
        return candidates

    def discard_model_failure(self, ad_id: str) -> bool:
        failures = self._data.get("model_failures", {})
        if not isinstance(failures, dict) or ad_id not in failures:
            return False
        failures.pop(ad_id)
        self._save()
        return True

    def seen_ad(self, ad_id: str) -> dict[str, Any] | None:
        entry = self._data["seen_ads"].get(ad_id)
        return dict(entry) if isinstance(entry, dict) else None

    def hide_ad(self, ad_id: str, *, reason: str) -> bool:
        entry = self._data["seen_ads"].get(ad_id)
        if not isinstance(entry, dict):
            return False
        now = datetime.now(UTC).isoformat()
        entry["hidden_at"] = now
        entry["hidden_reason"] = reason
        self._save()
        return True

    def availability_check_candidates(self, *, interval: timedelta) -> list[Ad]:
        now = datetime.now(UTC)
        candidates: list[Ad] = []
        for ad_id, entry in self._data["seen_ads"].items():
            if not isinstance(entry, dict) or entry.get("availability") == "unavailable":
                continue
            result = entry.get("evaluation")
            if not isinstance(result, dict) or result.get("next_action") not in {"notify", "review"}:
                continue
            checked_at = _parse_timestamp(entry.get("last_availability_checked_at"))
            if checked_at is not None and now - checked_at < interval:
                continue
            title = entry.get("title")
            url = entry.get("url")
            if isinstance(title, str) and title.strip() and isinstance(url, str) and url.strip():
                candidates.append(Ad(id=str(ad_id), title=title, url=url))
        return candidates

    def mark_availability_checked(self, ad_id: str, *, available: bool) -> bool:
        entry = self._data["seen_ads"].get(ad_id)
        if not isinstance(entry, dict):
            return False
        now = datetime.now(UTC).isoformat()
        entry["last_availability_checked_at"] = now
        entry["availability"] = "available" if available else "unavailable"
        if not available:
            entry["hidden_at"] = now
            entry["hidden_reason"] = "listing_unavailable"
            entry["unavailable_at"] = now
        self._save()
        return True

    def mark_model_failure(
        self,
        ad: Ad,
        *,
        error: str,
        max_retries: int,
    ) -> tuple[int, bool]:
        failures = self._data.setdefault("model_failures", {})
        now = datetime.now(UTC).isoformat()
        existing = failures.get(ad.id)

        first_failed_at = now
        failure_count = 1
        if isinstance(existing, dict):
            existing_count = existing.get("failure_count")
            if isinstance(existing_count, int) and existing_count > 0:
                failure_count = existing_count + 1
            stored_first = existing.get("first_failed_at")
            if isinstance(stored_first, str) and stored_first.strip():
                first_failed_at = stored_first

        exhausted = failure_count > max_retries
        failures[ad.id] = {
            "title": ad.title,
            "url": ad.url,
            "first_failed_at": first_failed_at,
            "last_failed_at": now,
            "failure_count": failure_count,
            "last_error": error,
            "max_retries": max_retries,
            "exhausted": exhausted,
            "exhausted_at": now if exhausted else None,
            "next_retry_at": (
                None if exhausted else (datetime.now(UTC) + timedelta(hours=2)).isoformat()
            ),
        }

        if exhausted:
            self._data["seen_ads"][ad.id] = {
                "title": ad.title,
                "url": ad.url,
                "first_seen_at": now,
                "last_seen_at": now,
                "model_retry_exhausted": True,
                "model_failure_count": failure_count,
                "model_last_error": error,
            }

        self._save()
        return failure_count, exhausted

    def mark_seen(self, ad: Ad, result: EvaluationResult | None = None) -> None:
        entry: dict[str, Any] = {
            "title": ad.title,
            "url": ad.url,
            "first_seen_at": datetime.now(UTC).isoformat(),
        }
        if result is not None:
            entry["evaluation"] = result.model_dump(mode="json")
            entry["availability"] = "available"
            entry["last_availability_checked_at"] = datetime.now(UTC).isoformat()

        existing = self._data["seen_ads"].get(ad.id)
        if isinstance(existing, dict) and "first_seen_at" in existing:
            entry["first_seen_at"] = existing["first_seen_at"]
            entry["last_seen_at"] = datetime.now(UTC).isoformat()

        self._data["seen_ads"][ad.id] = entry
        model_failures = self._data.get("model_failures")
        if isinstance(model_failures, dict):
            model_failures.pop(ad.id, None)
        self._save()

    def mark_many_seen(self, ads: list[Ad]) -> None:
        for ad in ads:
            self._data["seen_ads"][ad.id] = {
                "title": ad.title,
                "url": ad.url,
                "first_seen_at": datetime.now(UTC).isoformat(),
                "bootstrapped": True,
            }
            model_failures = self._data.get("model_failures")
            if isinstance(model_failures, dict):
                model_failures.pop(ad.id, None)
        self._save()

    def append_result(self, path: Path, evaluated_ad: EvaluatedAd) -> None:
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)

        absent_profile_fields = {
            field
            for field in ("profile_id", "profile_name")
            if getattr(evaluated_ad, field) is None
        }
        with path.open("a", encoding="utf-8") as output:
            output.write(evaluated_ad.model_dump_json(exclude=absent_profile_fields) + "\n")

    def repair_budget_retry_exhaustion(self) -> tuple[int, Path | None]:
        budget_phrase = "ModelDailyLimitExceeded"
        repaired = 0
        for ad_id, entry in list(self._data["seen_ads"].items()):
            if not isinstance(entry, dict):
                continue
            if not entry.get("model_retry_exhausted"):
                continue
            if budget_phrase not in str(entry.get("model_last_error", "")):
                continue
            self._data["seen_ads"].pop(ad_id, None)
            failures = self._data.get("model_failures")
            if isinstance(failures, dict):
                failures.pop(ad_id, None)
            repaired += 1

        if repaired == 0:
            return 0, None

        backup_path = self._path.with_suffix(
            self._path.suffix + f".budget-repair-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.bak"
        )
        if self._path.exists():
            shutil.copy2(self._path, backup_path)
        self._save()
        return repaired, backup_path

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"seen_ads": {}, "model_failures": {}}

        with self._path.open("r", encoding="utf-8") as input_file:
            loaded = json.load(input_file)

        if not isinstance(loaded, dict) or not isinstance(loaded.get("seen_ads"), dict):
            raise ValueError(f"State file {self._path} does not have the expected shape.")

        if not isinstance(loaded.get("model_failures"), dict):
            loaded["model_failures"] = {}

        return loaded

    def _save(self) -> None:
        if not self._path.parent.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)

        temporary_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with temporary_path.open("w", encoding="utf-8") as output:
            json.dump(self._data, output, indent=2, sort_keys=True)
            output.write("\n")

        temporary_path.replace(self._path)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
