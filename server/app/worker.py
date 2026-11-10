"""Background worker: ``python -m app.worker``.

Runs GATHERLOOM_WORKER_CONCURRENCY threads (default 2). Each thread claims one
job at a time, keeps its lease alive with heartbeats and runs it through the
ingestion graph. SIGTERM or SIGINT stops claiming; running jobs finish their
current attempt before the process exits.
"""

import json
import logging
import os
import signal
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Self
from uuid import uuid4

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row
from sqlalchemy import make_url
from sqlalchemy.orm import Session, sessionmaker

from app import jobs, tokenizer
from app.db import DATABASE_URL, SessionLocal
from app.ingestion import Pipeline
from app.providers import Models, build_models
from app.settings import load_settings

log = logging.getLogger("gatherloom.worker")
IDLE_SECONDS = 2.0
LOG_FIELDS = (
    "job_id",
    "status",
    "stage",
    "latency_ms",
    "error_code",
    "error_type",
    "model",
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Never includes entry text or keys."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(
            {key: getattr(record, key) for key in LOG_FIELDS if hasattr(record, key)}
        )
        return json.dumps(payload, ensure_ascii=False)


def checkpoint_url(url: str = DATABASE_URL) -> str:
    return (
        make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
    )


@contextmanager
def checkpointer(url: str = DATABASE_URL) -> Iterator[PostgresSaver]:
    """A PostgresSaver on its own autocommit connection, as LangGraph requires."""
    with Connection.connect(
        checkpoint_url(url), autocommit=True, prepare_threshold=0, row_factory=dict_row
    ) as connection:
        yield PostgresSaver(connection)


def setup_checkpoints(url: str = DATABASE_URL) -> None:
    with checkpointer(url) as saver:
        saver.setup()


class Heartbeat:
    """Extends a job's lease every third of the lease period."""

    def __init__(
        self, sessions: sessionmaker[Session], claim: jobs.Claim, worker_id: str
    ) -> None:
        self.stopped = threading.Event()
        self.thread = threading.Thread(
            target=self.beat, args=(sessions, claim, worker_id), daemon=True
        )

    def beat(
        self, sessions: sessionmaker[Session], claim: jobs.Claim, worker_id: str
    ) -> None:
        interval = jobs.LEASE.total_seconds() / 3
        while not self.stopped.wait(interval):
            try:
                with sessions() as db:
                    if not jobs.heartbeat(db, claim, worker_id, jobs.utcnow()):
                        return
            except Exception:  # noqa: BLE001 - the lease check in fence decides
                log.warning("heartbeat failed", extra={"job_id": str(claim.id)})

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stopped.set()
        self.thread.join()


def work(
    stopping: threading.Event,
    worker_id: str,
    models: Models,
    sessions: sessionmaker[Session] = SessionLocal,
    url: str = DATABASE_URL,
) -> None:
    with checkpointer(url) as saver:
        pipeline = Pipeline(sessions, saver, worker_id, models=models)
        while not stopping.is_set():
            try:
                with sessions() as db:
                    claim = jobs.claim(db, worker_id, jobs.utcnow())
            except Exception:
                log.exception("claim failed")
                claim = None
            if claim is None:
                stopping.wait(IDLE_SECONDS)
                continue
            with Heartbeat(sessions, claim, worker_id):
                pipeline.run(claim)


def main() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    concurrency = int(os.environ.get("GATHERLOOM_WORKER_CONCURRENCY", "2"))
    settings = load_settings()
    if problems := settings.problems():
        raise SystemExit("worker configuration: " + "; ".join(problems))
    tokenizer.warm_up()
    setup_checkpoints()
    stopping = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopping.set())
    prefix = f"{socket.gethostname()[:32]}:{os.getpid()}"
    threads = [
        threading.Thread(
            target=work,
            args=(
                stopping,
                f"{prefix}:{index}:{uuid4().hex[:8]}",
                build_models(settings),
            ),
        )
        for index in range(concurrency)
    ]
    for thread in threads:
        thread.start()
    log.info("worker started", extra={"status": f"concurrency={concurrency}"})
    for thread in threads:
        thread.join()
    log.info("worker stopped")


if __name__ == "__main__":
    main()
