from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from marktplaats_ad_watcher.config import Settings

DEFAULT_PROFILE_ID = "freezers"
PROFILE_REGISTRY_FILENAME = "profiles.json"
PROFILE_DIRECTORY_NAME = "profiles"
MIGRATION_DIRECTORY_NAME = "profile-migrations"
MIGRATION_BACKUP_DIRECTORY_NAME = "profile-migration-backups"
LEGACY_MIGRATION_NAME = "legacy-single-search-v1"
PROFILE_REGISTRY_SCHEMA_VERSION = 1
MIGRATION_MANIFEST_SCHEMA_VERSION = 1

_PROFILE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_MIGRATION_LOCK = threading.Lock()


class ProfileConfigurationError(ValueError):
    """Raised when a persisted profile registry is malformed or unsafe."""


class ProfileMigrationError(RuntimeError):
    """Raised when legacy data cannot be copied and verified without data loss."""


@dataclass(frozen=True)
class SearchProfile:
    """The non-secret configuration and bootstrap policy for one saved search."""

    id: str
    name: str
    search_url: str
    use_case: str
    enabled: bool = True
    sort_order: int = 0
    bootstrap_existing_ads: bool = False

    def __post_init__(self) -> None:
        _validate_profile_id(self.id)
        if not isinstance(self.name, str) or not self.name.strip():
            raise ProfileConfigurationError("Profile name must not be empty.")
        _validate_http_url("Profile search URL", self.search_url)
        if not isinstance(self.use_case, str) or not self.use_case.strip():
            raise ProfileConfigurationError("Profile evaluation instructions must not be empty.")
        if not isinstance(self.enabled, bool):
            raise ProfileConfigurationError("Profile enabled must be a boolean.")
        if not isinstance(self.sort_order, int) or isinstance(self.sort_order, bool):
            raise ProfileConfigurationError("Profile sort_order must be an integer.")
        if not isinstance(self.bootstrap_existing_ads, bool):
            raise ProfileConfigurationError("Profile bootstrap_existing_ads must be a boolean.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "search_url": self.search_url,
            "use_case": self.use_case,
            "enabled": self.enabled,
            "sort_order": self.sort_order,
            "bootstrap_existing_ads": self.bootstrap_existing_ads,
        }

    @classmethod
    def from_dict(cls, value: Any) -> SearchProfile:
        if not isinstance(value, dict):
            raise ProfileConfigurationError("Each profile must be a JSON object.")
        return cls(
            id=_required_string(value, "id", "Profile"),
            name=_required_string(value, "name", "Profile"),
            search_url=_required_string(value, "search_url", "Profile"),
            use_case=_required_string(value, "use_case", "Profile"),
            enabled=value.get("enabled", True),
            sort_order=value.get("sort_order", 0),
            bootstrap_existing_ads=value.get("bootstrap_existing_ads", False),
        )


@dataclass(frozen=True)
class ProfileRegistry:
    """Versioned persistent collection of profiles with a stable default."""

    default_profile_id: str
    profiles: tuple[SearchProfile, ...]
    schema_version: int = PROFILE_REGISTRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PROFILE_REGISTRY_SCHEMA_VERSION:
            raise ProfileConfigurationError(
                f"Unsupported profile registry schema version: {self.schema_version}."
            )
        _validate_profile_id(self.default_profile_id)
        if not self.profiles:
            raise ProfileConfigurationError("Profile registry must contain at least one profile.")
        if not all(isinstance(profile, SearchProfile) for profile in self.profiles):
            raise ProfileConfigurationError("Profile registry contains an invalid profile.")

        ids = [profile.id for profile in self.profiles]
        if len(ids) != len(set(ids)):
            raise ProfileConfigurationError("Profile registry contains duplicate profile IDs.")
        if self.default_profile_id not in ids:
            raise ProfileConfigurationError("Default profile ID does not exist in the registry.")

        sort_orders = [profile.sort_order for profile in self.profiles]
        if len(sort_orders) != len(set(sort_orders)):
            raise ProfileConfigurationError(
                "Profile registry contains duplicate sort_order values."
            )

    @property
    def default_profile(self) -> SearchProfile:
        return self.profile(self.default_profile_id)

    def profile(self, profile_id: str) -> SearchProfile:
        for profile in self.profiles:
            if profile.id == profile_id:
                return profile
        raise ProfileConfigurationError(f"Unknown profile ID: {profile_id}.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "default_profile_id": self.default_profile_id,
            "profiles": [profile.to_dict() for profile in self.profiles],
        }

    @classmethod
    def from_dict(cls, value: Any) -> ProfileRegistry:
        if not isinstance(value, dict):
            raise ProfileConfigurationError("Profile registry must be a JSON object.")

        schema_version = value.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ProfileConfigurationError("Profile registry schema_version must be an integer.")
        profiles_value = value.get("profiles")
        if not isinstance(profiles_value, list):
            raise ProfileConfigurationError("Profile registry profiles must be a JSON array.")

        return cls(
            schema_version=schema_version,
            default_profile_id=_required_string(value, "default_profile_id", "Profile registry"),
            profiles=tuple(SearchProfile.from_dict(profile) for profile in profiles_value),
        )


@dataclass(frozen=True)
class ProfileStoragePaths:
    """Filesystem locations owned by one validated profile ID."""

    data_root: Path
    profile_id: str

    def __post_init__(self) -> None:
        _validate_profile_id(self.profile_id)

    @property
    def directory(self) -> Path:
        return self.data_root / PROFILE_DIRECTORY_NAME / self.profile_id

    @property
    def state_file(self) -> Path:
        return self.directory / "seen_ads.json"

    @property
    def results_file(self) -> Path:
        return self.directory / "evaluations.jsonl"

    @property
    def status_file(self) -> Path:
        return self.directory / "runtime_status.json"

    @property
    def pipeline_progress_file(self) -> Path:
        return self.directory / "pipeline_progress.json"


@dataclass(frozen=True)
class MigrationFileIntegrity:
    name: str
    source_exists: bool
    source_sha256: str | None
    backup_sha256: str | None
    profile_sha256: str | None
    byte_count: int
    record_count: int

    def __post_init__(self) -> None:
        if self.name not in (*_LEGACY_FILE_NAMES, "model_usage.json"):
            raise ProfileMigrationError(
                f"Unsupported legacy file in migration manifest: {self.name}."
            )
        if self.byte_count < 0 or self.record_count < 0:
            raise ProfileMigrationError("Migration manifest counts must not be negative.")
        hashes = (self.source_sha256, self.backup_sha256, self.profile_sha256)
        if self.source_exists:
            if any(not _is_sha256(value) for value in hashes):
                raise ProfileMigrationError("Migration manifest contains an invalid file hash.")
            if len(set(hashes)) != 1:
                raise ProfileMigrationError("Migration manifest file hashes do not agree.")
        elif any(value is not None for value in hashes) or self.byte_count or self.record_count:
            raise ProfileMigrationError("Absent migration files must not have hashes or counts.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_exists": self.source_exists,
            "source_sha256": self.source_sha256,
            "backup_sha256": self.backup_sha256,
            "profile_sha256": self.profile_sha256,
            "byte_count": self.byte_count,
            "record_count": self.record_count,
        }

    @classmethod
    def from_dict(cls, value: Any) -> MigrationFileIntegrity:
        if not isinstance(value, dict):
            raise ProfileMigrationError("Migration manifest file entry must be a JSON object.")
        name = value.get("name")
        source_exists = value.get("source_exists")
        byte_count = value.get("byte_count")
        record_count = value.get("record_count")
        if not isinstance(name, str) or not isinstance(source_exists, bool):
            raise ProfileMigrationError("Migration manifest file entry is malformed.")
        if (
            not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
            or not isinstance(record_count, int)
            or isinstance(record_count, bool)
        ):
            raise ProfileMigrationError("Migration manifest counts must be integers.")
        return cls(
            name=name,
            source_exists=source_exists,
            source_sha256=value.get("source_sha256"),
            backup_sha256=value.get("backup_sha256"),
            profile_sha256=value.get("profile_sha256"),
            byte_count=byte_count,
            record_count=record_count,
        )


@dataclass(frozen=True)
class MigrationManifest:
    profile_id: str
    created_at: str
    files: tuple[MigrationFileIntegrity, ...]
    global_model_usage: MigrationFileIntegrity
    schema_version: int = MIGRATION_MANIFEST_SCHEMA_VERSION
    migration: str = LEGACY_MIGRATION_NAME

    def __post_init__(self) -> None:
        if self.schema_version != MIGRATION_MANIFEST_SCHEMA_VERSION:
            raise ProfileMigrationError(
                f"Unsupported migration manifest schema version: {self.schema_version}."
            )
        if self.migration != LEGACY_MIGRATION_NAME:
            raise ProfileMigrationError(f"Unsupported migration marker: {self.migration}.")
        _validate_profile_id(self.profile_id)
        if self.profile_id != DEFAULT_PROFILE_ID:
            raise ProfileMigrationError("Legacy migration must target the freezers profile.")
        try:
            datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as error:
            raise ProfileMigrationError(
                "Migration manifest created_at must be an ISO timestamp."
            ) from error

        file_names = {file.name for file in self.files}
        if file_names != set(_LEGACY_FILE_NAMES) or len(file_names) != len(self.files):
            raise ProfileMigrationError(
                "Migration manifest does not cover every legacy search file."
            )
        if self.global_model_usage.name != "model_usage.json":
            raise ProfileMigrationError("Migration manifest has an invalid global usage entry.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "migration": self.migration,
            "profile_id": self.profile_id,
            "created_at": self.created_at,
            "files": [file.to_dict() for file in self.files],
            "global_model_usage": self.global_model_usage.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> MigrationManifest:
        if not isinstance(value, dict):
            raise ProfileMigrationError("Migration manifest must be a JSON object.")
        schema_version = value.get("schema_version")
        files = value.get("files")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ProfileMigrationError("Migration manifest schema_version must be an integer.")
        if not isinstance(files, list):
            raise ProfileMigrationError("Migration manifest files must be a JSON array.")
        migration = value.get("migration")
        profile_id = value.get("profile_id")
        created_at = value.get("created_at")
        if not isinstance(migration, str):
            raise ProfileMigrationError("Migration manifest migration is malformed.")
        if not isinstance(profile_id, str):
            raise ProfileMigrationError("Migration manifest profile_id is malformed.")
        if not isinstance(created_at, str):
            raise ProfileMigrationError("Migration manifest string fields are malformed.")
        return cls(
            schema_version=schema_version,
            migration=migration,
            profile_id=profile_id,
            created_at=created_at,
            files=tuple(MigrationFileIntegrity.from_dict(file) for file in files),
            global_model_usage=MigrationFileIntegrity.from_dict(value.get("global_model_usage")),
        )

    def verify_profile_copies(self, data_root: Path) -> None:
        storage = ProfileStoragePaths(data_root, self.profile_id)
        backup_directory = _migration_backup_directory(data_root)
        for file in self.files:
            profile_path = storage.directory / file.name
            backup_path = backup_directory / file.name
            if not file.source_exists:
                if profile_path.exists() or backup_path.exists():
                    raise ProfileMigrationError(
                        f"Migration manifest expects no copy of {file.name}, but one exists."
                    )
                continue

            _verify_file_integrity(profile_path, file, "profile copy")
            _verify_file_integrity(backup_path, file, "backup copy")


@dataclass(frozen=True)
class ProfileMigrationResult:
    registry: ProfileRegistry
    migrated: bool
    manifest_path: Path | None


@dataclass(frozen=True)
class _LegacySnapshot:
    name: str
    source_path: Path
    content: bytes | None
    sha256: str | None
    record_count: int

    @property
    def exists(self) -> bool:
        return self.content is not None

    @property
    def byte_count(self) -> int:
        return len(self.content) if self.content is not None else 0


def migrate_legacy_single_search(settings: Settings) -> ProfileMigrationResult:
    """Create and verify the default profile from legacy single-search persistence.

    This function is intentionally not wired into the current runner or web service. Future entry
    points can call it before activating profile-scoped execution.
    """

    data_root = settings.data_root
    registry_store = ProfileRegistryStore(data_root)
    manifest_path = _migration_manifest_path(data_root)

    with _MIGRATION_LOCK:
        if manifest_path.exists():
            registry = registry_store.load()
            manifest = _load_manifest(manifest_path)
            _verify_activated_migration(registry, manifest, data_root)
            return ProfileMigrationResult(
                registry=registry,
                migrated=False,
                manifest_path=manifest_path,
            )

        snapshots = _legacy_snapshots(settings)
        has_legacy_search_data = any(
            snapshot.exists for snapshot in snapshots[: len(_LEGACY_FILE_NAMES)]
        )
        existing_registry = registry_store.load_if_exists()
        if existing_registry is not None and not has_legacy_search_data:
            return ProfileMigrationResult(
                registry=existing_registry,
                migrated=False,
                manifest_path=None,
            )

        migrated_profile = SearchProfile(
            id=DEFAULT_PROFILE_ID,
            name="Freezers",
            search_url=settings.marktplaats_search_url,
            use_case=settings.marktplaats_use_case,
            enabled=True,
            sort_order=0,
            bootstrap_existing_ads=(
                False if has_legacy_search_data else settings.bootstrap_existing_ads
            ),
        )
        registry = ProfileRegistry(
            default_profile_id=DEFAULT_PROFILE_ID,
            profiles=(migrated_profile,),
        )
        if existing_registry is not None:
            _verify_pending_registry(existing_registry, registry)
            registry = existing_registry

        _copy_legacy_backups(data_root, snapshots[:-1])
        _copy_profile_files(data_root, migrated_profile.id, snapshots[:-1])
        _verify_source_snapshots(snapshots)
        _verify_copied_snapshots(data_root, migrated_profile.id, snapshots[:-1])

        if existing_registry is None:
            registry_store.save_new(registry)
        manifest = _manifest_from_snapshots(migrated_profile.id, snapshots)
        _write_new_json(manifest_path, manifest.to_dict(), data_root)

        return ProfileMigrationResult(
            registry=registry,
            migrated=has_legacy_search_data,
            manifest_path=manifest_path if has_legacy_search_data else None,
        )


def ensure_profile_registry(settings: Settings) -> ProfileMigrationResult:
    """Compatibility-friendly alias for future CLI and web activation code."""

    return migrate_legacy_single_search(settings)


class ProfileRegistryStore:
    """Atomic persistence API for the profile registry."""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root

    @property
    def path(self) -> Path:
        return self._data_root / PROFILE_REGISTRY_FILENAME

    def load_if_exists(self) -> ProfileRegistry | None:
        if not self.path.exists():
            return None
        return self.load()

    def load(self) -> ProfileRegistry:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProfileConfigurationError(
                f"Could not load profile registry {self.path}: {error}"
            ) from error
        return ProfileRegistry.from_dict(value)

    def save_new(self, registry: ProfileRegistry) -> None:
        _write_new_json(self.path, registry.to_dict(), self._data_root)


def profile_storage_paths(data_root: Path, profile_id: str) -> ProfileStoragePaths:
    """Return validated profile-local paths without trusting a display name as a path."""

    return ProfileStoragePaths(data_root, profile_id)


def _legacy_snapshots(settings: Settings) -> tuple[_LegacySnapshot, ...]:
    legacy_paths = settings.legacy_search_file_paths()
    snapshots = [
        _read_legacy_snapshot(name, path)
        for name, path in legacy_paths.items()
    ]
    snapshots.append(_read_legacy_snapshot("model_usage.json", settings.global_model_usage_file))
    return tuple(snapshots)


def _read_legacy_snapshot(name: str, path: Path) -> _LegacySnapshot:
    if path.is_symlink():
        raise ProfileMigrationError(f"Legacy file {path} must not be a symbolic link.")
    if not path.exists():
        return _LegacySnapshot(
            name=name,
            source_path=path,
            content=None,
            sha256=None,
            record_count=0,
        )
    if not path.is_file():
        raise ProfileMigrationError(f"Legacy path {path} must be a regular file.")

    try:
        content = path.read_bytes()
    except OSError as error:
        raise ProfileMigrationError(f"Could not read legacy file {path}: {error}") from error

    record_count = _validate_legacy_content(name, content, path)
    return _LegacySnapshot(
        name=name,
        source_path=path,
        content=content,
        sha256=_sha256(content),
        record_count=record_count,
    )


def _validate_legacy_content(name: str, content: bytes, path: Path) -> int:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProfileMigrationError(f"Legacy file {path} is not UTF-8 text.") from error

    if name == "evaluations.jsonl":
        records = 0
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ProfileMigrationError(
                    f"Legacy evaluations file {path} has invalid JSON on line {line_number}."
                ) from error
            if not isinstance(value, dict):
                raise ProfileMigrationError(
                    f"Legacy evaluations file {path} has a non-object record on line {line_number}."
                )
            records += 1
        return records

    value = _load_json_object(content, path)
    if name == "seen_ads.json":
        seen_ads = value.get("seen_ads")
        if not isinstance(seen_ads, dict):
            raise ProfileMigrationError(f"Legacy state file {path} does not contain seen_ads.")
        return len(seen_ads)
    if name == "pipeline_progress.json":
        records = value.get("records")
        if not isinstance(records, dict):
            raise ProfileMigrationError(f"Legacy pipeline file {path} does not contain records.")
        return len(records)
    if name in {"runtime_status.json", "model_usage.json"}:
        return 1
    raise ProfileMigrationError(f"Unsupported legacy file {path}.")


def _load_json_object(content: bytes, path: Path) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProfileMigrationError(f"Legacy file {path} is not valid JSON.") from error
    if not isinstance(value, dict):
        raise ProfileMigrationError(f"Legacy file {path} must contain a JSON object.")
    return value


def _copy_legacy_backups(data_root: Path, snapshots: tuple[_LegacySnapshot, ...]) -> None:
    backup_directory = _migration_backup_directory(data_root)
    for snapshot in snapshots:
        _copy_snapshot_if_safe(snapshot, backup_directory / snapshot.name, data_root)


def _copy_profile_files(
    data_root: Path,
    profile_id: str,
    snapshots: tuple[_LegacySnapshot, ...],
) -> None:
    storage = ProfileStoragePaths(data_root, profile_id)
    for snapshot in snapshots:
        _copy_snapshot_if_safe(snapshot, storage.directory / snapshot.name, data_root)


def _copy_snapshot_if_safe(snapshot: _LegacySnapshot, destination: Path, data_root: Path) -> None:
    _assert_destination_within_data_root(destination, data_root)
    if not snapshot.exists:
        if destination.exists():
            raise ProfileMigrationError(
                f"Expected no migration copy at {destination}, but a file already exists."
            )
        return

    assert snapshot.content is not None
    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise ProfileMigrationError(
                f"Migration destination {destination} is not a regular file."
            )
        try:
            existing = destination.read_bytes()
        except OSError as error:
            raise ProfileMigrationError(
                f"Could not read migration destination {destination}: {error}"
            ) from error
        if existing != snapshot.content:
            raise ProfileMigrationError(
                f"Migration destination {destination} already differs from legacy data."
            )
        return

    _write_new_bytes(destination, snapshot.content, data_root)


def _verify_source_snapshots(snapshots: tuple[_LegacySnapshot, ...]) -> None:
    for snapshot in snapshots:
        if not snapshot.exists:
            if snapshot.source_path.exists():
                raise ProfileMigrationError(
                    f"Legacy file {snapshot.source_path} appeared while migration was running."
                )
            continue
        try:
            current = snapshot.source_path.read_bytes()
        except OSError as error:
            raise ProfileMigrationError(
                f"Could not re-verify legacy file {snapshot.source_path}: {error}"
            ) from error
        if current != snapshot.content:
            raise ProfileMigrationError(
                f"Legacy file {snapshot.source_path} changed while migration was running."
            )


def _verify_copied_snapshots(
    data_root: Path,
    profile_id: str,
    snapshots: tuple[_LegacySnapshot, ...],
) -> None:
    storage = ProfileStoragePaths(data_root, profile_id)
    backup_directory = _migration_backup_directory(data_root)
    for snapshot in snapshots:
        if not snapshot.exists:
            continue
        assert snapshot.sha256 is not None
        expected = MigrationFileIntegrity(
            name=snapshot.name,
            source_exists=True,
            source_sha256=snapshot.sha256,
            backup_sha256=snapshot.sha256,
            profile_sha256=snapshot.sha256,
            byte_count=snapshot.byte_count,
            record_count=snapshot.record_count,
        )
        _verify_file_integrity(backup_directory / snapshot.name, expected, "backup copy")
        _verify_file_integrity(storage.directory / snapshot.name, expected, "profile copy")


def _manifest_from_snapshots(
    profile_id: str,
    snapshots: tuple[_LegacySnapshot, ...],
) -> MigrationManifest:
    records = tuple(_integrity_from_snapshot(snapshot) for snapshot in snapshots[:-1])
    return MigrationManifest(
        profile_id=profile_id,
        created_at=datetime.now(UTC).isoformat(),
        files=records,
        global_model_usage=_integrity_from_snapshot(snapshots[-1]),
    )


def _integrity_from_snapshot(snapshot: _LegacySnapshot) -> MigrationFileIntegrity:
    return MigrationFileIntegrity(
        name=snapshot.name,
        source_exists=snapshot.exists,
        source_sha256=snapshot.sha256,
        backup_sha256=snapshot.sha256,
        profile_sha256=snapshot.sha256,
        byte_count=snapshot.byte_count,
        record_count=snapshot.record_count,
    )


def _verify_activated_migration(
    registry: ProfileRegistry,
    manifest: MigrationManifest,
    data_root: Path,
) -> None:
    try:
        profile = registry.profile(manifest.profile_id)
    except ProfileConfigurationError as error:
        raise ProfileMigrationError("Migration marker references a missing profile.") from error
    if (
        registry.default_profile_id != DEFAULT_PROFILE_ID
        or profile.sort_order != 0
        or not profile.enabled
    ):
        raise ProfileMigrationError(
            "Migrated freezer profile is no longer the enabled default profile."
        )
    manifest.verify_profile_copies(data_root)


def _verify_pending_registry(existing: ProfileRegistry, expected: ProfileRegistry) -> None:
    if existing != expected:
        raise ProfileMigrationError(
            "A profile registry already exists without a migration marker and cannot be safely "
            "reused."
        )


def _load_manifest(path: Path) -> MigrationManifest:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProfileMigrationError(f"Could not load migration marker {path}: {error}") from error
    return MigrationManifest.from_dict(value)


def _migration_manifest_path(data_root: Path) -> Path:
    return data_root / MIGRATION_DIRECTORY_NAME / f"{LEGACY_MIGRATION_NAME}.json"


def _migration_backup_directory(data_root: Path) -> Path:
    return data_root / MIGRATION_BACKUP_DIRECTORY_NAME / LEGACY_MIGRATION_NAME


def _verify_file_integrity(
    path: Path,
    expected: MigrationFileIntegrity,
    label: str,
) -> None:
    if path.is_symlink() or not path.is_file():
        raise ProfileMigrationError(f"Migration {label} {path} is missing or unsafe.")
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ProfileMigrationError(f"Could not read migration {label} {path}: {error}") from error
    if len(content) != expected.byte_count or _sha256(content) != expected.source_sha256:
        raise ProfileMigrationError(f"Migration {label} {path} does not match its manifest hash.")
    count = _validate_legacy_content(expected.name, content, path)
    if count != expected.record_count:
        raise ProfileMigrationError(f"Migration {label} {path} does not match its manifest count.")


def _write_new_json(path: Path, value: dict[str, Any], data_root: Path) -> None:
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _write_new_bytes(path, encoded, data_root)


def _write_new_bytes(path: Path, content: bytes, data_root: Path) -> None:
    _assert_destination_within_data_root(path, data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as output:
            output.write(content)
        if path.exists():
            raise ProfileMigrationError(f"Refusing to overwrite existing file {path}.")
        temporary.replace(path)
    except OSError as error:
        raise ProfileMigrationError(f"Could not write migration file {path}: {error}") from error
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _assert_destination_within_data_root(destination: Path, data_root: Path) -> None:
    root = data_root.resolve()
    try:
        destination.resolve().relative_to(root)
    except ValueError as error:
        raise ProfileMigrationError(
            f"Migration destination {destination} escapes persistent data root {data_root}."
        ) from error


def _required_string(value: dict[str, Any], key: str, owner: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ProfileConfigurationError(f"{owner} {key} must be a string.")
    return item


def _validate_profile_id(profile_id: str) -> None:
    if not isinstance(profile_id, str) or not _PROFILE_ID_PATTERN.fullmatch(profile_id):
        raise ProfileConfigurationError(
            "Profile ID must use lowercase letters, digits, and hyphens, start with a letter, "
            "and be at most 63 characters."
        )


def _validate_http_url(name: str, value: str) -> None:
    if not isinstance(value, str):
        raise ProfileConfigurationError(f"{name} must be a string.")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProfileConfigurationError(f"{name} must be an HTTP(S) URL.")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_sha256(value: str | None) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


_LEGACY_FILE_NAMES = (
    "seen_ads.json",
    "evaluations.jsonl",
    "runtime_status.json",
    "pipeline_progress.json",
)
