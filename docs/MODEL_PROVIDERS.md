# Model provider configuration

The watcher separates the ad-evaluation contract from provider communication. The shared prompt,
JSON Schema, parser, and dry-run implementation live in `evaluation.py`. Provider presets and their
protocol selection live in `model_config.py`. HTTP protocol adapters live in
`model_providers.py`.

## Included providers

| Provider value | Transport | Default base URL | Default model |
| --- | --- | --- | --- |
| `deepseek` | OpenAI-compatible Chat Completions | `https://api.deepseek.com/v1` | `deepseek-v4-flash` |
| `openai` | OpenAI Responses | `https://api.openai.com/v1` | `gpt-5.6-luna` |
| `gemini` | OpenAI-compatible Chat Completions | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-3.5-flash-lite` |
| `anthropic` | Anthropic Messages | `https://api.anthropic.com` | `claude-haiku-4-5` |
| `openai-compatible` | OpenAI-compatible Chat Completions | User supplied | User supplied |

DeepSeek and Gemini need no provider-specific evaluator because both expose the widely supported
OpenAI Chat Completions request and response shape. OpenAI itself uses its newer Responses API so
reasoning controls, structured output, privacy (`store: false`), and vision use the current native
contract. Anthropic uses its native Messages API because its authentication, system prompt,
structured output, and image blocks differ from OpenAI.

## Configuration

`MODEL_PROVIDER` selects the preset. `MODEL_API_KEY` is always the selected provider's key.
`MODEL_BASE_URL` and `MODEL_NAME` override preset defaults and are required for a custom
OpenAI-compatible provider. The remaining options are:

- `MODEL_TEMPERATURE`: randomness from 0 to 2. Native adapters omit unsupported or unnecessary
  temperature settings where appropriate.
- `MODEL_MAX_TOKENS`: output limit. OpenAI Responses requires at least 16.
- `MODEL_REASONING_EFFORT`: blank for the provider default, or `none`, `minimal`, `low`, `medium`,
  `high`, `xhigh`, or `max`. Providers and individual models may support only a subset.
- `MODEL_JSON_MODE`: asks the provider for structured JSON. Keep it enabled for presets. Disable it
  for a custom compatibility endpoint that rejects `response_format`; local Pydantic validation is
  always performed.
- `SEND_IMAGE_CONTENT_TO_MODEL`: when enabled, sends image URLs using the chosen protocol's native
  content-block shape and tells the model to inspect the attached images. It is disabled by
  default; when disabled, no image URLs, image attachments, or image-specific instructions are
  sent. Only enable it for a vision-capable model.
- `MAX_IMAGES_FOR_MODEL`: caps the number of image inputs per ad.

Not every advanced control applies to every provider. The web page hides and disables controls
that the selected adapter will not send, and the backend independently omits unsupported stale
values:

| Provider | Reasoning effort | Temperature |
| --- | --- | --- |
| DeepSeek | Not sent | Sent |
| OpenAI | Sent | Sent only when reasoning is `none` |
| Gemini | `minimal`, `low`, `medium`, or `high` | Sent; exact support depends on the model |
| Anthropic Haiku preset | Not sent | Not sent |
| Custom OpenAI-compatible | Not sent | Sent; endpoint support may vary |

Maximum output tokens apply to every adapter. Structured JSON is supported by all presets; a
custom compatibility endpoint can reject the option, so JSON mode can be disabled while local
response validation remains active. Image controls apply only when the selected model actually
supports vision and are disabled by default.

The Anthropic Messages API supports effort on selected newer Opus, Sonnet, Fable, and Mythos
models, but the inexpensive default Haiku model does not. The preset therefore omits effort rather
than risking a 400 response. A future preset for an effort-capable Claude model can enable the
verified values explicitly. Likewise, custom Chat Completions stays within the portable OpenAI
fields and does not assume a non-standard `reasoning_effort` extension.

The web UI fills preset base URLs, models, reasoning defaults, and JSON mode when the provider
selection changes. It deliberately clears the previous provider's API key on a provider switch so
a DeepSeek key, for example, cannot silently be sent to OpenAI. Existing secret fields remain
write-only.

In web/Portainer mode, model and Telegram configuration is file-backed in the persistent data
volume. Process-environment model keys are deliberately ignored so stale container environment
values cannot be displayed, persisted, or revived after a provider switch. `WEB_ADMIN_TOKEN` is
the exception: it may come from the Portainer stack environment and is used without copying it
into the settings file unless the operator explicitly enters a new value in the form. CLI mode
continues to support ordinary environment variables and `.env` loading.

## Compatibility and migration

Old `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`, `DEEPSEEK_TEMPERATURE`, and
`DEEPSEEK_MAX_TOKENS` settings continue to work when no generic provider setting is present. The
web UI displays them through the generic fields and writes only `MODEL_*` names when saved.

An OpenAI-compatible endpoint is expected to accept bearer authentication and expose
`<base-url>/chat/completions`. It should return assistant text in
`choices[0].message.content`. JSON response format and `reasoning_effort` are optional provider
capabilities controlled by the settings above.

## Adding another provider

If another service supports Chat Completions, add a preset in `model_config.py` using the
`openai_chat` protocol; no new HTTP implementation is needed. If it uses a different wire format:

1. Add a new `ModelProtocol` value.
2. Implement `HttpModelEvaluator.request()` and `response_text()` in `model_providers.py`.
3. Register the adapter in `ADAPTERS`.
4. Add a provider preset that selects it.
5. Add request and response contract tests in `test_model_providers.py`.

No watcher, state, Telegram, or scheduling code should need to change. Every adapter must return an
`EvaluationResult` through the shared parser, which prevents provider-specific response shapes from
leaking into the rest of the application.

Protocol references:

- OpenAI Responses: <https://developers.openai.com/api/docs/api-reference/responses/create>
- OpenAI-compatible Gemini API: <https://ai.google.dev/gemini-api/docs/openai>
- Anthropic Messages: <https://platform.claude.com/docs/en/api/messages/create>
