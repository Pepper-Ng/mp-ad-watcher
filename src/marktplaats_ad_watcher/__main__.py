from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

import uvicorn

from marktplaats_ad_watcher.config import Settings, load_dotenv
from marktplaats_ad_watcher.factory import build_watcher
from marktplaats_ad_watcher.status import RuntimeStatusStore
from marktplaats_ad_watcher.web import create_web_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch Marktplaats ads and evaluate new ones.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Run one scan and exit.")
    mode.add_argument(
        "--loop",
        action="store_true",
        help="Run forever using POLL_INTERVAL_SECONDS.",
    )
    mode.add_argument("--serve", action="store_true", help="Run the watcher with the web UI.")
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
    args = parser.parse_args()

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
    asyncio.run(_run(settings=settings, once=args.once))


async def _run(*, settings: Settings, once: bool) -> None:
    watcher = build_watcher(
        settings,
        status_store=RuntimeStatusStore(settings.status_file),
    )

    if once:
        summary = await watcher.run_once()
        print(summary.model_dump_json(indent=2))
    else:
        await watcher.run_loop()


if __name__ == "__main__":
    main()
