"""Run-on-a-schedule loop.

Supports either a fixed interval (minutes) or a cron expression. Each run
processes only new or previously deferred activity; a failure in one run is
logged and does not stop the loop (the next run safely retries).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable

from .config import Config

logger = logging.getLogger(__name__)


def run_once(run_fn: Callable[[], dict]) -> dict:
    try:
        return run_fn()
    except Exception:
        logger.exception("Run failed; will retry on next scheduled run")
        return {"error": 1}


def run_forever(config: Config, run_fn: Callable[[], dict]) -> None:
    if config.schedule.cron:
        _run_cron(config.schedule.cron, run_fn)
    else:
        _run_interval(config.schedule.interval_minutes or 15, run_fn)


def _run_interval(interval_minutes: int, run_fn: Callable[[], dict]) -> None:
    interval = max(1, interval_minutes) * 60
    logger.info("Scheduler: every %s minute(s)", interval_minutes)
    while True:
        start = time.monotonic()
        run_once(run_fn)
        elapsed = time.monotonic() - start
        time.sleep(max(0.0, interval - elapsed))


def _run_cron(expression: str, run_fn: Callable[[], dict]) -> None:
    from croniter import croniter

    logger.info("Scheduler: cron '%s'", expression)
    while True:
        now = datetime.now(timezone.utc)
        nxt = croniter(expression, now).get_next(datetime)
        sleep_for = max(0.0, (nxt - now).total_seconds())
        logger.info("Next run at %s (%.0fs)", nxt.isoformat(), sleep_for)
        time.sleep(sleep_for)
        run_once(run_fn)
