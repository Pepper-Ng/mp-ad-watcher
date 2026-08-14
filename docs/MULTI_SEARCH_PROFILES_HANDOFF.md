# Multi-search profiles: implementation handoff

## Status

This document records the feasibility assessment for generalizing the watcher from one Marktplaats search to multiple independent searches. It is a handoff for the agent that implements the feature.

Implementation status:

- **Phase 1 complete:** versioned profile registry, verified legacy migration, isolated storage
  paths, and the migrated `freezers` default profile are implemented.
- **Phase 2 complete:** CLI execution activates and verifies the profile registry before running,
  can run a selected/default profile or all profiles sequentially, records profile-local schedule
  state, isolates profile failures, and shares the root model quota.
- **Phase 3 complete:** web UI profile selection, aggregate read-only scope, profile CRUD/archive,
  profile-scoped tools/actions/downloads, and profile-aware pipeline views are implemented.
- **Phase 4 complete:** Telegram headings now include safe profile labels across production,
  manual per-result, and standalone test sends; diagnostics logs now include profile context for
  profile filtering; `/healthz` reports non-ok only when migration integrity is inconsistent while
  fresh unconfigured installs remain healthy/paused.
- **Deferred:** release rollout safeguards and deployment execution remain operator-controlled.

No deployment changes have been made for this feature.

Current baseline:

- Deployed application commit: `2b4d1de32b14722d4773f30d57f5662391093961`.
- Existing production default search criteria are migrated into the `freezers` profile. Do not
  record personal search criteria in this document.
- Existing production data must be preserved, including the current seen-ad state, evaluations,
  runtime status, pipeline progress, usage history, and saved configuration.
- The latest full validation baseline is 72 passing tests, with Ruff and Pylance diagnostics clean.
- Portainer stack `17` is the only permitted deployment target. Do not modify any other Portainer object.

## Non-negotiable migration invariant

The existing freezer search is the production system of record. The multi-profile implementation
must preserve it completely and make it the **first enabled profile** after migration.

This is not a best-effort migration. The update is acceptable only if all currently persisted data
is either retained intact in its legacy location or copied and verified in the new profile storage.
No saved ad may become new again, no historical evaluation may disappear, and no migration may
cause duplicate Telegram notifications.

The migrated freezer profile must:

- Use a stable ID such as `freezers`.
- Copy the existing Marktplaats search URL and evaluation instructions exactly.
- Be enabled by default.
- Have the first persistent display/scheduling order, for example `sort_order: 0`.
- Be selected as the default profile when no profile is specified in the UI or a legacy CLI workflow.
- Retain the current seen-ad, evaluation, failure, runtime, pipeline, and model-budget behavior.

Adding later profiles must never change, replace, re-bootstrap, or silently reset the migrated
freezer profile.

## User goal

Support any practical number of independent Marktplaats searches, for example:

- Freezers
- Bicycles
- Kids' pools

Each search must have its own:

- Marktplaats search URL
- Human-readable name
- Stable identifier
- Evaluation prompt and criteria
- Seen-ad history
- Evaluation history
- Runtime status and failures
- Pipeline progress

The web UI must keep profiles separated while still offering a useful aggregate view. Telegram notifications must identify which search produced each result.

The user is asking for separate search criteria, not necessarily separate model providers or separate operating-system agents. One watcher process and the existing provider abstraction should evaluate all profiles using the profile-specific prompt.

## Feasibility and recommendation

This is **highly feasible and a medium-sized refactor**, not a rewrite. The Marktplaats client, evaluation schema, provider adapters, quota system, and Telegram transport are reusable. The main work is removing single-search assumptions from configuration, persistence, scheduling, status reporting, and UI links.

A robust production implementation should not be attempted as one untested, all-at-once patch. It should be delivered in stages with tests after each stage:

1. Profile model and persistence migration.
2. Profile-scoped runner, state, status, and pipeline records.
3. Profile-aware UI and profile management.
4. Telegram labels and notification tests.
5. Migration, regression tests, operational safeguards, and deployment validation.

An advanced agent such as Terra or Sol is a good choice if it can retain a longer multi-file context and run the full validation workflow. The current agent could also implement the feature, but should do so as several verified passes rather than one giant change. Do not combine implementation with an automatic Portainer deployment.

## Existing code and likely impact

### Reusable with minor changes

- [marktplaats.py](../src/marktplaats_ad_watcher/marktplaats.py): fetching and normalizing a search result.
- [evaluation.py](../src/marktplaats_ad_watcher/evaluation.py): shared evaluation contract and prompt construction.
- [model_providers.py](../src/marktplaats_ad_watcher/model_providers.py): provider adapters and common request path.
- [usage.py](../src/marktplaats_ad_watcher/usage.py): global daily model budget.
- [factory.py](../src/marktplaats_ad_watcher/factory.py): service construction, once profiles are passed through.

### Profile-aware components

- [config.py](../src/marktplaats_ad_watcher/config.py): currently reads one search URL and one use-case prompt.
- [runner.py](../src/marktplaats_ad_watcher/runner.py): currently executes one search and returns one run summary.
- [models.py](../src/marktplaats_ad_watcher/models.py): persisted evaluation and status models need profile identity.
- [state.py](../src/marktplaats_ad_watcher/state.py): currently has one global seen-ad mapping.
- [status.py](../src/marktplaats_ad_watcher/status.py): currently stores one global runtime status.
- [pipeline_progress.py](../src/marktplaats_ad_watcher/pipeline_progress.py): current records are keyed by ad ID only.
- [web.py](../src/marktplaats_ad_watcher/web.py): profile scoping, aggregate mode, diagnostics filtering,
  and migration-aware health behavior are implemented and should be preserved.
- [telegram.py](../src/marktplaats_ad_watcher/telegram.py): profile labels in safe HTML headings are
  implemented for production, manual, and standalone sends.

## Recommended domain model

Introduce a profile definition separate from global application settings. A profile should have a stable, non-secret identity and search-specific behavior.

Suggested fields:

- `id`: immutable, safe storage key; generated slug or UUID.
- `name`: editable human-readable label, such as `Freezers`.
- `search_url`: Marktplaats `lrp/api/search` JSON endpoint.
- `use_case`: evaluation prompt and criteria.
- `enabled`: whether the profile is scheduled.
- `sort_order`: stable integer ordering; the migrated freezer profile is `0` and appears first.
- `poll_interval_seconds`: optional override; otherwise use the global default.
- `max_ads_per_run`: optional safety limit.
- `created_at` and `updated_at`.
- Optional future fields: notification overrides, model override, archive state, prompt version.

Keep provider credentials, selected provider/model, Telegram credentials, authentication, and the daily quota global at first. Per-profile provider credentials and quotas can be added later if there is a real need.

A separate persisted profile registry such as `data/profiles.json` is preferable to encoding an arbitrary number of profiles into environment variables. The existing `settings.env` can continue to hold global settings. Never put API keys or Telegram tokens into profile records.

Example conceptual profile record:

```json
{
  "id": "freezers",
  "name": "Freezers",
  "search_url": "https://www.marktplaats.nl/lrp/api/search?...",
  "use_case": "Find reliable chest freezers ...",
  "enabled": true,
  "sort_order": 0,
  "poll_interval_seconds": 900,
  "max_ads_per_run": 10,
  "created_at": "2026-08-14T00:00:00Z",
  "updated_at": "2026-08-14T00:00:00Z"
}
```

The exact schema can differ, but profile IDs must be validated before they are used in filesystem paths. Do not derive paths directly from arbitrary display names.

## Persistence and migration

The same ad ID can legitimately occur in multiple searches. Every persisted search-specific record must therefore be qualified by profile identity.

Recommended layout:

```text
data/
  profiles.json
  profiles/
    <profile-id>/
      seen_ads.json
      evaluations.jsonl
      runtime_status.json
      pipeline_progress.json
  model_usage.json
```

Alternative storage designs are acceptable if they preserve the same isolation guarantees.

### Required migration behavior

The current deployment uses legacy single-search files such as `seen_ads.json`, `evaluations.jsonl`, `runtime_status.json`, and `pipeline_progress.json`. Migration must:

1. Detect legacy data when no profile registry exists and stop safely if its shape is invalid.
2. Create an immutable backup copy of every legacy file before changing profile storage. Keep this
  backup inside the persistent data volume and record it in migration metadata; do not rely only on
  an operator remembering to make a manual backup.
3. Create one enabled `freezers` profile with `sort_order: 0`, copying the existing search URL and
  evaluation instructions exactly. It must be the default selected profile for the UI and legacy
  CLI behavior.
4. Copy—not move or delete—the legacy `seen_ads.json`, `evaluations.jsonl`,
  `runtime_status.json`, and `pipeline_progress.json` into that profile's namespace. Preserve the
  root files until an operator deliberately removes the backup after a verified release.
5. Preserve baseline markers, processed evaluations, pending failures, Telegram delivery state, and
  model-usage history. `model_usage.json` remains a global file and must not be reset, copied into
  a profile, or double-counted.
6. Verify imported records before activating profile mode: compare seen-ad IDs, JSONL record count,
  pipeline record count, and a checksum or equivalent integrity marker for every copied file.
7. Write a versioned migration marker only after all copies and verification succeed, making the
  operation idempotent and safe to resume after a restart.
8. Never bootstrap the migrated freezer profile. Its migrated seen IDs must be used immediately so
  existing ads cannot become new or generate duplicate model calls/Telegram notifications.
9. Keep old environment variables working for CLI/backwards compatibility when no profile registry
  exists. Once the registry exists, legacy URL/prompt settings must resolve to the default
  `freezers` profile without creating a second independent search.
10. Fail safely with a clear diagnostic if a migration cannot be completed. Do not silently delete,
   overwrite, replace, or partially activate any historical data.

The current freezer search must become the first profile; it must not be re-bootstrapped after the
update. A migration that only creates an empty profile or discards current history is a release
blocker.

Evaluation records should include a profile ID and preferably a prompt/profile revision. Existing JSONL records without a profile ID should be interpreted as belonging to the migrated legacy profile.

## Runner and scheduling design

Add a profile-level execution method, conceptually `run_profile(profile)`, and retain a top-level operation that runs all enabled profiles and returns an aggregate summary.

Recommended first implementation: one process, sequential profile execution.

1. Load enabled profiles.
2. Determine which profile is due.
3. Fetch and process one profile.
4. Persist that profile's state and status.
5. Continue to the next due profile.

Sequential execution is safer initially because it limits bursts against Marktplaats, the model provider, and Telegram. One profile failing must not prevent the remaining profiles from running.

For continuous mode, track `next_run_at` per profile rather than having one global next-run timestamp. A profile-specific interval should not cause a tight loop or starve other profiles.

For a first release, all profiles should share the existing global model quota in [usage.py](../src/marktplaats_ad_watcher/usage.py). Add fairness controls such as `max_ads_per_run` and round-robin ordering so one profile with many new ads cannot consume the complete daily budget before other profiles are considered.

A later release could add concurrent execution with a global concurrency limit, but that should not be necessary for a small personal deployment.

## UI design

The current pages should remain recognizable. Add a profile selector to the Dashboard, Evaluations, Seen ads, Pipeline tools, Diagnostics, and configuration views.

Recommended behavior:

- `All searches` shows aggregate counts and recent activity.
- The migrated `Freezers` profile appears first in selectors/tabs and is the default view when a
  user has not selected another profile.
- Selecting one profile scopes cards, tables, actions, errors, and downloads to that profile.
- Every result card visibly shows the profile name.
- Profile-specific actions carry the profile ID in their form/action URL; never infer it from an untrusted display label.
- The selected profile should survive navigation through a query parameter or equivalent server-side selection.
- Downloads should be profile-scoped when a profile is selected and clearly named.
- Pipeline preview, manual AI tests, Telegram tests, and production-run actions must operate only on the selected profile.
- The UI should show disabled/paused profiles and the reason a profile is not running.

Add an authenticated profile-management page or section with create, edit, enable/disable, and safe delete/archive actions. Deletion should preferably archive a profile rather than immediately remove its history. If hard deletion is offered, require an explicit confirmation and preserve a backup-friendly export path.

For a small number of profiles, tabs are user-friendly. For an arbitrary number, use a profile selector/dropdown as the primary mechanism and optionally render tabs when there are only a few profiles.

## Telegram design

Every production notification should include a stable search label. A human-readable name is the important part; including the stable ID is useful for diagnostics.

Example heading:

```text
[Freezers · freezers] New likely Marktplaats match
```

The message should continue to include the ad title, decision, confidence, reason, and URL. Escape the profile name and all ad fields using the same HTML-safety rules as the rest of the Telegram message.

Also include the profile name/ID in:

- Standalone Telegram connectivity test messages.
- Per-result test sends from the pipeline UI.
- Persisted Telegram delivery status and diagnostics.

A result that matches two profiles should produce two clearly labeled profile records/notifications, subject to normal deduplication rules within each profile.

## Model prompting

Use the existing evaluator and pass the selected profile's `use_case` as the criteria. There is no need to launch a separate model agent for every profile.

The model prompt should also receive enough profile context to make diagnostics understandable, such as the profile name, but the profile name must not replace the actual criteria. Keep the existing strict JSON result contract.

Record the profile's prompt revision or a prompt hash with new evaluations if practical. This prevents confusion when a user edits criteria later and reviews historical decisions.

## Quota and operational limits

Keep one global daily quota initially. It protects the complete model account regardless of which profile generated a request. Successful provider responses count as they do today; HTTP/network failures release their reservation and do not count.

Recommended initial safeguards:

- Maximum new ads processed per profile per run.
- Round-robin profile ordering.
- Shared in-flight request limit if concurrency is introduced.
- Clear profile status when the global quota is exhausted.
- Avoid aggressive polling; keep Marktplaats requests low-frequency.
- Do not bypass Marktplaats bot protection or rate limits.

A few dozen low-frequency profiles should be practical. Hundreds of frequently polling profiles would require explicit rate limiting, scheduling, monitoring, and probably a different deployment architecture.

## Backwards compatibility and public API caution

Preserve current single-search behavior when the profile registry is absent or contains only one profile. Existing environment variables should continue to work for CLI usage:

- `MARKTPLAATS_SEARCH_URL`
- `MARKTPLAATS_USE_CASE`
- Existing global model, Telegram, polling, and notification settings

Avoid breaking existing public method signatures unless necessary. Where possible, add profile-aware methods and keep compatibility wrappers for current tests and callers. The existing web routes should continue to work, with an optional profile selector added rather than replacing all routes.

## Test plan and acceptance criteria

Add focused tests before deployment. At minimum:

### Profile/configuration tests

- Create, edit, disable, enable, and validate profiles.
- Reject invalid URLs, empty prompts, duplicate IDs, unsafe IDs, and invalid intervals.
- Preserve global settings and secrets.
- Migrate the legacy single-search configuration exactly once.
- Migrate the legacy freezer configuration into the enabled default `freezers` profile with
  `sort_order: 0` and unchanged URL/prompt values.

### State/evaluation isolation tests

- The same ad ID can exist independently in two profiles.
- Baseline state is isolated per profile.
- An evaluation or failure in one profile does not change another profile.
- Existing legacy records are assigned to the migrated profile without duplication.
- Legacy source files are retained as a verified backup after successful migration.
- Seen-ad IDs, evaluation count, pipeline records, failures, and model usage are identical before
  and after migration.
- Prompt/profile revision metadata is persisted when implemented.

### Runner/scheduling tests

- All enabled profiles are processed.
- Disabled profiles are skipped.
- One profile's fetch/model/Telegram failure does not stop later profiles.
- Per-profile intervals and next-run values are respected.
- Global quota is shared and enforced across profiles.
- Per-profile run limits prevent one profile from starving others.

### Web/pipeline tests

- Profile selector and `All searches` views render correctly.
- Every profile-specific action uses the correct profile.
- Evaluations, seen ads, diagnostics, downloads, and pipeline progress do not leak between profiles.
- Profile management is authenticated.
- Existing single-profile routes and behavior remain functional.

### Telegram tests

- Notifications contain the profile label and stable ID as designed.
- HTML escaping works for profile names and ad data.
- Manual and standalone test messages identify the profile.
- A Telegram failure is recorded against the correct profile.

### Regression validation

Run the complete suite, Ruff, and Pylance diagnostics. Before any production update, back up the persistent watcher data and validate migration against a copy of the current volume.

## Suggested implementation order

### Phase 1: domain and migration

- Add `SearchProfile` and profile registry storage.
- Add profile identity to evaluation, failure, status, and pipeline models.
- Implement idempotent migration from legacy single-search files.
- Add unit tests and verify existing tests remain green.

### Phase 2: execution

- Refactor the runner into profile-level and aggregate operations.
- Add per-profile state paths and next-run tracking.
- Keep the global quota path unchanged.
- Add failure isolation and fairness limits.

### Phase 3: UI

- Add profile selection and `All searches` aggregation.
- Scope existing pages and actions by profile.
- Add authenticated profile management.
- Add profile-aware download and pipeline behavior.

### Phase 4: Telegram and polish

- Add profile labels to all notification paths.
- Add profile context to diagnostics and delivery state.
- Improve empty/disabled/quota-exhausted states.
- Add end-to-end web and Telegram tests.

### Phase 5: release validation

- Run the full local test/lint/diagnostic suite.
- Test migration and rollback using a data-volume copy.
- Test the migration against a copy of the actual current watcher-data volume, not only fabricated
  fixtures. Verify that the `Freezers` profile is first, enabled, selected by default, and has the
  same counts/checksums as the legacy data before deployment.
- Review the diff for secrets and personal search data.
- Commit the implementation separately from deployment.
- Deploy only after explicit approval, and only to Portainer stack `17`.
- Verify container health, profile migration, existing freezer history, and labeled notifications.

## Definition of done

The feature is ready when:

- Multiple profiles can be created without editing environment variables.
- Each profile can use a different URL and evaluation prompt.
- Profiles have isolated state, evaluations, failures, statuses, and pipeline progress.
- The existing freezer search and its history survive migration unchanged.
- The existing freezer search is the first enabled/default profile, with its original URL, prompt,
  seen state, evaluations, status, pipeline records, and global usage accounting preserved.
- Legacy single-search files remain intact as a verified backup until an operator explicitly
  approves their removal.
- The UI can inspect one profile or all profiles without ambiguity.
- Telegram messages clearly identify the originating profile.
- One profile cannot prevent the others from running.
- The global daily model budget remains enforced.
- Tests cover migration, isolation, scheduling, UI actions, Telegram labels, and regressions.
- No Portainer deployment has occurred without explicit approval.
