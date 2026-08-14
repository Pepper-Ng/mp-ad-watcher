from __future__ import annotations

from marktplaats_ad_watcher.config import Settings
from marktplaats_ad_watcher.evaluation import DryRunEvaluator, Evaluator
from marktplaats_ad_watcher.marktplaats import MarktplaatsClient
from marktplaats_ad_watcher.model_providers import build_model_evaluator
from marktplaats_ad_watcher.runner import ProfileOrchestrator, Watcher
from marktplaats_ad_watcher.state import SeenStore
from marktplaats_ad_watcher.status import RuntimeStatusStore
from marktplaats_ad_watcher.telegram import TelegramNotifier


def build_watcher(
    settings: Settings,
    *,
    evaluator: Evaluator | None = None,
    status_store: RuntimeStatusStore | None = None,
) -> Watcher:
    selected_evaluator = evaluator
    if selected_evaluator is None:
        selected_evaluator = (
            DryRunEvaluator() if settings.dry_run else build_model_evaluator(settings)
        )

    return Watcher(
        settings=settings,
        marktplaats_client=MarktplaatsClient(
            timeout_seconds=settings.request_timeout_seconds,
            user_agent=settings.user_agent,
        ),
        evaluator=selected_evaluator,
        notifier=TelegramNotifier(settings),
        store=SeenStore(settings.state_file),
        status_store=status_store,
    )


def build_profile_orchestrator(settings: Settings) -> ProfileOrchestrator:
    """Build the profile-aware CLI runner while retaining ``build_watcher`` compatibility."""

    return ProfileOrchestrator(
        settings=settings,
        watcher_builder=lambda profile_settings, status_store: build_watcher(
            profile_settings,
            status_store=status_store,
        ),
    )