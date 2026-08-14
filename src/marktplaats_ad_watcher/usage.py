from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

_USAGE_LOCK = threading.Lock()
_IN_FLIGHT: dict[str, int] = {}


class ModelDailyLimitExceeded(RuntimeError):
    def __init__(self, *, used: int, limit: int, reset_at: datetime) -> None:
        self.used = used
        self.limit = limit
        self.reset_at = reset_at
        super().__init__(
            f"Daily model request limit reached ({used}/{limit}). "
            f"The budget resets at {reset_at.isoformat()}."
        )


@dataclass(frozen=True)
class ModelUsageSnapshot:
    day: date
    used: int
    limit: int
    in_flight: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used - self.in_flight)

    @property
    def reset_at(self) -> datetime:
        return datetime.combine(self.day + timedelta(days=1), time.min, tzinfo=UTC)


class ModelUsageStore:
    def __init__(self, path: Path, *, default_limit: int = 30) -> None:
        if default_limit < 1:
            raise ValueError("The default daily model request limit must be positive.")
        self._path = path
        self._default_limit = default_limit
        self._key = str(path.resolve())

    def snapshot(self) -> ModelUsageSnapshot:
        with _USAGE_LOCK:
            state = self._load_current()
            self._save(state)
            return self._snapshot(state)

    def reserve(self) -> ModelUsageSnapshot:
        reservation = self.acquire()
        return reservation.commit()

    def acquire(self) -> ModelUsageReservation:
        with _USAGE_LOCK:
            state = self._load_current()
            snapshot = self._snapshot(state)
            if snapshot.used + snapshot.in_flight >= snapshot.limit:
                self._save(state)
                raise ModelDailyLimitExceeded(
                    used=snapshot.used,
                    limit=snapshot.limit,
                    reset_at=snapshot.reset_at,
                )
            _IN_FLIGHT[self._key] = snapshot.in_flight + 1
            return ModelUsageReservation(self)

    def set_limit(self, limit: int) -> ModelUsageSnapshot:
        if limit < 1 or limit > 1000:
            raise ValueError("The daily model request limit must be between 1 and 1000.")
        with _USAGE_LOCK:
            state = self._load_current()
            state["limit"] = limit
            self._save(state)
            return self._snapshot(state)

    def reset_today(self) -> ModelUsageSnapshot:
        with _USAGE_LOCK:
            state = self._load_current()
            state["used"] = 0
            self._save(state)
            return self._snapshot(state)

    def _finish(self, *, success: bool) -> ModelUsageSnapshot:
        with _USAGE_LOCK:
            in_flight = _IN_FLIGHT.get(self._key, 0)
            if in_flight > 1:
                _IN_FLIGHT[self._key] = in_flight - 1
            else:
                _IN_FLIGHT.pop(self._key, None)
            state = self._load_current()
            if success:
                state["used"] = int(state["used"]) + 1
                self._save(state)
            return self._snapshot(state)

    def _load_current(self) -> dict[str, Any]:
        today = datetime.now(UTC).date()
        loaded: dict[str, Any] = {}
        if self._path.exists():
            try:
                candidate = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                candidate = None
            if isinstance(candidate, dict):
                loaded = candidate

        limit = loaded.get("limit")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            limit = self._default_limit
        used = loaded.get("used")
        if not isinstance(used, int) or isinstance(used, bool) or used < 0:
            used = 0
        if loaded.get("day") != today.isoformat():
            used = 0

        return {"day": today.isoformat(), "used": used, "limit": limit}

    def _save(self, state: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._path)

    def _snapshot(self, state: dict[str, Any]) -> ModelUsageSnapshot:
        return ModelUsageSnapshot(
            day=date.fromisoformat(str(state["day"])),
            used=int(state["used"]),
            limit=int(state["limit"]),
            in_flight=_IN_FLIGHT.get(self._key, 0),
        )


class ModelUsageReservation:
    def __init__(self, store: ModelUsageStore) -> None:
        self._store = store
        self._active = True

    def commit(self) -> ModelUsageSnapshot:
        if not self._active:
            raise RuntimeError("This model usage reservation is already closed.")
        self._active = False
        return self._store._finish(success=True)

    def release(self) -> ModelUsageSnapshot:
        if not self._active:
            raise RuntimeError("This model usage reservation is already closed.")
        self._active = False
        return self._store._finish(success=False)
