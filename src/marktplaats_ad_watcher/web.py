from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import deque
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from marktplaats_ad_watcher.config import Settings, parse_dotenv, write_dotenv
from marktplaats_ad_watcher.factory import build_watcher
from marktplaats_ad_watcher.marktplaats import MarktplaatsClient
from marktplaats_ad_watcher.model_config import (
    PROVIDER_PRESETS,
    REASONING_EFFORTS,
    provider_preset,
    resolved_model_environment,
)
from marktplaats_ad_watcher.model_providers import ModelProviderError, build_model_evaluator
from marktplaats_ad_watcher.models import Ad, EvaluatedAd, WatcherRunSummary
from marktplaats_ad_watcher.pipeline_progress import (
    PipelineProgressRecord,
    PipelineProgressStore,
)
from marktplaats_ad_watcher.state import SeenStore
from marktplaats_ad_watcher.status import RuntimeStatus, RuntimeStatusStore
from marktplaats_ad_watcher.telegram import TelegramNotifier
from marktplaats_ad_watcher.usage import (
    ModelDailyLimitExceeded,
    ModelUsageSnapshot,
    ModelUsageStore,
)

EDITABLE_KEYS = [
    "MARKTPLAATS_SEARCH_URL",
    "MARKTPLAATS_USE_CASE",
    "BOOTSTRAP_EXISTING_ADS",
    "EXCLUDE_ADMARKT_ADS",
    "POLL_INTERVAL_SECONDS",
    "MAX_ADS_PER_POLL",
    "NOTIFY_MIN_CONFIDENCE",
    "REVIEW_MIN_CONFIDENCE",
    "NOTIFY_REVIEW_ACTIONS",
    "MODEL_PROVIDER",
    "MODEL_API_KEY",
    "MODEL_BASE_URL",
    "MODEL_NAME",
    "MODEL_TEMPERATURE",
    "MODEL_MAX_TOKENS",
    "MODEL_REASONING_EFFORT",
    "MODEL_JSON_MODE",
    "SEND_IMAGE_CONTENT_TO_MODEL",
    "MAX_IMAGES_FOR_MODEL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_DISABLE_WEB_PAGE_PREVIEW",
    "STATE_FILE",
    "RESULTS_FILE",
    "STATUS_FILE",
    "REQUEST_TIMEOUT_SECONDS",
    "USER_AGENT",
    "WEB_ADMIN_TOKEN",
]

BOOLEAN_KEYS = {
    "BOOTSTRAP_EXISTING_ADS",
    "EXCLUDE_ADMARKT_ADS",
    "NOTIFY_REVIEW_ACTIONS",
    "MODEL_JSON_MODE",
    "SEND_IMAGE_CONTENT_TO_MODEL",
    "TELEGRAM_DISABLE_WEB_PAGE_PREVIEW",
}

SECRET_KEYS = {"MODEL_API_KEY", "TELEGRAM_BOT_TOKEN", "WEB_ADMIN_TOKEN"}
LEGACY_MODEL_KEYS = {
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_TEMPERATURE",
    "DEEPSEEK_MAX_TOKENS",
}
CONFIG_DEFAULTS = {
    "BOOTSTRAP_EXISTING_ADS": "true",
    "EXCLUDE_ADMARKT_ADS": "true",
    "POLL_INTERVAL_SECONDS": "600",
    "MAX_ADS_PER_POLL": "30",
    "NOTIFY_MIN_CONFIDENCE": "0.65",
    "REVIEW_MIN_CONFIDENCE": "0",
    "NOTIFY_REVIEW_ACTIONS": "true",
    "SEND_IMAGE_CONTENT_TO_MODEL": "false",
    "MAX_IMAGES_FOR_MODEL": "3",
    "TELEGRAM_DISABLE_WEB_PAGE_PREVIEW": "false",
    "STATE_FILE": "data/seen_ads.json",
    "RESULTS_FILE": "data/evaluations.jsonl",
    "STATUS_FILE": "data/runtime_status.json",
    "REQUEST_TIMEOUT_SECONDS": "20",
    "USER_AGENT": "marktplaats-ad-watcher/0.1 (+local personal watcher)",
}
LOGGER = logging.getLogger(__name__)
ERROR_RETRY_SECONDS = 60
DEPLOYMENT_ENV_KEYS = {"WEB_ADMIN_TOKEN"}


class RecentLogBuffer(logging.Handler):
    def __init__(self, *, maximum: int = 200) -> None:
        super().__init__(level=logging.INFO)
        self.entries: deque[dict[str, str]] = deque(maxlen=maximum)

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if record.exc_info and record.exc_info[1]:
            error = record.exc_info[1]
            message += f" — {type(error).__name__}: {error}"
        message = _redact_diagnostic_text(message)
        detail = _redact_diagnostic_text(str(getattr(record, "diagnostic_detail", "")))
        self.entries.append(
            {
                "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": message[:1200],
                "detail": detail[:8000],
            }
        )


def _redact_diagnostic_text(value: str) -> str:
    value = re.sub(r"(?i)([?&]token=)[^&\s\"']+", r"\1[REDACTED]", value)
    return re.sub(
        r"(?i)((?:authorization|password|token|api[_-]?key)\s*[:=]\s*)\S+",
        r"\1[REDACTED]",
        value,
    )


class WatcherService:
    def __init__(self, *, env_file: Path, dry_run: bool) -> None:
        self._env_file = env_file
        self._dry_run = dry_run
        self._run_lock = asyncio.Lock()
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._manual_tasks: set[asyncio.Task[None]] = set()
        self._stopping = False
        self._preview_ads: dict[str, Ad] = {}
        self._preview_fetched_at: datetime | None = None
        self._preview_counts = (0, 0, 0)

    @property
    def env_file(self) -> Path:
        return self._env_file

    def read_config(self) -> dict[str, str]:
        values = parse_dotenv(self._env_file)
        for key in DEPLOYMENT_ENV_KEYS:
            if key not in values and key in os.environ:
                values[key] = os.environ[key]
        for key, default in CONFIG_DEFAULTS.items():
            if not values.get(key, "").strip():
                values[key] = default
        return resolved_model_environment(values)

    def admin_token(self) -> str | None:
        value = self.read_config().get("WEB_ADMIN_TOKEN", "").strip()
        return value if _secret_is_configured(value) else None

    def status_store(self) -> RuntimeStatusStore:
        values = self.read_config()
        status_file = Path(values.get("STATUS_FILE", "data/runtime_status.json"))
        return RuntimeStatusStore(status_file)

    def model_usage_store(self) -> ModelUsageStore:
        values = self.read_config()
        results_file = Path(values["RESULTS_FILE"])
        return ModelUsageStore(results_file.parent / "model_usage.json")

    def model_usage(self) -> ModelUsageSnapshot:
        return self.model_usage_store().snapshot()

    def set_model_daily_limit(self, limit: int) -> ModelUsageSnapshot:
        return self.model_usage_store().set_limit(limit)

    def reset_model_usage_today(self) -> ModelUsageSnapshot:
        return self.model_usage_store().reset_today()

    def pipeline_progress_store(self) -> PipelineProgressStore:
        values = self.read_config()
        results_file = Path(values["RESULTS_FILE"])
        return PipelineProgressStore(results_file.parent / "pipeline_progress.json")

    def pipeline_progress(self) -> list[PipelineProgressRecord]:
        values = self.read_config()
        return self.pipeline_progress_store().sync_evaluations(Path(values["RESULTS_FILE"]))

    @property
    def preview_ads(self) -> list[Ad]:
        if self._preview_fetched_at is None:
            return []
        if datetime.now(UTC) - self._preview_fetched_at > timedelta(minutes=30):
            self._preview_ads.clear()
            self._preview_fetched_at = None
            self._preview_counts = (0, 0, 0)
            return []
        return list(self._preview_ads.values())

    @property
    def preview_fetched_at(self) -> datetime | None:
        return self._preview_fetched_at

    @property
    def preview_counts(self) -> tuple[int, int, int]:
        return self._preview_counts

    async def fetch_preview(self) -> list[Ad]:
        settings = self._load_settings()
        client = MarktplaatsClient(
            timeout_seconds=settings.request_timeout_seconds,
            user_agent=settings.user_agent,
        )
        fetched = await client.fetch_ads(
            settings.marktplaats_search_url,
            limit=settings.max_ads_per_poll,
        )
        eligible = [
            ad
            for ad in fetched
            if not settings.exclude_admarkt_ads or not ad.id.lower().startswith("a")
        ]
        self._preview_ads = {ad.id: ad for ad in eligible}
        self._preview_fetched_at = datetime.now(UTC)
        self._preview_counts = (len(fetched), len(eligible), len(fetched) - len(eligible))
        return eligible

    async def test_preview_ad(self, ad_id: str) -> EvaluatedAd:
        ads = {ad.id: ad for ad in self.preview_ads}
        ad = ads.get(ad_id)
        if ad is None:
            raise ValueError("The preview expired or the selected ad is unavailable. Fetch again.")
        settings = self._load_settings()
        client = MarktplaatsClient(
            timeout_seconds=settings.request_timeout_seconds,
            user_agent=settings.user_agent,
        )
        enriched_ad = await client.enrich_ad(ad)
        self._preview_ads[enriched_ad.id] = enriched_ad
        result = await build_model_evaluator(settings).evaluate(enriched_ad)
        evaluated_ad = EvaluatedAd(ad=enriched_ad, result=result)
        seen_store = SeenStore(settings.state_file)
        seen_store.append_result(settings.results_file, evaluated_ad)
        seen_store.mark_seen(ad, result)
        self.pipeline_progress_store().save_ai_result(evaluated_ad)
        RuntimeStatusStore(settings.status_file).resolve_evaluation_failure(ad.id)
        return evaluated_ad

    async def send_pipeline_result_to_telegram(self, ad_id: str) -> PipelineProgressRecord:
        self.pipeline_progress()
        record = self.pipeline_progress_store().get(ad_id)
        if record is None:
            raise ValueError("No saved AI result exists for this ad.")
        send_result = await TelegramNotifier(self._load_settings()).send(record.evaluated_ad)
        if not send_result.sent:
            raise ValueError(send_result.reason or "Telegram did not send the result.")
        return self.pipeline_progress_store().mark_telegram_sent(
            ad_id,
            message_id=send_result.message_id,
        )

    async def send_standalone_telegram_test(self) -> None:
        send_result = await TelegramNotifier(self._load_settings()).send_test_message()
        if not send_result.sent:
            raise ValueError(send_result.reason or "Telegram connectivity test failed.")

    async def start(self) -> None:
        self._stopping = False
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._stopping = True
        if self._stop_event is not None:
            self._stop_event.set()
        if self._loop_task is not None:
            await self._loop_task
        if self._manual_tasks:
            await asyncio.gather(*tuple(self._manual_tasks), return_exceptions=True)

    async def run_once(self) -> None:
        async with self._run_lock:
            try:
                settings = self._load_settings()
                watcher = build_watcher(
                    settings,
                    status_store=RuntimeStatusStore(settings.status_file),
                )
            except Exception as error:
                self.status_store().mark_failed(error)
                raise

            await watcher.run_once()

    def queue_run_once(self) -> bool:
        if self._stopping or self._run_lock.locked() or self._manual_tasks:
            return False

        task = asyncio.create_task(self._run_once_safely())
        self._manual_tasks.add(task)
        task.add_done_callback(self._manual_tasks.discard)
        return True

    async def _run_once_safely(self) -> None:
        with suppress(Exception):
            await self.run_once()

    async def _run_forever(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            delay_seconds = ERROR_RETRY_SECONDS
            try:
                await self.run_once()
                settings = self._load_settings()
                delay_seconds = settings.poll_interval_seconds
            except Exception:
                LOGGER.exception("Watcher run failed; retrying in %s seconds.", delay_seconds)

            next_run = datetime.now(UTC) + timedelta(seconds=delay_seconds)
            try:
                self.status_store().set_next_run_at(next_run)
            except Exception:
                LOGGER.exception("Could not update the next scheduled run time.")

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=delay_seconds,
                )
            except TimeoutError:
                continue

    def _load_settings(self) -> Settings:
        return Settings.from_environment(self.read_config(), dry_run=self._dry_run)


def create_web_app(*, env_file: Path, dry_run: bool = False) -> Starlette:
    service = WatcherService(env_file=env_file, dry_run=dry_run)
    recent_logs = RecentLogBuffer()

    @asynccontextmanager
    async def lifespan(_: Starlette):
        root_logger = logging.getLogger()
        root_logger.addHandler(recent_logs)
        await service.start()
        try:
            yield
        finally:
            await service.stop()
            root_logger.removeHandler(recent_logs)

    async def index(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied

        status = service.status_store().read()
        body = f"""
                {_navigation(request, current="dashboard")}
        {_warning_for_missing_token(service)}
                {_status_panel(status)}
                {_last_run_panel(status)}
        {_model_usage_panel(service.model_usage(), request)}
                <section class="panel">
                    <h2>Activity since reset</h2>
                    <div class="metric-grid">
                        {_metric("Runs", status.total_runs)}
                        {_metric("Errors", status.total_errors)}
                        {_metric("Evaluated", status.total_evaluated)}
                        {_metric("AI failed", status.total_evaluation_failed)}
                        {_metric("Telegram sent", status.total_notified)}
                    </div>
                    <details>
                        <summary>All counters</summary>
                        <table>
                            {_row("Fetched results", status.total_fetched)}
                            {_row("Kept after filters", status.total_kept)}
                            {_row("Filtered locally", status.total_filtered)}
                              {_row("New-ad attempts", status.total_new)}
                              {_row("AI evaluation failures", status.total_evaluation_failed)}
                            {_row("Model ignored", status.total_ignored)}
                            {_row("Model review", status.total_reviewed)}
                            {_row("Model notify", status.total_notify_actions)}
                        </table>
                    </details>
        </section>
                <p class="action-row">
                    <a class="button-link" href="/tools{_token_query(request)}">
                        Open pipeline tools
                    </a>
                    <a href="/api/status{_token_query(request)}">Status JSON</a>
                </p>
        """
        return HTMLResponse(_page("Marktplaats watcher", body))

    async def status_json(request: Request) -> JSONResponse | PlainTextResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return PlainTextResponse("Unauthorized", status_code=401)

        return JSONResponse(service.status_store().read().model_dump(mode="json"))

    async def health(_: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    async def evaluations(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied

        action = _evaluation_action(request)
        values = service.read_config()
        evaluations = _read_evaluations(Path(values["RESULTS_FILE"]), action=action)
        token_query = _token_query(request)
        action_query = _query_with_token(request, action=action)
        body = f"""
        {_navigation(request, current="evaluations")}
        <section class="panel">
          <form class="filter-form" method="get" action="/evaluations">
            {_token_hidden_input(request)}
            <label>Decision
              <select name="action">
                {_evaluation_filter_options(action)}
              </select>
            </label>
            <button type="submit">Filter</button>
          </form>
          <p>{len(evaluations)} evaluation(s). Newest first.</p>
          <p><a href="/api/evaluations{action_query}" download="evaluations.json">Download JSON</a>
          </p>
          {_evaluation_cards(evaluations)}
        </section>
        <p><a href="/{token_query}">Back to status</a></p>
        """
        return HTMLResponse(_page("Evaluations", body))

    async def evaluations_json(request: Request) -> JSONResponse | PlainTextResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return PlainTextResponse("Unauthorized", status_code=401)

        values = service.read_config()
        evaluations = _read_evaluations(
            Path(values["RESULTS_FILE"]), action=_evaluation_action(request)
        )
        return JSONResponse([evaluation.model_dump(mode="json") for evaluation in evaluations])

    async def seen_ads(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied

        selected_kind = _seen_filter(request)
        values = service.read_config()
        entries = _read_seen_ads(Path(values["STATE_FILE"]), kind=selected_kind)
        body = f"""
        {_navigation(request, current="seen")}
        <section class="panel">
          <form class="filter-form" method="get" action="/seen">
            {_token_hidden_input(request)}
            <label>Seen reason
              <select name="kind">{_seen_filter_options(selected_kind)}</select>
            </label>
            <button type="submit">Filter</button>
          </form>
          <p>{len(entries)} seen ad(s). Baseline ads were present when tracking started and
          intentionally skipped AI evaluation.</p>
          <p class="hint">A currently new ad appears here only after its production AI evaluation
          succeeds. Failed ads remain pending, stay off this page, and retry on later runs.</p>
          {_seen_ads_table(entries)}
        </section>
        """
        return HTMLResponse(_page("Seen ads", body))

    def tools_page(
        request: Request,
        *,
        notice: str | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        values = service.read_config()
        seen = _read_seen_ads(Path(values["STATE_FILE"]), kind="all")
        seen_by_id = {str(entry["id"]): entry for entry in seen}
        runtime_status = service.status_store().read()
        summary = runtime_status.last_summary
        attempt_time = _format_time_text(runtime_status.last_finished_at)
        failures_by_id = (
            {
                failure.ad_id: f"Latest production attempt at {attempt_time}: {failure.error}"
                for failure in summary.evaluation_failures
            }
            if summary
            else {}
        )
        progress = service.pipeline_progress()
        body = f"""
        {_navigation(request, current="tools")}
        {_notice(notice, error=error)}
        {_model_usage_panel(service.model_usage(), request, compact=True)}
        <section class="panel">
          <h2>Phase 1 · Fetch current ads</h2>
          <p><strong>Fetch only.</strong> Contacts Marktplaats and changes no local state.</p>
          <form method="post" action="/tools/fetch{_token_query(request)}">
            <button type="submit">Fetch current ads</button>
          </form>
          {_preview_summary(service)}
          {_preview_ads_form(request, service.preview_ads, seen_by_id, failures_by_id)}
        </section>
        <section class="panel">
          <h2>Phase 2 · AI test</h2>
                    <p>Sends one fetched ad to the configured model. A successful result is saved to
          Evaluations, marks the ad processed, and clears its pending AI failure. Telegram is
          not called automatically.</p>
                    {_pipeline_progress_cards(request, progress)}
                </section>
                <section class="panel">
                    <h2>Phase 3 · Telegram for a saved result</h2>
                    <p>Each saved AI result has its own explicit Telegram action. No result means no
                    per-ad Telegram action is available.</p>
                    {_pipeline_telegram_actions(request, progress)}
                </section>
                <section class="panel">
                    <h2>Standalone Telegram test</h2>
                    <p>Sends a neutral connectivity message without an ad or AI result.</p>
                    <form method="post" action="/tools/telegram-test{_token_query(request)}">
                        <button type="submit">Send standalone Telegram test</button>
                    </form>
        </section>
        <section class="panel full-run-panel">
          <h2>Full production run</h2>
          <p>Fetches current ads, processes only new ads, writes state and evaluations, updates
          runtime status, and may send Telegram.</p>
          <a class="button-link warning-button" href="/tools/full-run{_token_query(request)}">
            Review full run…
          </a>
        </section>
        """
        return HTMLResponse(_page("Pipeline tools", body))

    async def tools(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        notice = request.query_params.get("notice")
        return tools_page(request, notice=notice)

    async def tools_fetch(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        try:
            await service.fetch_preview()
        except Exception as error:
            LOGGER.exception("Pipeline fetch preview failed.")
            return tools_page(request, error=_safe_error("Fetch failed", error))
        return tools_page(request, notice="Fetched current ads without changing watcher state.")

    async def tools_test(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        ad_id = form.get("ad_id", [""])[0].strip()
        try:
            result = await service.test_preview_ad(ad_id)
        except Exception as error:
            LOGGER.exception("Pipeline AI preview failed.")
            return tools_page(request, error=_safe_error("AI test failed", error))
        return tools_page(
            request,
            notice=(
                f"AI phase completed for {result.ad.title}. The result was saved and the ad is "
                "now processed. Telegram was not called."
            ),
        )

    async def tools_telegram(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        ad_id = form.get("ad_id", [""])[0].strip()
        try:
            record = await service.send_pipeline_result_to_telegram(ad_id)
        except Exception as error:
            LOGGER.exception("Pipeline Telegram result test failed.")
            return tools_page(request, error=_safe_error("Telegram test failed", error))
        return tools_page(
            request,
            notice=f"Telegram sent for {record.evaluated_ad.ad.title} and delivery was recorded.",
        )

    async def tools_telegram_test(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        try:
            await service.send_standalone_telegram_test()
        except Exception as error:
            LOGGER.exception("Standalone Telegram test failed.")
            return tools_page(request, error=_safe_error("Telegram test failed", error))
        return tools_page(request, notice="Standalone Telegram test message sent successfully.")

    async def full_run_confirm(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        body = f"""
        {_navigation(request, current="tools")}
        <section class="panel full-run-panel">
          <h2>Confirm full production run</h2>
          <p>This action writes seen/evaluation state and may send Telegram for new ads.</p>
          <form method="post" action="/run-now{_token_query(request)}">
            <button class="warning-button" type="submit">Start full run</button>
            <a href="/tools{_token_query(request)}">Cancel</a>
          </form>
        </section>
        """
        return HTMLResponse(_page("Confirm full run", body))

    async def diagnostics(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        body = f"""
        {_navigation(request, current="diagnostics")}
        <section class="panel">
          <h2>Recent watcher logs</h2>
          <p>Shows the latest in-process messages since this container started. For complete Docker
          output, open Portainer → Containers → marktplaats-ad-watcher → Logs.</p>
          {_recent_logs_table(list(recent_logs.entries))}
        </section>
        """
        return HTMLResponse(_page("Diagnostics", body))

    async def model_usage(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        usage = service.model_usage()
        notice = request.query_params.get("notice")
        body = f"""
        {_navigation(request, current="usage")}
        {_notice(notice)}
        {_model_usage_panel(usage, request, compact=True)}
        <section class="panel">
          <h2>Change daily limit</h2>
          <p>All production and manual AI calls share this UTC-daily budget. Only successful
          provider responses count as used; failed HTTP/network calls release their reservation.</p>
                    <form class="model-limit-form" method="post"
                        action="/model-usage/limit{_token_query(request)}">
            <label>Requests per UTC day
              <input type="number" name="limit" min="1" max="1000" value="{usage.limit}">
            </label>
            <button type="submit">Review limit change</button>
          </form>
                    <p><a href="/model-usage/reset{_token_query(request)}">
                        Reset today's usage…
                    </a></p>
        </section>
        """
        return HTMLResponse(_page("Model request budget", body))

    async def model_usage_limit(request: Request) -> HTMLResponse | RedirectResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        try:
            new_limit = int(form.get("limit", [""])[0])
            if new_limit < 1 or new_limit > 1000:
                raise ValueError
        except ValueError:
            return HTMLResponse(
                _page("Invalid limit", "<p class='alert'>Enter a value from 1 to 1000.</p>"),
                status_code=400,
            )
        current = service.model_usage()
        if new_limit <= current.limit:
            updated = service.set_model_daily_limit(new_limit)
            notice = f"Daily model request limit changed to {updated.limit}."
            return RedirectResponse(
                f"/model-usage{_query_with_values(request, notice=notice)}",
                status_code=303,
            )
        body = f"""
        {_navigation(request, current="usage")}
        <section class="panel full-run-panel">
          <h2>Confirm increased model budget</h2>
          <p>Increase the daily limit from <strong>{current.limit}</strong> to
          <strong>{new_limit}</strong>? Usage is currently {current.used}; this immediately permits
          up to {max(0, new_limit - current.used)} more outbound request(s) today.</p>
          <form method="post" action="/model-usage/limit/apply{_token_query(request)}">
            <input type="hidden" name="limit" value="{new_limit}">
            <button class="warning-button" type="submit">Confirm increased limit</button>
            <a href="/model-usage{_token_query(request)}">Cancel</a>
          </form>
        </section>
        """
        return HTMLResponse(_page("Confirm model budget", body))

    async def model_usage_limit_apply(request: Request) -> RedirectResponse | HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        try:
            new_limit = int(form.get("limit", [""])[0])
            current = service.model_usage()
            if new_limit <= current.limit or new_limit > 1000:
                raise ValueError
        except ValueError:
            return HTMLResponse(
                _page("Invalid increase", "<p class='alert'>The increase is no longer valid.</p>"),
                status_code=400,
            )
        updated = service.set_model_daily_limit(new_limit)
        notice = f"Daily model request limit increased to {updated.limit} and is active now."
        return RedirectResponse(
            f"/model-usage{_query_with_values(request, notice=notice)}",
            status_code=303,
        )

    async def model_usage_reset(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        usage = service.model_usage()
        body = f"""
        {_navigation(request, current="usage")}
        <section class="panel full-run-panel">
          <h2>Reset today's model usage?</h2>
          <p>This changes usage from <strong>{usage.used}</strong> to <strong>0</strong> and
          immediately restores the daily allowance. It does not change the limit.</p>
          <form method="post" action="/model-usage/reset/apply{_token_query(request)}">
            <button class="warning-button" type="submit">Confirm usage reset</button>
            <a href="/model-usage{_token_query(request)}">Cancel</a>
          </form>
        </section>
        """
        return HTMLResponse(_page("Confirm usage reset", body))

    async def model_usage_reset_apply(request: Request) -> RedirectResponse | HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied
        updated = service.reset_model_usage_today()
        notice = f"Today's model usage was reset to {updated.used}/{updated.limit}."
        return RedirectResponse(
            f"/model-usage{_query_with_values(request, notice=notice)}",
            status_code=303,
        )

    async def config_get(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied

        values = service.read_config()
        token_query = _token_query(request)
        notify_review_checkbox = _checkbox(
            "NOTIFY_REVIEW_ACTIONS", values, "Send reviews to Telegram"
        )
        send_images_checkbox = _checkbox(
            "SEND_IMAGE_CONTENT_TO_MODEL", values, "Allow model to inspect listing images"
        )
        disable_previews_checkbox = _checkbox(
            "TELEGRAM_DISABLE_WEB_PAGE_PREVIEW", values, "Disable previews"
        )
        body = f"""
        {_navigation(request, current="config")}
        {_warning_for_missing_token(service)}
                <form class="config-form" method="post" action="/config{token_query}">
                    <fieldset>
                        <legend>Watch criteria</legend>
                        {_input("MARKTPLAATS_SEARCH_URL", values, label="Marktplaats search URL")}
                        {_textarea("MARKTPLAATS_USE_CASE", values, label="Evaluation instructions")}
                    </fieldset>

                    <fieldset>
                        <legend>Schedule and filtering</legend>
                        <div class="grid">
                              {_input("POLL_INTERVAL_SECONDS", values, label="Interval (seconds)")}
                            {_input("MAX_ADS_PER_POLL", values, label="Maximum ads per run")}
                        </div>
                        <div class="checks">
                            {_checkbox("BOOTSTRAP_EXISTING_ADS", values, "Bootstrap current ads")}
                            {_checkbox("EXCLUDE_ADMARKT_ADS", values, "Exclude Admarkt listings")}
                        </div>
                    </fieldset>

                    <fieldset>
                        <legend>Decision policy</legend>
                        <div class="grid">
                            {_input("NOTIFY_MIN_CONFIDENCE", values, label="Notify threshold")}
                            {_input("REVIEW_MIN_CONFIDENCE", values, label="Review threshold")}
                        </div>
                        <div class="checks">
                                                        {notify_review_checkbox}
                        </div>
                    </fieldset>

                    <fieldset>
                        <legend>Model provider</legend>
                        {_provider_select(values)}
                        <p id="provider-help" class="provider-note"></p>
                        <div class="grid">
                            {_secret("MODEL_API_KEY", values, label="API key")}
                            {_input("MODEL_NAME", values, label="Model")}
                        </div>
                        <details class="advanced">
                            <summary>Advanced model settings</summary>
                            <div class="grid advanced-grid">
                                {_input("MODEL_BASE_URL", values, label="Base URL")}
                                <div id="reasoning-field">{_reasoning_select(values)}</div>
                                <div id="temperature-field">
                                    {_input("MODEL_TEMPERATURE", values, label="Temperature")}
                                </div>
                                {_input("MODEL_MAX_TOKENS", values, label="Maximum output tokens")}
                                {_input("MAX_IMAGES_FOR_MODEL", values, label="Maximum images")}
                            </div>
                            <div class="checks">
                                {_checkbox("MODEL_JSON_MODE", values, "Structured JSON output")}
                                {send_images_checkbox}
                            </div>
                            <p class="hint">Off by default. When disabled, no image URLs or image
                            instructions are sent to the model.</p>
                        </details>
                        {_provider_defaults_script()}
                    </fieldset>

                    <fieldset>
                        <legend>Telegram</legend>
                        <div class="grid">
                            {_secret("TELEGRAM_BOT_TOKEN", values, label="Bot token")}
                            {_input("TELEGRAM_CHAT_ID", values, label="Chat ID")}
                        </div>
                        <div class="checks">
                                                        {disable_previews_checkbox}
                        </div>
                    </fieldset>

                    <fieldset>
                        <legend>Web access</legend>
                        {_secret("WEB_ADMIN_TOKEN", values, label="Admin token")}
                    </fieldset>

                    <details class="runtime-settings">
                        <summary>Runtime settings</summary>
                        <div class="grid advanced-grid">
                            {_input("STATE_FILE", values, label="Seen-ad state file")}
                            {_input("RESULTS_FILE", values, label="Evaluation results file")}
                            {_input("STATUS_FILE", values, label="Runtime status file")}
                              {_input("REQUEST_TIMEOUT_SECONDS", values, label="Timeout (seconds)")}
                            {_input("USER_AGENT", values, label="HTTP user agent")}
                        </div>
                    </details>

                    <div class="form-actions">
                        <button type="submit">Save configuration</button>
                        <a href="/{token_query}">Back to status</a>
                    </div>
        </form>
        """
        return HTMLResponse(_page("Watcher configuration", body))

    async def config_post(request: Request) -> RedirectResponse | HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied

        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        current = service.read_config()
        file_values = parse_dotenv(service.env_file)
        submitted_provider = form.get("MODEL_PROVIDER", [current.get("MODEL_PROVIDER", "")])[
            0
        ].strip().lower()
        current_provider = current.get("MODEL_PROVIDER", "").strip().lower()
        provider_changed = submitted_provider != current_provider
        try:
            preset = provider_preset(submitted_provider)
        except ValueError as error:
            return HTMLResponse(
                _page("Invalid configuration", f"<p>{escape(str(error))}</p>"),
                status_code=400,
            )
        submitted_reasoning = form.get("MODEL_REASONING_EFFORT", [""])[0].strip().lower()
        if submitted_reasoning and submitted_reasoning not in REASONING_EFFORTS:
            return HTMLResponse(
                _page(
                    "Invalid configuration",
                    f"<p>Unsupported reasoning effort: {escape(submitted_reasoning)}</p>",
                ),
                status_code=400,
            )
        updated: dict[str, str] = {}
        for key in EDITABLE_KEYS:
            if key in BOOLEAN_KEYS:
                updated[key] = "true" if key in form else "false"
                continue
            if key == "MODEL_PROVIDER":
                updated[key] = submitted_provider
                continue
            if key == "MODEL_REASONING_EFFORT":
                updated[key] = submitted_reasoning
                continue

            submitted = form.get(key, [""])[0].strip()
            if key in SECRET_KEYS and not submitted:
                if key == "MODEL_API_KEY" and provider_changed:
                    updated[key] = ""
                elif (
                    key == "MODEL_API_KEY"
                    and current_provider == "deepseek"
                    and "DEEPSEEK_API_KEY" in file_values
                ):
                    updated[key] = file_values["DEEPSEEK_API_KEY"]
                elif key in file_values:
                    updated[key] = file_values[key]
                continue
            updated[key] = submitted

        reasoning_effort = updated.get("MODEL_REASONING_EFFORT", "")
        if not preset.supports_reasoning_effort or (
            reasoning_effort and reasoning_effort not in preset.allowed_reasoning_efforts
        ):
            updated["MODEL_REASONING_EFFORT"] = ""
        reasoning_disabled = updated.get("MODEL_REASONING_EFFORT", "") in {"", "none"}
        if not preset.supports_temperature or (
            preset.temperature_requires_no_reasoning and not reasoning_disabled
        ):
            updated["MODEL_TEMPERATURE"] = "0"

        write_dotenv(service.env_file, updated)
        return RedirectResponse(f"/{_token_query(request)}", status_code=303)

    async def run_now(request: Request) -> RedirectResponse | HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied

        queued = service.queue_run_once()
        message = "Full run queued." if queued else "A run is already in progress."
        return RedirectResponse(
            f"/tools{_query_with_values(request, notice=message)}",
            status_code=303,
        )

    return Starlette(
        routes=[
            Route("/", index),
            Route("/healthz", health),
            Route("/api/status", status_json),
            Route("/evaluations", evaluations),
            Route("/api/evaluations", evaluations_json),
            Route("/seen", seen_ads),
            Route("/tools", tools),
            Route("/tools/fetch", tools_fetch, methods=["POST"]),
            Route("/tools/test", tools_test, methods=["POST"]),
            Route("/tools/telegram", tools_telegram, methods=["POST"]),
            Route("/tools/telegram-test", tools_telegram_test, methods=["POST"]),
            Route("/tools/full-run", full_run_confirm),
            Route("/diagnostics", diagnostics),
            Route("/model-usage", model_usage),
            Route("/model-usage/limit", model_usage_limit, methods=["POST"]),
            Route("/model-usage/limit/apply", model_usage_limit_apply, methods=["POST"]),
            Route("/model-usage/reset", model_usage_reset),
            Route("/model-usage/reset/apply", model_usage_reset_apply, methods=["POST"]),
            Route("/config", config_get, methods=["GET"]),
            Route("/config", config_post, methods=["POST"]),
            Route("/run-now", run_now, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


def _deny_if_needed(request: Request, service: WatcherService) -> HTMLResponse | None:
    configured_token = service.admin_token()
    if not configured_token:
        return None

    request_token = request.query_params.get("token")
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        request_token = auth_header[7:].strip()

    if request_token == configured_token:
        return None

    return HTMLResponse(_page("Unauthorized", "<p>Unauthorized.</p>"), status_code=401)


def _token_query(request: Request) -> str:
    token = request.query_params.get("token")
    return f"?{urlencode({'token': token})}" if token else ""


def _query_with_values(request: Request, **values: str) -> str:
    token = request.query_params.get("token")
    if token:
        values["token"] = token
    return f"?{urlencode(values)}" if values else ""


def _query_with_token(request: Request, *, action: str) -> str:
    return _query_with_values(request, action=action)


def _navigation(request: Request, *, current: str) -> str:
    token_query = _token_query(request)
    items = [
        ("dashboard", "/", "Dashboard"),
        ("evaluations", "/evaluations", "Evaluations"),
        ("seen", "/seen", "Seen ads"),
        ("tools", "/tools", "Pipeline tools"),
        ("usage", "/model-usage", "Model budget"),
        ("diagnostics", "/diagnostics", "Diagnostics"),
        ("config", "/config", "Configuration"),
    ]
    links = []
    for key, path, label in items:
        active = ' class="active" aria-current="page"' if key == current else ""
        links.append(f'<a{active} href="{path}{token_query}">{label}</a>')
    return f"<nav class='main-nav' aria-label='Main navigation'>{''.join(links)}</nav>"


def _status_panel(status: RuntimeStatus) -> str:
    has_evaluation_failures = bool(
        status.last_summary and status.last_summary.evaluation_failed_count
    )
    if status.last_error or has_evaluation_failures:
        state, css_class = "Needs attention", "status-error"
    elif status.is_running:
        state, css_class = "Running", "status-running"
    elif status.last_finished_at:
        state, css_class = "Scheduled", "status-ok"
    else:
        state, css_class = "Never run", "status-neutral"

    error = (
        f"<p class='alert' role='alert'><strong>Last error:</strong> "
        f"{escape(status.last_error)}</p>"
        if status.last_error
        else ""
    )
    return f"""
    <section class="panel">
      <div class="section-heading">
        <h2>Operating status</h2>
        <span class="status-badge {css_class}">{state}</span>
      </div>
      <dl class="status-list">
        <div><dt>Last completed</dt><dd>{_format_time(status.last_finished_at)}</dd></div>
        <div><dt>Next scheduled run</dt><dd>{_format_time(status.next_run_at)}</dd></div>
      </dl>
      {error}
    </section>
    """


def _last_run_panel(status: RuntimeStatus) -> str:
    summary = status.last_summary
    if summary is None:
        return """
        <section class="panel">
          <h2>Last run</h2>
          <p>No run has completed yet.</p>
        </section>
        """
    baseline = (
        f"<span class='mini-badge'>Baseline {summary.bootstrapped_count}</span>"
        if summary.bootstrapped_count
        else ""
    )
    failures = _evaluation_failures(summary, finished_at=status.last_finished_at)
    run_label = f"Run #{status.total_runs}" if status.total_runs else "Run before counter reset"
    return f"""
    <section class="panel">
      <h2>Latest completed run · {run_label} · {_format_time(status.last_finished_at)}</h2>
      <p class="pipeline-summary">
        <strong>{summary.fetched_count}</strong> fetched
        <span>→</span> <strong>{summary.kept_count}</strong> eligible
        <span>·</span> {summary.filtered_count} filtered
        <span>→</span> <strong>{summary.new_count}</strong> new
        <span>→</span> <strong>{summary.evaluated_count}</strong> evaluated
      </p>
      <div class="badge-row">
        <span class="mini-badge decision-notify">Notify {summary.notify_action_count}</span>
        <span class="mini-badge decision-review">Review {summary.review_count}</span>
        <span class="mini-badge decision-ignore">Ignore {summary.ignored_count}</span>
                <span class="mini-badge status-error">
                    AI failed {summary.evaluation_failed_count}
                </span>
        <span class="mini-badge">Telegram {summary.notified_count}</span>
        {baseline}
      </div>
        {failures}
    </section>
    """


def _evaluation_failures(
    summary: WatcherRunSummary,
    *,
    finished_at: datetime | None,
) -> str:
        if not summary.evaluation_failures:
                return ""
        items = "".join(
                f"<li><a href='{escape(failure.url)}' target='_blank' rel='noopener noreferrer'>"
                f"{escape(failure.title)}</a>: {escape(failure.error)}. "
                "The ad remains pending and will retry on the next production run.</li>"
                for failure in summary.evaluation_failures
        )
        return (
            "<div class='alert' role='alert'><strong>AI failures in this latest completed run "
            f"({_format_time(finished_at)})</strong>"
            f"<ul>{items}</ul></div>"
        )


def _model_usage_panel(
        usage: ModelUsageSnapshot,
        request: Request,
        *,
        compact: bool = False,
) -> str:
        heading = "Model request budget" if not compact else "Model budget"
        in_flight = (
            f" {usage.in_flight} request(s) currently in flight."
            if usage.in_flight
            else ""
        )
        return f"""
        <section class="panel usage-panel">
            <div class="section-heading">
                <h2>{heading}</h2>
                <strong>{usage.used} / {usage.limit}</strong>
            </div>
            <progress value="{usage.used}" max="{usage.limit}">
                {usage.used} of {usage.limit}
            </progress>
            <p>{usage.remaining} request(s) remaining.{in_flight} Resets
                {_format_time(usage.reset_at)}.
                <a href="/model-usage{_token_query(request)}">Manage limit</a>
            </p>
        </section>
        """


def _metric(label: str, value: int) -> str:
    return f"<div class='metric'><strong>{value}</strong><span>{escape(label)}</span></div>"


def _format_time(value: datetime | str | None) -> str:
    text, iso_value = _format_time_parts(value)
    if iso_value is None:
        return text
    return f"<time datetime='{escape(iso_value)}'>{text}</time>"


def _format_time_text(value: datetime | str | None) -> str:
    return _format_time_parts(value)[0]


def _format_time_parts(value: datetime | str | None) -> tuple[str, str | None]:
    if value is None:
        return "Not available", None
    parsed = value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return escape(value), None
    assert isinstance(parsed, datetime)
    display = parsed.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC")
    return display, parsed.isoformat()


def _notice(message: str | None, *, error: str | None = None) -> str:
    if error:
        return f"<p class='alert' role='alert'>{escape(error)}</p>"
    if message:
        return f"<p class='notice' role='status'>{escape(message)}</p>"
    return ""


def _safe_error(prefix: str, error: Exception) -> str:
    if isinstance(error, ValueError | ModelProviderError | ModelDailyLimitExceeded):
        return f"{prefix}: {error}"
    return f"{prefix}. Check the watcher logs for technical details and try again."


def _token_hidden_input(request: Request) -> str:
    token = request.query_params.get("token")
    if not token:
        return ""
    return f"<input type='hidden' name='token' value='{escape(token)}'>"


def _evaluation_action(request: Request) -> str:
    action = request.query_params.get("action", "all").strip().lower()
    return action if action in {"all", "notify", "review", "ignore"} else "all"


def _seen_filter(request: Request) -> str:
    kind = request.query_params.get("kind", "all").strip().lower()
    return kind if kind in {"all", "baseline", "processed", "recorded"} else "all"


def _read_seen_ads(path: Path, *, kind: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    loaded = json.loads(path.read_text(encoding="utf-8"))
    raw_entries = loaded.get("seen_ads", {})
    if not isinstance(raw_entries, dict):
        raise ValueError("Seen-ad history has an unexpected format.")

    entries: list[dict[str, Any]] = []
    for ad_id, value in raw_entries.items():
        if not isinstance(value, dict):
            continue
        entry = {"id": str(ad_id), **value}
        entry["kind"] = _seen_kind(entry)
        if kind == "all" or entry["kind"] == kind:
            entries.append(entry)
    return sorted(entries, key=lambda entry: str(entry.get("first_seen_at", "")), reverse=True)


def _seen_kind(entry: Mapping[str, Any]) -> str:
    if entry.get("bootstrapped") is True:
        return "baseline"
    if isinstance(entry.get("evaluation"), dict):
        return "processed"
    return "recorded"


def _seen_filter_options(selected: str) -> str:
    options = [
        ("all", "All seen ads"),
        ("baseline", "Baseline"),
        ("processed", "Processed"),
        ("recorded", "Recorded"),
    ]
    return "".join(
        f"<option value='{value}'{' selected' if value == selected else ''}>{label}</option>"
        for value, label in options
    )


def _seen_ads_table(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "<p>No seen ads match this filter yet.</p>"
    rows = []
    explanations = {
        "baseline": "Present when tracking started; skipped AI evaluation.",
        "processed": "Processed as a newly discovered ad by the normal pipeline.",
        "recorded": "Seen reason was not recorded by this version.",
    }
    for entry in entries:
        kind = str(entry["kind"])
        evaluation = entry.get("evaluation")
        decision = "—"
        if isinstance(evaluation, dict):
            action = escape(str(evaluation.get("next_action", "unknown")))
            confidence = evaluation.get("confidence")
            confidence_text = f" · {float(confidence):.0%}" if confidence is not None else ""
            decision = f"{action}{confidence_text}"
        rows.append(
            f"""
            <tr>
              <td><a href="{escape(str(entry.get('url', '')))}" rel="noopener noreferrer"
                target="_blank">{escape(str(entry.get('title', entry['id'])))}</a>
                <span class="secondary">{escape(str(entry['id']))}</span></td>
              <td><span class="seen-badge seen-{kind}">{kind}</span>
                <span class="secondary">{explanations[kind]}</span></td>
              <td>{_format_time(entry.get('first_seen_at'))}</td>
              <td>{decision}</td>
            </tr>
            """
        )
    return f"""
    <div class="table-scroll"><table class="history-table">
      <thead><tr><th>Ad</th><th>Seen reason</th><th>First seen</th><th>Decision</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table></div>
    """


def _preview_summary(service: WatcherService) -> str:
    if service.preview_fetched_at is None:
        return "<p class='hint'>No preview fetched in this process yet.</p>"
    fetched, eligible, filtered = service.preview_counts
    return (
        f"<p><strong>{fetched}</strong> fetched · <strong>{eligible}</strong> eligible · "
        f"{filtered} filtered · fetched {_format_time(service.preview_fetched_at)}</p>"
    )


def _preview_ads_form(
    request: Request,
    ads: list[Ad],
    seen_by_id: Mapping[str, Mapping[str, Any]],
    failures_by_id: Mapping[str, str],
) -> str:
    if not ads:
        return ""
    cards = []
    for index, ad in enumerate(ads):
        seen_entry = seen_by_id.get(ad.id)
        if seen_entry:
            kind = _seen_kind(seen_entry)
            state = f"<span class='seen-badge seen-{kind}'>{kind}</span>"
            state_detail = ""
        elif ad.id in failures_by_id:
            state = "<span class='seen-badge status-error'>AI failed · pending</span>"
            state_detail = (
                f'<span class="secondary failure-detail">{escape(failures_by_id[ad.id])}. '
                "The production watcher will retry this ad.</span>"
            )
        else:
            state = "<span class='seen-badge status-running'>new · pending</span>"
            state_detail = (
                "<span class='secondary'>Not in seen state; production evaluation has not "
                "completed yet.</span>"
            )
        details = " · ".join(value for value in [ad.price, ad.location, ad.seller] if value)
        display_details = escape(details) if details else "No price, location, or seller supplied."
        cards.append(
            f"""
            <label class="preview-card">
              <input type="radio" name="ad_id" value="{escape(ad.id)}"
                {'checked' if index == 0 else ''}>
                            <span class="preview-content">
                                <span class="preview-heading">
                                    <strong>{escape(ad.title)}</strong> {state}
                                </span>
                                <span>{display_details}</span>
                                <span class="secondary">
                                    ID {escape(ad.id)} · {len(ad.image_urls)} image(s) ·
                                    <a href="{escape(ad.url)}" target="_blank"
                                        rel="noopener noreferrer">Open ad</a>
                                </span>
                                {state_detail}
              </span>
            </label>
            """
        )
    return f"""
    <form method="post" action="/tools/test{_token_query(request)}">
      <fieldset class="preview-list">
        <legend>Select one eligible ad</legend>
        {''.join(cards)}
      </fieldset>
      <button type="submit">Test AI for selected ad</button>
    </form>
    """


def _pipeline_progress_cards(
    request: Request,
    records: list[PipelineProgressRecord],
) -> str:
    del request
    if not records:
        return "<p class='hint'>No saved manual AI results yet. Fetch ads and test one.</p>"
    cards = []
    for record in records:
        if record.telegram_sent is True:
            telegram = f"Telegram sent {_format_time(record.telegram_sent_at)}"
        elif record.telegram_sent is False:
            telegram = "Telegram not sent"
        else:
            telegram = "Telegram delivery not tracked by test pipeline"
        source = "Manual AI test" if record.source == "manual_test" else "Production evaluation"
        cards.append(
            f"""
            <div class="pipeline-progress">
              <p class="badge-row">
                <span class="mini-badge status-ok">AI complete · saved</span>
                <span class="mini-badge">{source}</span>
                <span class="mini-badge">{telegram}</span>
                <span class="hint">Tested {_format_time(record.tested_at)}</span>
              </p>
              {_evaluation_cards([record.evaluated_ad])}
            </div>
            """
        )
    return "".join(cards)


def _pipeline_telegram_actions(
    request: Request,
    records: list[PipelineProgressRecord],
) -> str:
    if not records:
        return "<p class='hint'>No saved AI result is available for Telegram.</p>"
    forms = []
    for record in records:
        ad = record.evaluated_ad.ad
        label = "Send again" if record.telegram_sent is True else "Send result"
        forms.append(
            f"""
            <form class="telegram-result-action" method="post"
              action="/tools/telegram{_token_query(request)}">
              <input type="hidden" name="ad_id" value="{escape(ad.id)}">
              <span><strong>{escape(ad.title)}</strong>
                <span class="secondary">{escape(record.evaluated_ad.result.next_action)} ·
                {record.evaluated_ad.result.confidence:.0%}</span>
              </span>
              <button type="submit">{label} via Telegram</button>
            </form>
            """
        )
    return "<div class='telegram-result-list'>" + "".join(forms) + "</div>"


def _read_evaluations(path: Path, *, action: str) -> list[EvaluatedAd]:
    if not path.exists():
        return []

    evaluations: list[EvaluatedAd] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            evaluation = EvaluatedAd.model_validate_json(line)
        except ValueError:
            LOGGER.warning("Skipping an invalid evaluation record in %s.", path)
            continue
        if action == "all" or evaluation.result.next_action == action:
            evaluations.append(evaluation)

    return sorted(evaluations, key=lambda evaluation: evaluation.evaluated_at, reverse=True)


def _evaluation_filter_options(selected_action: str) -> str:
    options = [
        ("all", "All decisions"),
        ("notify", "Notify"),
        ("review", "Review"),
        ("ignore", "Ignore"),
    ]
    return "".join(
        f"<option value='{value}'{' selected' if value == selected_action else ''}>{label}</option>"
        for value, label in options
    )


def _evaluation_cards(evaluations: list[EvaluatedAd], *, test_only: bool = False) -> str:
    if not evaluations:
        return "<p>No evaluations match this filter yet.</p>"

    cards = []
    for evaluation in evaluations:
        ad = evaluation.ad
        result = evaluation.result
        metadata = " · ".join(value for value in [ad.price, ad.location, ad.seller] if value)
        signals = _evaluation_list("Signals", result.signals)
        concerns = _evaluation_list("Concerns", result.concerns)
        details = ""
        if signals or concerns or ad.description:
            description = (
                f"<p><strong>Description:</strong> {escape(ad.description)}</p>"
                if ad.description
                else ""
            )
            details = "<details><summary>Signals, concerns, and description</summary>"
            details += f"{signals}{concerns}{description}</details>"
        test_badge = "<span class='mini-badge'>Test only</span>" if test_only else ""
        cards.append(
            f"""
            <article class="evaluation-card">
              <div class="evaluation-heading">
                <span class="decision decision-{escape(result.next_action)}">
                  {escape(result.next_action)}
                </span>
                <strong>{escape(ad.title)}</strong>
                {test_badge}
              </div>
              <p><a href="{escape(ad.url)}" rel="noopener noreferrer" target="_blank">
                Open Marktplaats ad
              </a></p>
              <p>{escape(metadata) if metadata else 'No price, location, or seller supplied.'}</p>
              <p><strong>Confidence:</strong> {result.confidence:.0%}</p>
              <p><strong>Reason:</strong> {escape(result.reason)}</p>
              {details}
              <p class="hint">Evaluated {_format_time(evaluation.evaluated_at)}</p>
            </article>
            """
        )
    return "".join(cards)


def _evaluation_list(label: str, values: list[str]) -> str:
    if not values:
        return ""
    items = "".join(f"<li>{escape(value)}</li>" for value in values)
    return f"<p><strong>{escape(label)}:</strong></p><ul>{items}</ul>"


def _recent_logs_table(entries: list[dict[str, str]]) -> str:
    if not entries:
        return "<p>No log messages have been captured since this container started.</p>"
    rows = []
    for entry in reversed(entries):
        level = entry.get("level", "INFO").lower()
        detail = entry.get("detail", "")
        rendered_message = escape(entry.get("message", ""))
        if detail:
            rendered_message += (
                "<details class='diagnostic-detail'><summary>Show response</summary>"
                f"<pre>{escape(detail)}</pre></details>"
            )
        rows.append(
            f"""
            <tr>
              <td>{_format_time(entry.get('timestamp'))}</td>
              <td><span class="log-level log-{escape(level)}">
                {escape(level)}
              </span></td>
              <td>{escape(entry.get('logger', ''))}</td>
              <td class="log-message">{rendered_message}</td>
            </tr>
            """
        )
    return f"""
    <div class="table-scroll"><table class="log-table">
      <thead><tr><th>Time</th><th>Level</th><th>Logger</th><th>Message</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table></div>
    """


def _warning_for_missing_token(service: WatcherService) -> str:
    if service.admin_token():
        return ""

    return "<p class='warning'>WEB_ADMIN_TOKEN is not configured. Do not expose this page.</p>"


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
        * {{ box-sizing: border-box; }}
        body {{
            background: #f7f8fa;
            color: #222;
            font-family: system-ui, sans-serif;
            margin: 0 auto;
            max-width: 1280px;
            padding: 1.5rem;
            width: 100%;
        }}
        h1 {{ font-size: 1.65rem; margin: 0 0 1.25rem; }}
        h2 {{ font-size: 1.15rem; margin: 0 0 0.85rem; }}
        a {{ color: #245b8f; }}
        table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
        td, th {{
            border: 1px solid #ddd;
            padding: 0.55rem;
            text-align: left;
            vertical-align: top;
        }}
        th {{ background: #f3f5f7; }}
        input, textarea, select {{
            background: #fff;
            border: 1px solid #aaa;
            border-radius: 5px;
            box-sizing: border-box;
            color: inherit;
            font: inherit;
            margin-top: 0.3rem;
            padding: 0.5rem;
            width: 100%;
        }}
        input:focus, textarea:focus, select:focus {{
            border-color: #356aa0;
            outline: 2px solid #bed7ee;
            outline-offset: 1px;
        }}
    textarea {{ min-height: 12rem; }}
        label {{ display: block; margin: 0; }}
        button, .button-link {{
            background: #315f8c;
            border: 1px solid #274d72;
            border-radius: 5px;
            color: white;
            cursor: pointer;
            display: inline-block;
            font: inherit;
            min-height: 2.65rem;
            padding: 0.55rem 0.9rem;
            text-decoration: none;
        }}
        .main-nav {{
            display: flex;
            flex-wrap: wrap;
            gap: 0.35rem;
            margin: 0 0 1.25rem;
        }}
        .main-nav a {{
            border-radius: 5px;
            color: #315f8c;
            padding: 0.45rem 0.65rem;
            text-decoration: none;
        }}
        .main-nav a:hover, .main-nav a.active {{ background: #e5edf5; }}
        .panel {{
            background: white;
            border: 1px solid #dfe3e7;
            border-radius: 7px;
            margin: 0 0 1rem;
            padding: 1rem;
        }}
        .section-heading {{
            align-items: center;
            display: flex;
            gap: 0.75rem;
            justify-content: space-between;
        }}
        .status-badge, .mini-badge, .seen-badge {{
            border-radius: 1rem;
            display: inline-block;
            font-size: 0.78rem;
            font-weight: 700;
            padding: 0.22rem 0.55rem;
            text-transform: uppercase;
        }}
        .status-ok, .status-running, .seen-processed {{ background: #dcefe3; color: #145c31; }}
        .status-error {{ background: #f7dddd; color: #8a2020; }}
        .status-neutral, .seen-recorded {{ background: #e8eaed; color: #4f5358; }}
        .seen-baseline {{ background: #e4ecf6; color: #315f8c; }}
        .status-list {{
            display: grid;
            gap: 0.7rem;
            grid-template-columns: repeat(2, 1fr);
            margin: 0;
        }}
        .status-list div {{ background: #f7f8fa; border-radius: 5px; padding: 0.7rem; }}
        .status-list dt {{ color: #555; font-size: 0.82rem; }}
        .status-list dd {{ margin: 0.2rem 0 0; }}
        .pipeline-summary {{ align-items: center; display: flex; flex-wrap: wrap; gap: 0.45rem; }}
        .badge-row, .action-row {{
            align-items: center;
            display: flex;
            flex-wrap: wrap;
            gap: 0.55rem;
        }}
        .badge-row + .alert {{ margin-top: 1rem; }}
        .mini-badge {{ background: #e8eaed; color: #30343a; }}
        .metric-grid {{
            display: grid;
            gap: 0.7rem;
            grid-template-columns: repeat(auto-fit, minmax(8rem, 1fr));
        }}
        .metric {{ background: #f7f8fa; border-radius: 5px; padding: 0.8rem; }}
        .metric strong {{ display: block; font-size: 1.45rem; }}
        .metric span {{ color: #555; font-size: 0.85rem; }}
        fieldset, details.runtime-settings {{
            border: 1px solid #d5d5d5;
            border-radius: 6px;
            margin: 0;
            padding: 1rem;
        }}
        legend {{ font-weight: 650; padding: 0 0.35rem; }}
        summary {{ cursor: pointer; font-weight: 600; }}
        .config-form {{ display: grid; gap: 1rem; }}
        .grid {{
            display: grid;
            gap: 0.9rem;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
        }}
        fieldset > .grid, .advanced-grid {{ margin-top: 0.8rem; }}
        .advanced {{ border-top: 1px solid #e1e1e1; margin-top: 1rem; padding-top: 0.8rem; }}
        .checks {{ display: grid; gap: 0.5rem; margin-top: 0.9rem; }}
        .checks label {{ align-items: center; display: flex; gap: 0.5rem; }}
        .checks input {{ flex: 0 0 auto; margin: 0; width: auto; }}
        .provider-note {{
            background: #f5f7f9;
            border-left: 3px solid #7892aa;
            color: #444;
            font-size: 0.9rem;
            margin: 0.8rem 0;
            padding: 0.55rem 0.7rem;
        }}
        .form-actions {{
            align-items: center;
            border-top: 1px solid #ddd;
            display: flex;
            gap: 1rem;
            padding-top: 1rem;
        }}
        .filter-form {{ align-items: end; display: flex; flex-wrap: wrap; gap: 0.75rem; }}
        .filter-form label {{ flex: 0 1 18rem; min-width: 12rem; }}
        .filter-form select {{ max-width: 18rem; }}
        input[type="number"] {{ max-width: 10rem; }}
        .model-limit-form button {{ margin-top: 0.75rem; }}
        .evaluation-card {{
            border: 1px solid #d5d5d5;
            border-radius: 6px;
            margin: 1rem 0;
            padding: 1rem;
        }}
        .evaluation-card p {{ margin: 0.55rem 0; }}
        .evaluation-heading {{ align-items: center; display: flex; flex-wrap: wrap; gap: 0.6rem; }}
        .decision {{
            border-radius: 1rem;
            color: white;
            font-size: 0.8rem;
            font-weight: 700;
            padding: 0.2rem 0.55rem;
            text-transform: uppercase;
        }}
        .decision-notify {{ background: #16733c; }}
        .decision-review {{ background: #956500; }}
        .decision-ignore {{ background: #5a5a5a; }}
        .table-scroll {{ overflow-x: auto; }}
        .history-table {{ min-width: 720px; }}
        .log-table {{ min-width: 850px; }}
        .log-table th:first-child, .log-table td:first-child {{
            min-width: 12.5rem;
            white-space: nowrap;
        }}
        .log-message {{ font-family: ui-monospace, monospace; font-size: 0.82rem; }}
        .diagnostic-detail {{ margin-top: 0.45rem; }}
        .diagnostic-detail pre {{
            margin-bottom: 0;
            overflow-wrap: anywhere;
            white-space: pre-wrap;
        }}
        .log-level {{
            border-radius: 1rem;
            font-size: 0.72rem;
            font-weight: 700;
            padding: 0.18rem 0.45rem;
        }}
        .log-error, .log-critical {{ background: #f7dddd; color: #8a2020; }}
        .log-warning {{ background: #fff0ca; color: #765000; }}
        .log-info, .log-debug {{ background: #e8eaed; color: #4f5358; }}
        .secondary {{ color: #5f646a; display: block; font-size: 0.82rem; margin-top: 0.25rem; }}
        .failure-detail {{ color: #8a2020; overflow-wrap: anywhere; word-break: break-word; }}
        .usage-panel progress {{ height: 0.85rem; width: 100%; }}
        .usage-panel p {{ margin-bottom: 0; }}
        .preview-list {{ display: grid; gap: 0.55rem; margin: 1rem 0; }}
        .preview-card {{
            align-items: start;
            background: #fafbfc;
            border: 1px solid #dfe3e7;
            border-radius: 6px;
            display: grid;
            gap: 0.3rem;
            grid-template-columns: auto minmax(0, 1fr);
            padding: 0.75rem;
        }}
        .preview-card input {{ margin: 0.25rem 0.35rem 0 0; width: auto; }}
        .preview-content {{ display: grid; gap: 0.3rem; min-width: 0; }}
        .preview-heading {{ align-items: center; display: flex; flex-wrap: wrap; gap: 0.4rem; }}
        .pipeline-progress {{ border-top: 1px solid #dfe3e7; margin-top: 1rem; padding-top: 1rem; }}
        .telegram-result-list {{ display: grid; gap: 0.65rem; }}
        .telegram-result-action {{
            align-items: center;
            background: #fafbfc;
            border: 1px solid #dfe3e7;
            border-radius: 6px;
            display: flex;
            flex-wrap: wrap;
            gap: 0.75rem;
            justify-content: space-between;
            padding: 0.75rem;
        }}
        .full-run-panel {{ border-color: #d6ad58; }}
        .warning-button {{ background: #956500; border-color: #765000; }}
        .notice, .alert, .warning {{ border-radius: 5px; padding: 0.7rem; }}
        .notice {{ background: #e2f1e7; border: 1px solid #8ab49a; }}
        .alert {{ background: #f8e1e1; border: 1px solid #d49a9a; }}
        .warning {{
            background: #fff4ce;
            border: 1px solid #e0b100;
            border-radius: 5px;
            padding: 0.7rem;
        }}
        .hint {{ color: #555; font-size: 0.9rem; }}
        [hidden] {{ display: none !important; }}
        @media (max-width: 600px) {{
            body {{ padding: 1rem; }}
            .grid {{ grid-template-columns: 1fr; }}
            .metric-grid, .status-list {{ grid-template-columns: repeat(2, 1fr); }}
            .main-nav a {{ flex: 1 1 auto; text-align: center; }}
        }}
  </style>
</head>
<body>
  <h1>{escape(title)}</h1>
  {body}
</body>
</html>"""


def _row(label: str, value: object) -> str:
    return f"<tr><th>{escape(label)}</th><td>{escape(str(value))}</td></tr>"


def _input(key: str, values: Mapping[str, str], *, label: str) -> str:
    value = values.get(key, "")
    return f"<label>{escape(label)}<input name='{key}' value='{escape(value)}'></label>"


def _textarea(key: str, values: Mapping[str, str], *, label: str) -> str:
    value = values.get(key, "")
    return f"<label>{escape(label)}<textarea name='{key}'>{escape(value)}</textarea></label>"


def _secret(key: str, values: Mapping[str, str], *, label: str) -> str:
    configured = "configured" if _secret_is_configured(values.get(key, "")) else "not configured"
    return (
        f"<label>{escape(label)}<input type='password' name='{key}' "
        f"placeholder='Leave blank to keep current ({configured})'></label>"
    )


def _secret_is_configured(value: str) -> bool:
    stripped = value.strip()
    return bool(stripped) and stripped.lower() not in {"replace-me", "changeme", "none", "null"}


def _provider_select(values: Mapping[str, str]) -> str:
    options = [(provider.id, provider.label) for provider in PROVIDER_PRESETS.values()]
    return _select(
        "MODEL_PROVIDER",
        values,
        label="Provider",
        options=options,
        element_id="model-provider",
    )


def _reasoning_select(values: Mapping[str, str]) -> str:
    options = [("", "Provider default")]
    options.extend((effort, effort.capitalize()) for effort in sorted(REASONING_EFFORTS))
    return _select(
        "MODEL_REASONING_EFFORT",
        values,
        label="Reasoning effort",
        options=options,
    )


def _select(
    key: str,
    values: Mapping[str, str],
    *,
    label: str,
    options: list[tuple[str, str]],
    element_id: str | None = None,
) -> str:
    selected_value = values.get(key, "")
    rendered_options = []
    for value, text in options:
        selected = " selected" if value == selected_value else ""
        rendered_options.append(
            f"<option value='{escape(value)}'{selected}>{escape(text)}</option>"
        )
    id_attribute = f" id='{escape(element_id)}'" if element_id else ""
    return (
        f"<label>{escape(label)}<select name='{key}'{id_attribute}>"
        f"{''.join(rendered_options)}</select></label>"
    )


def _provider_defaults_script() -> str:
    defaults = {
        provider.id: {
            "baseUrl": provider.base_url,
            "helpText": provider.help_text,
            "reasoningSupported": provider.supports_reasoning_effort,
            "model": provider.model,
            "reasoning": provider.reasoning_effort or "",
            "jsonMode": provider.json_mode,
            "reasoningEfforts": sorted(provider.allowed_reasoning_efforts),
            "temperatureRequiresNoReasoning": provider.temperature_requires_no_reasoning,
            "temperatureSupported": provider.supports_temperature,
        }
        for provider in PROVIDER_PRESETS.values()
    }
    encoded_defaults = json.dumps(defaults).replace("<", "\\u003c")
    return f"""
    <script>
      const providerDefaults = {encoded_defaults};
            const providerSelect = document.getElementById("model-provider");
            const apiKeyInput = document.querySelector('[name="MODEL_API_KEY"]');
            const baseUrlInput = document.querySelector('[name="MODEL_BASE_URL"]');
            const modelInput = document.querySelector('[name="MODEL_NAME"]');
            const reasoningSelect = document.querySelector('[name="MODEL_REASONING_EFFORT"]');
            const reasoningField = document.getElementById("reasoning-field");
            const temperatureInput = document.querySelector('[name="MODEL_TEMPERATURE"]');
            const temperatureField = document.getElementById("temperature-field");
            const jsonModeInput = document.querySelector('[name="MODEL_JSON_MODE"]');
            const providerHelp = document.getElementById("provider-help");
            const advancedSettings = document.querySelector("details.advanced");

            function applyProvider(resetDefaults) {{
                const defaults = providerDefaults[providerSelect.value];
        if (!defaults) return;
                if (resetDefaults) {{
                    baseUrlInput.value = defaults.baseUrl;
                    modelInput.value = defaults.model;
                    reasoningSelect.value = defaults.reasoning;
                    temperatureInput.value = "0";
                    jsonModeInput.checked = defaults.jsonMode;
                    apiKeyInput.value = "";
                    apiKeyInput.placeholder = "Enter a key for the selected provider";
                }}

                providerHelp.textContent = defaults.helpText;
                baseUrlInput.required = providerSelect.value === "openai-compatible";
                reasoningField.hidden = !defaults.reasoningSupported;
                reasoningSelect.disabled = !defaults.reasoningSupported;
                for (const option of reasoningSelect.options) {{
                    const supported = option.value === ""
                        || defaults.reasoningEfforts.includes(option.value);
                    option.hidden = !supported;
                    option.disabled = !supported;
                }}
                if (!defaults.reasoningSupported
                    || !defaults.reasoningEfforts.includes(reasoningSelect.value)) {{
                    reasoningSelect.value = "";
                }}

                const reasoningDisabled = ["", "none"].includes(reasoningSelect.value);
                const showTemperature = defaults.temperatureSupported
                    && (!defaults.temperatureRequiresNoReasoning || reasoningDisabled);
                temperatureField.hidden = !showTemperature;
                temperatureInput.disabled = !showTemperature;
                if (!showTemperature) temperatureInput.value = "0";
                if (providerSelect.value === "openai-compatible" && resetDefaults) {{
                    advancedSettings.open = true;
                }}
            }}

            providerSelect.addEventListener("change", () => applyProvider(true));
            reasoningSelect.addEventListener("change", () => applyProvider(false));
            applyProvider(false);
    </script>
    """


def _checkbox(key: str, values: Mapping[str, str], label: str) -> str:
    checked = " checked" if values.get(key, "").lower() in {"1", "true", "yes", "on"} else ""
    return f"<label><input type='checkbox' name='{key}'{checked}> {escape(label)}</label>"