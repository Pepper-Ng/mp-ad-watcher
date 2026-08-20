from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from marktplaats_ad_watcher.models import WatcherRunSummary


class RuntimeStatus(BaseModel):
    is_running: bool = False
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    next_run_at: datetime | None = None
    last_error: str | None = None
    last_summary: WatcherRunSummary | None = None
    total_runs: int = 0
    total_errors: int = 0
    total_fetched: int = 0
    total_kept: int = 0
    total_filtered: int = 0
    total_new: int = 0
    total_evaluated: int = 0
    total_notified: int = 0
    total_ignored: int = 0
    total_reviewed: int = 0
    total_notify_actions: int = 0
    total_evaluation_failed: int = 0
    last_ai_failure_alert_signature: str | None = None
    last_ai_failure_alert_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RuntimeStatusStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._status = self._load()

    def read(self) -> RuntimeStatus:
        return self._status

    def mark_started(self) -> None:
        self._status.is_running = True
        self._status.last_started_at = datetime.now(UTC)
        self._status.last_error = None
        self._touch()

    def mark_finished(self, summary: WatcherRunSummary) -> None:
        self._status.is_running = False
        self._status.last_finished_at = datetime.now(UTC)
        self._status.last_summary = summary
        self._status.total_runs += 1
        self._status.total_fetched += summary.fetched_count
        self._status.total_kept += summary.kept_count
        self._status.total_filtered += summary.filtered_count
        self._status.total_new += summary.new_count
        self._status.total_evaluated += summary.evaluated_count
        self._status.total_notified += summary.notified_count
        self._status.total_ignored += summary.ignored_count
        self._status.total_reviewed += summary.review_count
        self._status.total_notify_actions += summary.notify_action_count
        self._status.total_evaluation_failed += summary.evaluation_failed_count
        if not _model_failure_signature(summary):
            self._status.last_ai_failure_alert_signature = None
            self._status.last_ai_failure_alert_at = None
        self._touch()

    def mark_failed(self, error: Exception) -> None:
        self._status.is_running = False
        self._status.last_finished_at = datetime.now(UTC)
        self._status.last_error = f"{type(error).__name__}: {error}"
        self._status.total_errors += 1
        self._touch()

    def set_next_run_at(self, value: datetime | None) -> None:
        self._status.next_run_at = value
        self._touch()

    def resolve_evaluation_failure(self, ad_id: str) -> bool:
        summary = self._status.last_summary
        if summary is None:
            return False
        remaining = [failure for failure in summary.evaluation_failures if failure.ad_id != ad_id]
        if len(remaining) == len(summary.evaluation_failures):
            return False
        summary.evaluation_failures = remaining
        summary.evaluation_failed_count = len(remaining)
        self._touch()
        return True

    def should_send_ai_failure_alert(self, summary: WatcherRunSummary) -> bool:
        signature = _model_failure_signature(summary)
        return bool(signature and signature != self._status.last_ai_failure_alert_signature)

    def mark_ai_failure_alert_sent(self, summary: WatcherRunSummary) -> None:
        signature = _model_failure_signature(summary)
        if not signature:
            return
        self._status.last_ai_failure_alert_signature = signature
        self._status.last_ai_failure_alert_at = datetime.now(UTC)
        self._touch()

    def _load(self) -> RuntimeStatus:
        if not self._path.exists():
            return RuntimeStatus()

        with self._path.open("r", encoding="utf-8") as input_file:
            loaded: Any = json.load(input_file)

        return RuntimeStatus.model_validate(loaded)

    def _touch(self) -> None:
        self._status.updated_at = datetime.now(UTC)
        self._save()

    def _save(self) -> None:
        if not self._path.parent.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)

        temporary_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with temporary_path.open("w", encoding="utf-8") as output:
            output.write(self._status.model_dump_json(indent=2))
            output.write("\n")

        temporary_path.replace(self._path)


def _model_failure_signature(summary: WatcherRunSummary) -> str | None:
    model_failures = [
        failure for failure in summary.evaluation_failures if failure.stage == "model"
    ]
    if not model_failures:
        return None

    parts = [
        f"{failure.ad_id}|{_stable_error_text(failure.error)}"
        for failure in sorted(model_failures, key=lambda failure: failure.ad_id)
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _stable_error_text(value: str) -> str:
    return re.sub(r"\s*\(request id: [^)]+\)", "", value, flags=re.IGNORECASE).strip()