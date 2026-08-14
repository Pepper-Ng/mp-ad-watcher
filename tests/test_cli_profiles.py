from __future__ import annotations

import json
import sys

import pytest

from marktplaats_ad_watcher import __main__ as cli
from marktplaats_ad_watcher.profiles import (
    ProfileMigrationResult,
    ProfileRegistry,
    SearchProfile,
)


def test_profile_cli_parser_supports_once_loop_and_exclusive_profile_selection() -> None:
    parser = cli.build_parser()

    selected = parser.parse_args(["--once", "--profile", "bicycles"])
    all_profiles = parser.parse_args(["--loop", "--all-profiles"])
    migration = parser.parse_args(["--migration-only"])

    assert selected.once is True
    assert selected.profile == "bicycles"
    assert selected.all_profiles is False
    assert all_profiles.loop is True
    assert all_profiles.all_profiles is True
    assert migration.migration_only is True
    with pytest.raises(SystemExit):
        parser.parse_args(["--once", "--profile", "bicycles", "--all-profiles"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--once", "--migration-only"])


def test_migration_only_activates_profiles_without_constructing_a_watcher(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile = SearchProfile(
        id="freezers",
        name="Freezers",
        search_url="https://www.marktplaats.nl/lrp/api/search?query=freezer",
        use_case="Find useful freezer chests.",
    )
    migration = ProfileMigrationResult(
        registry=ProfileRegistry(default_profile_id="freezers", profiles=(profile,)),
        migrated=True,
        manifest_path=None,
    )
    called = False

    def ensure(_: object) -> ProfileMigrationResult:
        nonlocal called
        called = True
        return migration

    def unexpected_watcher_build(_: object) -> object:
        raise AssertionError("Migration-only mode must not construct a watcher.")

    monkeypatch.setenv(
        "MARKTPLAATS_SEARCH_URL",
        "https://www.marktplaats.nl/lrp/api/search?query=freezer",
    )
    monkeypatch.setenv("MARKTPLAATS_USE_CASE", "Find useful freezer chests.")
    monkeypatch.setattr(cli, "ensure_profile_registry", ensure)
    monkeypatch.setattr(cli, "build_profile_orchestrator", unexpected_watcher_build)
    monkeypatch.setattr(sys, "argv", ["marktplaats-ad-watcher", "--migration-only"])

    cli.main()

    assert called is True
    assert json.loads(capsys.readouterr().out) == {
        "migrated": True,
        "manifest_path": None,
        "default_profile_id": "freezers",
    }
