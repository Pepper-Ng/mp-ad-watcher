from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from marktplaats_ad_watcher.config import Settings, parse_dotenv, write_dotenv
from marktplaats_ad_watcher.factory import build_watcher
from marktplaats_ad_watcher.model_config import (
    PROVIDER_PRESETS,
    REASONING_EFFORTS,
    provider_preset,
    resolved_model_environment,
)
from marktplaats_ad_watcher.status import RuntimeStatusStore

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


class WatcherService:
    def __init__(self, *, env_file: Path, dry_run: bool) -> None:
        self._env_file = env_file
        self._dry_run = dry_run
        self._run_lock = asyncio.Lock()
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._manual_tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

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

    @asynccontextmanager
    async def lifespan(_: Starlette):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    async def index(request: Request) -> HTMLResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return denied

        status = service.status_store().read()
        token_query = _token_query(request)
        body = f"""
        {_warning_for_missing_token(service)}
        <section>
          <h2>Status</h2>
          <table>{_status_rows(status.model_dump(mode="json"))}</table>
          <form method="post" action="/run-now{token_query}">
            <button type="submit">Run now</button>
          </form>
        </section>
        <section>
          <h2>Totals</h2>
          <table>
            {_row("Runs", status.total_runs)}
            {_row("Errors", status.total_errors)}
            {_row("Fetched results", status.total_fetched)}
            {_row("Kept after filters", status.total_kept)}
            {_row("Filtered locally", status.total_filtered)}
            {_row("New ads evaluated", status.total_evaluated)}
            {_row("Telegram triggers", status.total_notified)}
            {_row("Model ignored", status.total_ignored)}
            {_row("Model review", status.total_reviewed)}
            {_row("Model notify", status.total_notify_actions)}
          </table>
        </section>
        <p><a href="/config{token_query}">Edit configuration</a></p>
        <p><a href="/api/status{token_query}">Status JSON</a></p>
        """
        return HTMLResponse(_page("Marktplaats watcher", body))

    async def status_json(request: Request) -> JSONResponse | PlainTextResponse:
        denied = _deny_if_needed(request, service)
        if denied:
            return PlainTextResponse("Unauthorized", status_code=401)

        return JSONResponse(service.status_store().read().model_dump(mode="json"))

    async def health(_: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

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
            "SEND_IMAGE_CONTENT_TO_MODEL", values, "Send model images"
        )
        disable_previews_checkbox = _checkbox(
            "TELEGRAM_DISABLE_WEB_PAGE_PREVIEW", values, "Disable previews"
        )
        body = f"""
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

        service.queue_run_once()
        return RedirectResponse(f"/{_token_query(request)}", status_code=303)

    return Starlette(
        routes=[
            Route("/", index),
            Route("/healthz", health),
            Route("/api/status", status_json),
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
        body {{
            box-sizing: border-box;
            color: #222;
            font-family: system-ui, sans-serif;
            margin: 0 auto;
            max-width: 900px;
            padding: 1.5rem;
        }}
        h1 {{ font-size: 1.65rem; margin: 0 0 1.25rem; }}
    table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
    td, th {{ border: 1px solid #ddd; padding: 0.45rem; vertical-align: top; }}
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
        button {{
            background: #315f8c;
            border: 1px solid #274d72;
            border-radius: 5px;
            color: white;
            cursor: pointer;
            font: inherit;
            padding: 0.55rem 0.9rem;
        }}
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
        }}
  </style>
</head>
<body>
  <h1>{escape(title)}</h1>
  {body}
</body>
</html>"""


def _status_rows(status: Mapping[str, object]) -> str:
    summary = status.get("last_summary")
    summary_text = "None" if summary is None else escape(str(summary))
    return "".join(
        [
            _row("Running", status.get("is_running")),
            _row("Last started", status.get("last_started_at")),
            _row("Last finished", status.get("last_finished_at")),
            _row("Next run", status.get("next_run_at")),
            _row("Last error", status.get("last_error")),
            _row("Last summary", summary_text),
        ]
    )


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