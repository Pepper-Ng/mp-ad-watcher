# Marktplaats Ad Watcher

Small local watcher that polls a Marktplaats search, evaluates only newly seen ads with a
configurable model provider, writes JSONL evaluation results, and optionally sends Telegram
notifications.

## What it does

- Polls one Marktplaats search URL every `POLL_INTERVAL_SECONDS`, defaulting to 10 minutes.
- Keeps `STATE_FILE` so older ads are not evaluated again.
- On the first normal run, marks the current result set as seen by default. This avoids notifying you about all existing ads.
- Filters out `a...` Admarkt/commercial-style listings by default.
- Loads the full Marktplaats detail page for each newly found ad before AI evaluation, including the
  complete description and visible listing characteristics. Existing/baseline ads are not reloaded
  on every poll.
- Sends each new ad to a strict JSON evaluator.
- Appends each evaluation to `RESULTS_FILE`, so another flow can consume it.
- Sends a Telegram message for `next_action="notify"`, and also for `next_action="review"` by default so plausible ads are not missed.
- Tracks runtime status in `STATUS_FILE` and exposes a small optional web UI.

## Setup

From this folder:

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -e .[dev]
Copy-Item .env.example .env
```

Then edit `.env`.

The most important values are:

```env
MARKTPLAATS_SEARCH_URL=https://www.marktplaats.nl/lrp/api/search?limit=30&offset=0&query=example&postcode=1234AB&distanceMeters=10000&searchInTitleAndDescription=true
MARKTPLAATS_USE_CASE=Describe exactly what should count as relevant.
EXCLUDE_ADMARKT_ADS=true
MODEL_PROVIDER=deepseek
MODEL_API_KEY=your-key
TELEGRAM_BOT_TOKEN=optional
TELEGRAM_CHAT_ID=optional
WEB_ADMIN_TOKEN=use-a-long-random-token-if-serving-the-web-ui
```

## Marktplaats search URL

Use the JSON search endpoint used by the Marktplaats result page:

1. Open your Marktplaats search in a browser.
2. Open developer tools and go to Network.
3. Filter for `lrp/api/search`.
4. Copy the request URL and paste it into `MARKTPLAATS_SEARCH_URL`.

Keep the interval modest. This project is intended for personal low-frequency polling and does not try to bypass access controls, rate limits, or bot protection.

## Run

Inspect one scan without model calls, seen-ad/evaluation writes, or Telegram. The runtime
status file is still updated so the web status page can report the run:

```powershell
.\.venv\Scripts\python -m marktplaats_ad_watcher --once --dry-run
```

Run once normally:

```powershell
.\.venv\Scripts\python -m marktplaats_ad_watcher --once
```

Run continuously:

```powershell
.\.venv\Scripts\python -m marktplaats_ad_watcher --loop
```

`--loop` is the default when no mode is provided.

Run with the small web UI:

```powershell
.\.venv\Scripts\python -m marktplaats_ad_watcher --serve --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080/?token=<WEB_ADMIN_TOKEN>` when `WEB_ADMIN_TOKEN` is set.
The dashboard shows formatted run status and activity totals. Protected pages provide evaluation
history with decision filtering and JSON download, seen-ad history with baseline reasons, and
staged pipeline tools. Fetch preview changes no state. A successful manual AI phase persists the
interpreted evaluation, marks that ad processed, and clears its current AI failure without sending
Telegram. Saved results expose explicit per-result Telegram actions; a standalone connectivity test
is also available. In profile-aware mode, production notifications, per-result sends, and
standalone test messages include a profile heading label such as `[Freezers · freezers]`; labels
are HTML-escaped before sending. A full production run remains a separate confirmed action. The configuration
page edits the poll interval, prompt, model settings, and API keys. Existing API keys are never
shown; leaving a secret field blank keeps the current value, except that changing providers clears
the previous provider key. Failed AI evaluations remain pending for retry and are shown with safe
provider error details. A protected diagnostics page keeps a bounded, token-redacted view of recent
application logs; complete container output remains available through Portainer. Production and
manual AI tests share a persistent UTC-daily request budget of 30 successful responses by default.
Failed HTTP/network attempts release their reservation and do not count. The UI shows usage,
remaining requests, in-flight reservations, and reset time; increasing or resetting the budget
requires confirmation and applies immediately through the shared quota file.

## Model providers

Choose a provider in the web configuration page and enter its API key. Presets supply the
protocol, base URL, and a practical default model:

| Provider | Protocol | Default model |
| --- | --- | --- |
| DeepSeek | OpenAI-compatible Chat Completions | `deepseek-v4-flash` |
| OpenAI | Native Responses API | `gpt-5.6-luna` with medium reasoning |
| Google Gemini | OpenAI-compatible Chat Completions | `gemini-3.5-flash-lite` |
| Anthropic | Native Messages API | `claude-haiku-4-5` |
| Custom OpenAI-compatible | OpenAI-compatible Chat Completions | User-supplied URL and model |

The provider-neutral environment variables are `MODEL_PROVIDER`, `MODEL_API_KEY`,
`MODEL_BASE_URL`, `MODEL_NAME`, `MODEL_TEMPERATURE`, `MODEL_MAX_TOKENS`,
`MODEL_REASONING_EFFORT`, and `MODEL_JSON_MODE`. DeepSeek's former `DEEPSEEK_*` variables remain
accepted for backwards compatibility and are migrated to the generic names the next time the web
configuration is saved.

Provider adapters share one validated evaluation schema. OpenAI uses native JSON Schema output;
Anthropic uses its native structured-output format; OpenAI-compatible providers use JSON mode and
the result is validated locally. Disable `MODEL_JSON_MODE` only when a custom compatibility
endpoint rejects response-format options.

The configuration page keeps common provider fields visible and places optional controls under
**Advanced model settings**. Provider-specific fields are shown only when applicable; unsupported
values are also omitted by the backend rather than being sent and causing avoidable API errors.

See [docs/MODEL_PROVIDERS.md](docs/MODEL_PROVIDERS.md) for protocol details, migration behavior,
and instructions for adding another provider or wire protocol.

## Telegram

Create a bot with BotFather, send it a message, then use the Bot API `getUpdates` endpoint to find your chat ID. Fill these values in `.env`:

```env
TELEGRAM_BOT_TOKEN=123456:abc
TELEGRAM_CHAT_ID=123456789
```

If Telegram values are empty, the watcher still writes JSONL evaluation results.

By default, both `notify` and `review` model actions trigger Telegram. The message heading differs:

- `notify`: likely match.
- `review`: check specs.
- `ignore`: no Telegram message.

When a profile is selected, the heading is prefixed with a visible profile label such as
`[Freezers · freezers]`. Profile labels are HTML-escaped. Legacy records with no profile metadata
retain the legacy heading when no active profile context is available.

Tune this with:

```env
NOTIFY_REVIEW_ACTIONS=true
NOTIFY_MIN_CONFIDENCE=0.65
REVIEW_MIN_CONFIDENCE=0
```

## Image handling

By default the evaluator receives no image URLs, image attachments, or image-specific instructions.
To ask a vision-capable model to inspect listing images, enable this in the web configuration page
or set:

```env
SEND_IMAGE_CONTENT_TO_MODEL=true
```

The code sends up to `MAX_IMAGES_FOR_MODEL` images using the content shape required by the chosen
protocol: OpenAI-compatible `image_url`, OpenAI Responses `input_image`, or Anthropic URL image
blocks. The system prompt also explicitly tells the model to inspect the attached images. Leave
this disabled for text-only models or when image analysis is not wanted.

## JSON evaluation shape

Each evaluated ad is appended to `data/evaluations.jsonl` with this structure:

```json
{
  "ad": {
    "id": "m123",
    "title": "Example",
    "url": "https://www.marktplaats.nl/v/...",
    "description": "...",
    "price": "EUR 125.00",
    "location": "Eindhoven",
    "seller": "Sam",
    "image_urls": []
  },
  "result": {
    "relevant": true,
    "confidence": 0.86,
    "reason": "Short practical explanation.",
    "signals": ["matched requirement"],
    "concerns": ["missing detail"],
    "next_action": "notify"
  },
  "evaluated_at": "2026-07-24T12:00:00Z",
  "profile_id": "freezers",
  "profile_name": "Freezers"
}
```

`profile_id` and `profile_name` are present for profile-aware evaluations and may be absent in
legacy single-search records.

## Scheduled mode

For a Windows scheduled task, prefer `--once` every 10 minutes. Continuous `--loop` also works if you run it as a long-lived process.

## Docker / Portainer

The container includes the watcher, web configuration page, persistent data directory,
non-root runtime user, and a Docker health check. It starts without a model key, but it will
not process ads until the required search URL and use-case prompt have been configured.

Build and run locally:

```powershell
docker build -t marktplaats-ad-watcher .
docker run --rm -p 8080:8080 -v marktplaats-ad-watcher-data:/app/data -e WEB_ADMIN_TOKEN=<long-random-token> marktplaats-ad-watcher
```

For Portainer, use a **Docker Standalone** environment and deploy `compose.yaml` as a Git-backed
stack. Set the stack environment variable `WEB_ADMIN_TOKEN` to a long random value. Optionally
set `WATCHER_PORT` when host port 8080 is already occupied. A short-lived initialization
container makes the named volume writable by the non-root watcher. The watcher stores editable
settings, seen ads, evaluations, and runtime status in that volume.

First open:

```text
http://<host>:8080/?token=<WEB_ADMIN_TOKEN>
```

Then fill in the Marktplaats URL, evaluation prompt, model provider and API key, Telegram bot token,
and Telegram chat ID from the web configuration page.

A domain is not required: the UI works at `http://<docker-host>:8080` on a local network.
Use a reverse proxy with HTTPS instead of exposing port 8080 directly to the internet.

See [docs/PORTAINER.md](docs/PORTAINER.md) for the complete GitHub branch, GitOps update,
first-run configuration, backup, security, and troubleshooting guide.

## Tests

```powershell
.\.venv\Scripts\python -m pytest
```
