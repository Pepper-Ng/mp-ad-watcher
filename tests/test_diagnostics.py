from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from marktplaats_ad_watcher.config import write_dotenv
from marktplaats_ad_watcher.diagnostics import DiagnosticHistoryStore
from marktplaats_ad_watcher.models import Ad
from marktplaats_ad_watcher.state import SeenStore
from marktplaats_ad_watcher.web import ProfileSelection, RecentLogBuffer, _diagnostic_entries, create_web_app


def _event(*, level: str, message: str) -> dict[str, str]:
    return {
        "timestamp": "2026-09-01T08:00:00+00:00",
        "level": level,
        "logger": "marktplaats_ad_watcher.runner",
        "message": message,
        "detail": "",
    }


def test_diagnostic_history_persists_warning_and_error_events(tmp_path: Path) -> None:
    history = DiagnosticHistoryStore(tmp_path / "diagnostic_events.jsonl")
    buffer = RecentLogBuffer(history_store=history)

    buffer.emit(logging.LogRecord("watcher", logging.INFO, "", 0, "Scheduled run", (), None))
    buffer.emit(logging.LogRecord("watcher", logging.WARNING, "", 0, "Model is retrying", (), None))
    buffer.emit(logging.LogRecord("watcher", logging.ERROR, "", 0, "Model failed", (), None))

    assert [entry["message"] for entry in history.read_recent()] == [
        "Model is retrying",
        "Model failed",
    ]


def test_diagnostic_entries_hide_routine_info_messages() -> None:
    entries = [
        _event(level="INFO", message="Scheduled profile execution"),
        _event(level="WARNING", message="[Freezers · freezers] Retrying model"),
        _event(level="ERROR", message="[Bicycles · bicycles] Model failed"),
    ]

    all_entries = _diagnostic_entries(entries, ProfileSelection(registry=None, profile=None, is_all=True))

    assert [entry["level"] for entry in all_entries] == ["WARNING", "ERROR"]


@pytest.mark.asyncio
async def test_diagnostics_page_shows_persistent_history_and_failure_state(tmp_path: Path) -> None:
    state_file = tmp_path / "seen_ads.json"
    results_file = tmp_path / "evaluations.jsonl"
    write_dotenv(
        tmp_path / "settings.env",
        {
            "WEB_ADMIN_TOKEN": "admin-token",
            "STATE_FILE": str(state_file),
            "RESULTS_FILE": str(results_file),
        },
    )
    store = SeenStore(state_file)
    pending = Ad(id="pending", title="Pending freezer", url="https://example.test/pending")
    abandoned = Ad(id="abandoned", title="Abandoned freezer", url="https://example.test/abandoned")
    store.mark_model_failure(
        pending,
        error="ModelOutputError: Invalid JSON response.",
        max_retries=2,
    )
    for _ in range(3):
        store.mark_model_failure(
            abandoned,
            error="ModelProviderError: HTTP 503.",
            max_retries=2,
        )
    DiagnosticHistoryStore(tmp_path / "diagnostic_events.jsonl").append(
        _event(level="ERROR", message="[legacy] ModelOutputError: Invalid JSON response.")
    )

    app = create_web_app(env_file=tmp_path / "settings.env", dry_run=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/diagnostics?token=admin-token")

    assert page.status_code == 200
    assert "Pending and abandoned AI failures" in page.text
    assert "Pending freezer" in page.text
    assert "Abandoned freezer" in page.text
    assert "<strong>1</strong> pending" in page.text
    assert "<strong>1</strong> abandoned" in page.text
    assert "Retries only if this listing appears again." in page.text
    assert "No automatic retries remain." in page.text
    assert "Persistent error history" in page.text
    assert "Current-session attention events" in page.text
    assert "Scheduled profile execution" not in page.text
