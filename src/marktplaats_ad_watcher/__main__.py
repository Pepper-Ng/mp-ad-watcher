from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

import uvicorn

from marktplaats_ad_watcher.config import Settings, load_dotenv
from marktplaats_ad_watcher.factory import build_profile_orchestrator
from marktplaats_ad_watcher.profiles import ensure_profile_registry, verify_profile_registry
from marktplaats_ad_watcher.web import create_web_app


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.serve and (args.profile is not None or args.all_profiles):
        parser.error("--profile and --all-profiles are available for --once and --loop only.")
    if (args.migration_only or args.verify_migration) and (
        args.profile is not None or args.all_profiles
    ):
        parser.error("--profile and --all-profiles cannot be used with migration-only commands.")

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    env_file = Path(args.env_file)
    if args.serve:
        app = create_web_app(env_file=env_file, dry_run=args.dry_run)
        uvicorn.run(app, host=args.host, port=args.port)
        return

    load_dotenv(env_file, override=True)
    settings = Settings.from_environment(dry_run=args.dry_run)
    if args.migration_only:
        print(_migration_payload(ensure_profile_registry(settings)))
        return
    if args.verify_migration:
        print(_migration_payload(verify_profile_registry(settings)))
        return
    asyncio.run(
        _run(
            settings=settings,
            once=args.once,
            profile_id=args.profile,
            all_profiles=args.all_profiles,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Watch Marktplaats ads and evaluate new ones.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Run one scan and exit.")
    mode.add_argument(
        "--loop",
        action="store_true",
        help="Run forever using POLL_INTERVAL_SECONDS.",
    )
    mode.add_argument("--serve", action="store_true", help="Run the watcher with the web UI.")
    mode.add_argument(
        "--migration-only",
        "--migrate-only",
        action="store_true",
        dest="migration_only",
        help="Safely activate or resume profile migration, verify copies, and exit.",
    )
    mode.add_argument(
        "--verify-migration",
        action="store_true",
        help="Verify the existing profile registry and migration copies without changing data.",
    )
    profiles = parser.add_mutually_exclusive_group()
    profiles.add_argument("--profile", help="Run one selected profile ID; defaults to Freezers.")
    profiles.add_argument(
        "--all-profiles",
        action="store_true",
        help="Run all profiles sequentially; disabled profiles are skipped.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not call the model provider, write seen/evaluation state, or notify.",
    )
    parser.add_argument("--env-file", default=".env", help="Path to a .env file. Defaults to .env.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level. Defaults to INFO.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Web UI host for --serve.")
    parser.add_argument("--port", type=int, default=8080, help="Web UI port for --serve.")
    return parser


async def _run(
    *,
    settings: Settings,
    once: bool,
    profile_id: str | None = None,
    all_profiles: bool = False,
) -> None:
    orchestrator = build_profile_orchestrator(settings)
    if once:
        summary = (
            await orchestrator.run_all_enabled()
            if all_profiles
            else await orchestrator.run_profile(profile_id)
        )
        print(summary.model_dump_json(indent=2))
    else:
        await orchestrator.run_loop(profile_id=profile_id, all_profiles=all_profiles)


def _migration_payload(result: object) -> str:
    from marktplaats_ad_watcher.profiles import ProfileMigrationResult

    if not isinstance(result, ProfileMigrationResult):
        raise TypeError("Expected a profile migration result.")
    return json.dumps(
        {
            "migrated": result.migrated,
            "manifest_path": str(result.manifest_path) if result.manifest_path else None,
            "default_profile_id": result.registry.default_profile_id,
        },
        indent=2,
    )


if __name__ == "__main__":
    main()
