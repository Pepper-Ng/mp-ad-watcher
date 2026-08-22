from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from marktplaats_ad_watcher.config import Settings
from marktplaats_ad_watcher.profiles import (
    DEFAULT_PROFILE_ID,
    LEGACY_MIGRATION_NAME,
    ProfileConfigurationError,
    ProfileMigrationError,
    ProfileRegistry,
    ProfileRegistryStore,
    SearchProfile,
    migrate_legacy_single_search,
    verify_profile_registry,
)


def _settings(data_root: Path) -> Settings:
    return Settings.from_environment(
        {
            "MARKTPLAATS_SEARCH_URL": "https://www.marktplaats.nl/lrp/api/search?query=vrieskist",
            "MARKTPLAATS_USE_CASE": "Find dependable freezer chests with useful capacity details.",
            "STATE_FILE": str(data_root / "seen_ads.json"),
            "RESULTS_FILE": str(data_root / "evaluations.jsonl"),
            "STATUS_FILE": str(data_root / "runtime_status.json"),
            "BOOTSTRAP_EXISTING_ADS": "true",
        }
    )


def _legacy_files(data_root: Path) -> dict[str, bytes]:
    return {
        "seen_ads.json": (
            b'{\n  "seen_ads": {\n    "m1": {"title": "Existing freezer"},\n'
            b'    "m2": {"title": "Another freezer"}\n  }\n}\n'
        ),
        "evaluations.jsonl": b'{"ad":{"id":"m1"}}\n\n{"ad":{"id":"m2"}}\n',
        "runtime_status.json": b'{"total_runs": 4, "total_evaluated": 2}\n',
        "pipeline_progress.json": (
            b'{"records":{"m1":{"source":"production"},'
            b'"m2":{"source":"manual_test"}}}\n'
        ),
        "model_usage.json": b'{"day":"2026-08-14","used":7,"limit":30}\n',
    }


def _write_legacy_files(data_root: Path, files: dict[str, bytes]) -> None:
    data_root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (data_root / name).write_bytes(content)


@pytest.mark.parametrize(
    "profile_id",
    [
        "",
        "all",
        "Freezers",
        "freezers/other",
        "../freezers",
        "freezers..",
        "freezers_name",
    ],
)
def test_profile_registry_rejects_unsafe_profile_ids(profile_id: str) -> None:
    with pytest.raises(ProfileConfigurationError, match="Profile ID"):
        SearchProfile(
            id=profile_id,
            name="Freezers",
            search_url="https://www.marktplaats.nl/lrp/api/search?query=vrieskist",
            use_case="Find freezer chests.",
        )


def test_profile_registry_rejects_invalid_profile_values_and_duplicate_ids() -> None:
    with pytest.raises(ProfileConfigurationError, match="HTTP"):
        SearchProfile(
            id="freezers",
            name="Freezers",
            search_url="not-a-url",
            use_case="Find freezer chests.",
        )
    with pytest.raises(ProfileConfigurationError, match="instructions"):
        SearchProfile(
            id="freezers",
            name="Freezers",
            search_url="https://www.marktplaats.nl/lrp/api/search?query=vrieskist",
            use_case="   ",
        )

    profile = SearchProfile(
        id="freezers",
        name="Freezers",
        search_url="https://www.marktplaats.nl/lrp/api/search?query=vrieskist",
        use_case="Find freezer chests.",
    )
    with pytest.raises(ProfileConfigurationError, match="duplicate profile IDs"):
        ProfileRegistry(default_profile_id="freezers", profiles=(profile, profile))


def test_profile_search_url_defaults_ui_sort_when_missing() -> None:
    profile = SearchProfile(
        id="freezers",
        name="Freezers",
        search_url=(
            "https://www.marktplaats.nl/lrp/api/search?"
            "limit=30&offset=0&query=vrieskist&postcode=6005JW&"
            "distanceMeters=30000&searchInTitleAndDescription=true"
        ),
        use_case="Find freezer chests.",
    )

    parsed = urlsplit(profile.search_url)
    params = parse_qs(parsed.query)
    assert params["sortBy"] == ["SORT_INDEX"]
    assert params["sortOrder"] == ["DECREASING"]


def test_migration_copies_legacy_data_with_verified_manifest_and_global_quota(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    legacy_files = _legacy_files(data_root)
    _write_legacy_files(data_root, legacy_files)
    settings = _settings(data_root)

    result = migrate_legacy_single_search(settings)

    assert result.migrated is True
    assert result.manifest_path == (
        data_root / "profile-migrations" / f"{LEGACY_MIGRATION_NAME}.json"
    )
    registry = ProfileRegistryStore(data_root).load()
    profile = registry.default_profile
    assert profile.id == DEFAULT_PROFILE_ID
    assert profile.name == "Freezers"
    assert profile.search_url == settings.marktplaats_search_url
    assert profile.use_case == settings.marktplaats_use_case
    assert profile.enabled is True
    assert profile.sort_order == 0
    assert profile.bootstrap_existing_ads is False

    resolved = settings.for_profile(profile)
    assert resolved.active_profile_id == DEFAULT_PROFILE_ID
    assert resolved.bootstrap_existing_ads is False
    assert resolved.global_model_usage_file == data_root / "model_usage.json"

    profile_directory = data_root / "profiles" / DEFAULT_PROFILE_ID
    backup_directory = data_root / "profile-migration-backups" / LEGACY_MIGRATION_NAME
    for name in (
        "seen_ads.json",
        "evaluations.jsonl",
        "runtime_status.json",
        "pipeline_progress.json",
    ):
        assert (data_root / name).read_bytes() == legacy_files[name]
        assert (profile_directory / name).read_bytes() == legacy_files[name]
        assert (backup_directory / name).read_bytes() == legacy_files[name]

    assert (data_root / "model_usage.json").read_bytes() == legacy_files["model_usage.json"]
    assert not (profile_directory / "model_usage.json").exists()

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    integrity_by_name = {entry["name"]: entry for entry in manifest["files"]}
    expected_counts = {
        "seen_ads.json": 2,
        "evaluations.jsonl": 2,
        "runtime_status.json": 1,
        "pipeline_progress.json": 2,
    }
    for name, count in expected_counts.items():
        integrity = integrity_by_name[name]
        digest = hashlib.sha256(legacy_files[name]).hexdigest()
        assert integrity["record_count"] == count
        assert integrity["byte_count"] == len(legacy_files[name])
        assert integrity["source_sha256"] == digest
        assert integrity["backup_sha256"] == digest
        assert integrity["profile_sha256"] == digest

    usage_integrity = manifest["global_model_usage"]
    assert usage_integrity["record_count"] == 1
    assert usage_integrity["source_sha256"] == hashlib.sha256(
        legacy_files["model_usage.json"]
    ).hexdigest()

    verification = verify_profile_registry(settings)
    assert verification.migrated is False
    assert verification.manifest_path == result.manifest_path
    assert verification.registry.default_profile_id == DEFAULT_PROFILE_ID


def test_migration_is_idempotent_and_retains_legacy_originals(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    legacy_files = _legacy_files(data_root)
    _write_legacy_files(data_root, legacy_files)
    settings = _settings(data_root)

    first = migrate_legacy_single_search(settings)
    assert first.manifest_path is not None
    first_manifest = first.manifest_path.read_bytes()
    root_before_second_migration = {
        name: (data_root / name).read_bytes() for name in legacy_files
    }

    second = migrate_legacy_single_search(settings)

    assert second.migrated is False
    assert second.manifest_path == first.manifest_path
    assert second.manifest_path.read_bytes() == first_manifest
    assert {
        name: (data_root / name).read_bytes() for name in legacy_files
    } == root_before_second_migration
    assert ProfileRegistryStore(data_root).load().default_profile.id == DEFAULT_PROFILE_ID


def test_migration_preserves_empty_legacy_state_without_bootstrap(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    legacy_files = {
        "seen_ads.json": b'{"seen_ads": {}}\n',
        "evaluations.jsonl": b"",
        "runtime_status.json": b"{}\n",
        "pipeline_progress.json": b'{"records": {}}\n',
    }
    _write_legacy_files(data_root, legacy_files)
    settings = _settings(data_root)

    result = migrate_legacy_single_search(settings)

    assert result.migrated is True
    profile = ProfileRegistryStore(data_root).load().default_profile
    resolved = settings.for_profile(profile)
    profile_state = data_root / "profiles" / DEFAULT_PROFILE_ID / "seen_ads.json"
    assert profile.bootstrap_existing_ads is False
    assert resolved.bootstrap_existing_ads is False
    assert profile_state.read_bytes() == legacy_files["seen_ads.json"]
    assert b"bootstrapped" not in profile_state.read_bytes()


def test_invalid_legacy_data_fails_without_registry_or_partial_migration(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    legacy_files = _legacy_files(data_root)
    legacy_files["evaluations.jsonl"] = b'{"ad":\n'
    _write_legacy_files(data_root, legacy_files)
    before = {name: (data_root / name).read_bytes() for name in legacy_files}

    with pytest.raises(ProfileMigrationError, match="invalid JSON"):
        migrate_legacy_single_search(_settings(data_root))

    assert {name: (data_root / name).read_bytes() for name in legacy_files} == before
    assert not (data_root / "profiles.json").exists()
    assert not (data_root / "profiles").exists()
    assert not (data_root / "profile-migrations").exists()
    assert not (data_root / "profile-migration-backups").exists()


def test_registry_atomic_mutations_keep_ids_immutable_and_archive_history(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    settings = _settings(data_root)
    registry = migrate_legacy_single_search(settings).registry
    store = ProfileRegistryStore(data_root)
    bicycles = SearchProfile(
        id="bicycles",
        name="Bicycles",
        search_url="https://www.marktplaats.nl/lrp/api/search?query=bicycle",
        use_case="Find reliable city bicycles.",
        sort_order=1,
    )

    store.create(bicycles)
    updated = store.update(replace(bicycles, name="City bicycles", enabled=False))
    archived = store.archive("bicycles")

    assert registry.default_profile_id == DEFAULT_PROFILE_ID
    assert updated.profile("bicycles").id == "bicycles"
    assert updated.profile("bicycles").name == "City bicycles"
    assert archived.profile("bicycles").archived is True
    assert archived.profile("bicycles").enabled is False
    assert [profile.id for profile in archived.active_profiles] == [DEFAULT_PROFILE_ID]
    with pytest.raises(ProfileConfigurationError, match="default Freezers"):
        store.archive(DEFAULT_PROFILE_ID)
    with pytest.raises(ProfileConfigurationError, match="Archived"):
        store.set_enabled("bicycles", enabled=True)
