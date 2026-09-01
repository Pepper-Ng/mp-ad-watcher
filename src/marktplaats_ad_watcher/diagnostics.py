from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path


class DiagnosticHistoryStore:
    """Append-only persistent history of attention-worthy operational events."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, event: Mapping[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(dict(event), ensure_ascii=False, sort_keys=True))
            output.write("\n")

    def read_recent(self, *, limit: int = 200) -> list[dict[str, str]]:
        if limit < 1 or not self._path.exists():
            return []

        entries: list[dict[str, str]] = []
        with self._path.open("r", encoding="utf-8") as input_file:
            for line in input_file:
                try:
                    loaded = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(loaded, dict):
                    continue
                entry = {
                    key: value
                    for key, value in loaded.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
                if {"timestamp", "level", "logger", "message"} <= entry.keys():
                    entry.setdefault("detail", "")
                    entries.append(entry)

        return entries[-limit:]
