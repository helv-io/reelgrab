"""Entrypoint: python -m reelgrab"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from reelgrab import __app_name__, __version__
from reelgrab.config import bootstrap, load_config


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog=__app_name__,
        description="Short-form video grabber for Matrix (appservice + yt-dlp)",
    )
    p.add_argument(
        "-c",
        "--config",
        help="Path to config.yaml (default: $REELGRAB_DATA/config.yaml or /data/config.yaml)",
    )
    p.add_argument(
        "-d",
        "--data-dir",
        help="Data directory (default: $REELGRAB_DATA or /data or ./data)",
    )
    p.add_argument(
        "--generate-registration",
        action="store_true",
        help="Regenerate registration.yaml from config and exit",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"{__app_name__} {__version__}",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    if args.generate_registration:
        result = bootstrap(
            config_file=args.config,
            data_dir=args.data_dir,
            generate_registration=True,
            allow_example_domain=True,
        )
        for msg in result.messages:
            print(msg, file=sys.stderr)
        if result.created_config:
            print(
                "Config was missing and has been written. "
                "Edit it, then re-run --generate-registration.",
                file=sys.stderr,
            )
            sys.exit(0)
        print(f"registration: {result.config.registration_path}", file=sys.stderr)
        sys.exit(0)

    config = load_config(path=args.config, data_dir=args.data_dir)

    logging.basicConfig(
        level=getattr(logging, config.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("reelgrab")
    log.info("%s %s starting", __app_name__, __version__)
    log.info(
        "homeserver=%s domain=%s user=%s data=%s",
        config.homeserver.address,
        config.homeserver.domain,
        config.user_id,
        config.data_dir,
    )
    log.info(
        "registration=%s backend=%s",
        config.registration_path,
        config.download.backend,
    )

    from reelgrab.handlers import run_bot

    try:
        asyncio.run(run_bot(config))
    except KeyboardInterrupt:
        log.info("shutting down")
        sys.exit(0)
    except Exception:
        log.exception("fatal error")
        sys.exit(1)


if __name__ == "__main__":
    main()
