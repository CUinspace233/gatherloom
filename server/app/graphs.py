"""Shared machinery for job graphs run by the worker (ADR 0006).

A ``JobPipeline`` executes one claimed job through a LangGraph graph with a
PostgreSQL checkpointer: it resumes from the last checkpoint after a crash,
turns exceptions into job outcomes, records model calls in ``runs``, and
gives nodes fenced transactions (``step``) so that a worker that lost its
lease cannot write. Graph state holds identifiers only; nodes read content
from the domain tables and must be idempotent.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import StateGraph
from sqlalchemy.orm import Session, sessionmaker

from app import jobs
from app.models import Job, Run
from app.providers import Call, Models

log = logging.getLogger("gatherloom.jobs")
Hook = Callable[[str], None]


class JobPipeline(ABC):
    """Runs claimed jobs of one kind. One instance per worker thread."""

    #: Prompt version recorded in ``runs`` for each model task.
    PROMPT_VERSIONS: ClassVar[dict[str, int]] = {}

    def __init__(
        self,
        sessions: sessionmaker[Session],
        checkpointer: BaseCheckpointSaver[Any],
        worker_id: str,
        *,
        models: Models | None = None,
        clock: Callable[[], datetime] = jobs.utcnow,
        hook: Hook | None = None,
    ) -> None:
        self.sessions = sessions
        self.models = models
        self.checkpointer = checkpointer
        self.worker_id = worker_id
        self.clock = clock
        self.hook = hook or (lambda _point: None)
        self.claim: jobs.Claim | None = None
        self.last_run: UUID | None = None
        self.graph = self.build().compile(checkpointer=checkpointer)

    @abstractmethod
    def build(self) -> StateGraph[Any]:
        """The graph, ending with a node that settles the job."""

    @abstractmethod
    def initial_state(self, claim: jobs.Claim) -> dict[str, Any]:
        """Identifiers only: checkpoints must not contain user content."""

    def run(self, claim: jobs.Claim) -> str:
        """Execute or resume the job. Returns its resulting status."""
        self.claim = claim
        config: RunnableConfig = {"configurable": {"thread_id": claim.thread_id}}
        started = self.clock()
        try:
            pending = self.graph.get_state(config).next
            self.graph.invoke(
                None if pending else self.initial_state(claim),
                config,
                durability="sync",
            )
            status = "succeeded"
        except jobs.Cancelled:
            with self.sessions() as db:
                jobs.record_cancelled(db, claim, self.worker_id, self.clock())
            status = "cancelled"
        except jobs.LeaseLost:
            # The new owner (or the deletion) decides what happens next.
            status = "lost"
        except Exception as error:  # noqa: BLE001 - recorded as a job failure
            failure = (
                error
                if isinstance(error, jobs.JobError)
                else jobs.Retryable("internal_error", type(error).__name__)
            )
            try:
                with self.sessions() as db:
                    status = jobs.record_failure(
                        db, claim, self.worker_id, failure, self.clock()
                    )
            except jobs.LeaseLost:
                status = "lost"
            log.warning(
                "job attempt failed",
                extra={
                    "job_id": str(claim.id),
                    "error_code": failure.code,
                    "error_type": type(error).__name__,
                },
            )
        if status in ("succeeded", "cancelled", "failed"):
            self.checkpointer.delete_thread(claim.thread_id)
        log.info(
            "job attempt finished",
            extra={
                "job_id": str(claim.id),
                "status": status,
                "stage": claim.kind,
                "latency_ms": int((self.clock() - started).total_seconds() * 1000),
            },
        )
        return status

    @contextmanager
    def step(self, stage: str) -> Iterator[tuple[Session, Job]]:
        """A fenced transaction that records the current stage."""
        assert self.claim is not None
        with self.sessions() as db:
            job = jobs.fence(db, self.claim, self.worker_id)
            job.stage = stage
            job.updated_at = self.clock()
            yield db, job
            self.hook(f"{stage}:commit")
            db.commit()

    def finish(self, state: Any) -> dict[str, Any]:
        self.hook("finish")
        with self.step("done") as (_db, job):
            jobs.settle(job, "succeeded", self.clock())
        return {}

    def record(self, call: Call) -> None:
        """Store one model call's usage and cost, whatever the outcome."""
        assert self.claim is not None
        cost, price = call.cost()
        with self.sessions.begin() as db:
            run = Run(
                owner_id=self.claim.owner_id,
                job_id=self.claim.id,
                task=call.task,
                provider=call.provider,
                model=call.model,
                prompt_version=self.PROMPT_VERSIONS.get(call.task),
                input_tokens=call.input_tokens,
                output_tokens=call.output_tokens,
                cost_estimate=cost,
                currency=price.currency if price else None,
                price_date=price.checked if price else None,
                latency_ms=call.latency_ms,
                error_code=call.error_code,
            )
            db.add(run)
            db.flush()
            self.last_run = run.id
        log.info(
            "model call",
            extra={
                "job_id": str(self.claim.id),
                "stage": call.task,
                "latency_ms": call.latency_ms,
                "error_code": call.error_code,
                "model": call.model,
            },
        )
