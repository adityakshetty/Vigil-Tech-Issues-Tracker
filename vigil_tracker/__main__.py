"""Command-line entry point.

    python -m vigil_tracker --config config.yaml          # run on the schedule
    python -m vigil_tracker --config config.yaml --once   # single run, then exit
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import Config
from .factory import build_tracker
from .scheduler import run_forever, run_once


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="vigil_tracker", description=__doc__)
    parser.add_argument("--config", required=True, help="Path to config YAML")
    parser.add_argument(
        "--once", action="store_true", help="Run a single cycle and exit"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = Config.load(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    tracker = build_tracker(config)

    if args.once:
        result = run_once(tracker.run)
        return 1 if result.get("error") else 0

    run_forever(config, tracker.run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
