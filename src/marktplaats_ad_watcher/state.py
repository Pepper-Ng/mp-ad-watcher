from __future__ import annotations

import json
from datetime import UTC, datetime
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

    def mark_seen(self, ad: Ad, result: EvaluationResult | None = None) -> None:
        entry: dict[str, Any] = {
            "title": ad.title,
            "url": ad.url,
            "first_seen_at": datetime.now(UTC).isoformat(),
        }
        if result is not None:
            entry["evaluation"] = result.model_dump(mode="json")

        existing = self._data["seen_ads"].get(ad.id)
        if isinstance(existing, dict) and "first_seen_at" in existing:
            entry["first_seen_at"] = existing["first_seen_at"]
            entry["last_seen_at"] = datetime.now(UTC).isoformat()

        self._data["seen_ads"][ad.id] = entry
        self._save()

    def mark_many_seen(self, ads: list[Ad]) -> None:
        for ad in ads:
            self._data["seen_ads"][ad.id] = {
                "title": ad.title,
                "url": ad.url,
                "first_seen_at": datetime.now(UTC).isoformat(),
                "bootstrapped": True,
            }
        self._save()

    def append_result(self, path: Path, evaluated_ad: EvaluatedAd) -> None:
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)

        with path.open("a", encoding="utf-8") as output:
            output.write(evaluated_ad.model_dump_json() + "\n")

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"seen_ads": {}}

        with self._path.open("r", encoding="utf-8") as input_file:
            loaded = json.load(input_file)

        if not isinstance(loaded, dict) or not isinstance(loaded.get("seen_ads"), dict):
            raise ValueError(f"State file {self._path} does not have the expected shape.")

        return loaded

    def _save(self) -> None:
        if not self._path.parent.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)

        temporary_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with temporary_path.open("w", encoding="utf-8") as output:
            json.dump(self._data, output, indent=2, sort_keys=True)
            output.write("\n")

        temporary_path.replace(self._path)
