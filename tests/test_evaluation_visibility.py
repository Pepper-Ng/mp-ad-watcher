from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
import pytest

from marktplaats_ad_watcher.config import write_dotenv
from marktplaats_ad_watcher.models import Ad, EvaluatedAd, EvaluationResult
from marktplaats_ad_watcher.state import SeenStore
from marktplaats_ad_watcher.web import create_web_app


def _result(action: Literal["notify", "ignore", "review"]) -> EvaluationResult:
    return EvaluationResult(
        relevant=action != "ignore",
        confidence=0.8,
        reason="Recorded evaluation.",
        next_action=action,
    )


def _save_evaluation(
    store: SeenStore,
    results_file: Path,
    *,
    ad_id: str,
    action: Literal["notify", "ignore", "review"],
    age_days: int,
) -> None:
    ad = Ad(id=ad_id, title=f"{action} {ad_id}", url=f"https://example.test/{ad_id}")
    evaluation = EvaluatedAd(
        ad=ad,
        result=_result(action),
        evaluated_at=datetime.now(UTC) - timedelta(days=age_days),
    )
    store.append_result(results_file, evaluation)
    store.mark_seen(ad, evaluation.result)


@pytest.mark.asyncio
async def test_evaluation_page_filters_old_results_and_hides_confirmed_ad(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "seen_ads.json"
    results_file = tmp_path / "evaluations.jsonl"
    env_file = tmp_path / "settings.env"
    write_dotenv(
        env_file,
        {
            "WEB_ADMIN_TOKEN": "admin-token",
            "STATE_FILE": str(state_file),
            "RESULTS_FILE": str(results_file),
        },
    )
    store = SeenStore(state_file)
    _save_evaluation(store, results_file, ad_id="recent-ignore", action="ignore", age_days=6)
    _save_evaluation(store, results_file, ad_id="old-ignore", action="ignore", age_days=8)
    _save_evaluation(store, results_file, ad_id="recent-review", action="review", age_days=13)
    _save_evaluation(store, results_file, ad_id="old-review", action="review", age_days=15)

    app = create_web_app(env_file=env_file, dry_run=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        default_page = await client.get("/evaluations?token=admin-token")
        history_page = await client.get("/evaluations?token=admin-token&history=all")
        confirmation = await client.get("/evaluations/recent-review/hide?token=admin-token")
        hidden = await client.post(
            "/evaluations/recent-review/hide?token=admin-token",
            follow_redirects=False,
        )
        after_hide = await client.get("/evaluations?token=admin-token&history=all")
        seen_page = await client.get("/seen?token=admin-token")

    assert "recent-ignore" in default_page.text
    assert "recent-review" in default_page.text
    assert "old-ignore" not in default_page.text
    assert "old-review" not in default_page.text
    assert "old-review" in history_page.text
    assert "old-ignore" not in history_page.text
    assert "Hide evaluation permanently?" in confirmation.text
    assert hidden.status_code == 303
    assert "recent-review" not in after_hide.text
    assert "recent-review" not in seen_page.text
