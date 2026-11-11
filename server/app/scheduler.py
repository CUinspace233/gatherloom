"""Scheduler: ``python -m app.scheduler``.

Every few minutes, queues the weekly reviews that are due (app.reviews.scan).
Running two schedulers is safe: duplicate jobs publish at most one review.
"""

import logging
import signal
import threading

from app.db import SessionLocal
from app.jobs import utcnow
from app.reviews import scan
from app.worker import JsonFormatter

log = logging.getLogger("gatherloom.scheduler")
INTERVAL_SECONDS = 300


def tick() -> int:
    with SessionLocal.begin() as db:
        return scan(db, utcnow())


def main() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    stopping = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopping.set())
    log.info("scheduler started")
    while not stopping.is_set():
        try:
            queued = tick()
            if queued:
                log.info("weekly reviews queued", extra={"status": f"queued={queued}"})
        except Exception:
            log.exception("scan failed")
        stopping.wait(INTERVAL_SECONDS)
    log.info("scheduler stopped")


if __name__ == "__main__":
    main()
