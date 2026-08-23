"""Worker process: claim jobs, execute them concurrently, report liveness.

One worker is one OS process running one asyncio event loop with three
concurrent tasks:

    claim_loop      poll queues -> claim a batch -> dispatch executors
    heartbeat_loop  prove liveness so the reaper does not revive our jobs
    shutdown        drain in-flight work on SIGTERM

Scaling out means running more of these processes (`docker compose up
--scale worker=3`), not more threads. Each is a separate interpreter with its
own GIL, so CPU-bound handlers in one worker cannot slow another -- and within
a worker, I/O-bound handlers interleave on the event loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import signal
import socket
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import insert, select, update

from apps.api.core.logging import configure_logging
from apps.worker.executor import execute_job
from apps.worker.handlers import registered_types
from packages.db import Queue, Worker, WorkerHeartbeat, dispose_engine, session_scope
from packages.db.claim import claim_jobs, release_jobs
from packages.db.enums import WorkerStatus

log = structlog.get_logger("worker")

VERSION = "0.1.0"


class JobWorker:
    def __init__(self) -> None:
        self.id: uuid.UUID | None = None
        self.concurrency = int(os.getenv("WORKER_CONCURRENCY", "10"))
        self.poll_interval_ms = int(os.getenv("WORKER_POLL_INTERVAL_MS", "100"))
        self.poll_max_backoff_ms = int(os.getenv("WORKER_POLL_MAX_BACKOFF_MS", "2000"))
        self.heartbeat_interval_s = int(os.getenv("WORKER_HEARTBEAT_INTERVAL_S", "10"))
        self.shutdown_grace_s = int(os.getenv("WORKER_SHUTDOWN_GRACE_S", "30"))

        # Comma-separated queue names; empty means "every queue".
        raw = os.getenv("WORKER_QUEUES", "").strip()
        self.queue_names = [q.strip() for q in raw.split(",") if q.strip()] or None

        # Caps how many jobs this worker runs at once. Acquired per job and
        # released when it finishes, so the worker never holds more work than
        # it can actually progress.
        self._slots = asyncio.Semaphore(self.concurrency)
        self._in_flight: dict[uuid.UUID, asyncio.Task] = {}
        self._stopping = asyncio.Event()
        self._processed = 0

        # Separate interpreters for CPU-bound handlers. Sized small: these are
        # the exception, and each process is a full Python interpreter.
        self._pool = concurrent.futures.ProcessPoolExecutor(
            max_workers=max(2, os.cpu_count() // 2 if os.cpu_count() else 2)
        )

    # -- lifecycle ----------------------------------------------------------

    async def register(self) -> None:
        """Insert this process's row in `workers`, making it visible to the
        dashboard and to the reaper."""
        async with session_scope() as db:
            result = await db.execute(
                insert(Worker)
                .values(
                    hostname=socket.gethostname(),
                    pid=os.getpid(),
                    version=VERSION,
                    concurrency=self.concurrency,
                    queue_names=self.queue_names,
                    status=WorkerStatus.ACTIVE,
                )
                .returning(Worker.id)
            )
            self.id = result.scalar_one()

        log.info(
            "worker.registered",
            worker_id=str(self.id),
            concurrency=self.concurrency,
            queues=self.queue_names or "all",
            handlers=registered_types(),
        )

    async def _target_queues(self) -> list[uuid.UUID]:
        """Resolve which queue ids to poll.

        Re-resolved on every claim cycle rather than cached at startup, so a
        queue created after the worker booted is picked up without a restart.
        """
        async with session_scope() as db:
            stmt = select(Queue.id).where(Queue.is_paused == False)  # noqa: E712
            if self.queue_names:
                stmt = stmt.where(Queue.name.in_(self.queue_names))
            stmt = stmt.order_by(Queue.priority.desc())
            return list((await db.execute(stmt)).scalars().all())

    # -- heartbeats ---------------------------------------------------------

    async def heartbeat_loop(self) -> None:
        """Prove liveness every N seconds.

        Two writes: the current-state column on `workers` that the reaper reads,
        and an append to the `worker_heartbeats` time series that the dashboard
        charts. Kept in one task so that a stalled event loop stops both --
        which is precisely the condition the reaper needs to detect.

        A third write undoes a *premature* reaping. If this process was paused
        long enough to miss the heartbeat threshold -- a suspended laptop, a
        briefly unreachable database -- the reaper will have declared us dead
        and handed our in-flight jobs to someone else. Beating again proves
        that verdict wrong, so we take `active` back rather than staying a
        ghost: still claiming work, but absent from the fleet list and from
        capacity. Only `dead` is reversed. A worker that is `draining` or
        `stopped` asked to leave, and must be allowed to.

        A genuinely wedged worker cannot exploit this, because the stall that
        makes it wedged is the same stall that stops this loop.
        """
        while not self._stopping.is_set():
            try:
                async with session_scope() as db:
                    await db.execute(
                        update(Worker)
                        .where(Worker.id == self.id)
                        .values(
                            last_heartbeat_at=datetime.now(UTC),
                            jobs_processed=self._processed,
                        )
                    )
                    revived = await db.execute(
                        update(Worker)
                        .where(Worker.id == self.id, Worker.status == WorkerStatus.DEAD)
                        .values(status=WorkerStatus.ACTIVE, stopped_at=None)
                    )
                    if revived.rowcount:
                        # Loud on purpose: the fleet lost work it did not need
                        # to lose. Any job the reaper reclaimed is now fenced
                        # (`lock_token = NULL`), so our writes for it are
                        # discarded and it runs exactly once elsewhere.
                        log.warning(
                            "worker.resurrected",
                            worker_id=str(self.id),
                            heartbeat_interval_s=self.heartbeat_interval_s,
                        )

                    await db.execute(
                        insert(WorkerHeartbeat).values(
                            worker_id=self.id,
                            active_jobs=len(self._in_flight),
                            jobs_processed=self._processed,
                        )
                    )
            except Exception:
                # A failed heartbeat must not kill the worker. If the database
                # is briefly unreachable we keep executing; if it stays
                # unreachable the reaper will correctly conclude we are gone.
                log.exception("worker.heartbeat_failed")

            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.heartbeat_interval_s
                )
            except TimeoutError:
                pass

    # -- claiming -----------------------------------------------------------

    async def _dispatch(self, job: dict) -> None:
        """Execute one job, then release its slot."""
        try:
            outcome = await execute_job(job, self.id, self._pool)
            self._processed += 1
            log.debug("job.finished", job_id=str(job["id"]), outcome=outcome)
        except Exception:
            log.exception("worker.executor_crashed", job_id=str(job["id"]))
        finally:
            self._in_flight.pop(job["id"], None)
            self._slots.release()

    async def claim_loop(self) -> None:
        """Poll for work until told to stop.

        Backs off exponentially when idle, from `poll_interval_ms` up to
        `poll_max_backoff_ms`, so an empty fleet does not hammer the database
        with pointless claim queries. Resets to the fast interval the moment
        work appears, so latency on a busy queue stays low.

        `LISTEN/NOTIFY` on job insert is the event-driven upgrade path, noted
        in DESIGN-DECISIONS.md -- polling with backoff is chosen here because
        it needs no additional connection per worker and degrades gracefully.
        """
        backoff_ms = self.poll_interval_ms

        while not self._stopping.is_set():
            # Never claim more than we have free slots for. Claiming work we
            # cannot start would hold it away from an idle worker that could.
            free = self.concurrency - len(self._in_flight)
            if free <= 0:
                await asyncio.sleep(self.poll_interval_ms / 1000)
                continue

            claimed_any = False
            try:
                for queue_id in await self._target_queues():
                    if self._stopping.is_set():
                        break

                    free = self.concurrency - len(self._in_flight)
                    if free <= 0:
                        break

                    async with session_scope() as db:
                        jobs = await claim_jobs(db, queue_id, self.id, free)

                    for job in jobs:
                        claimed_any = True
                        await self._slots.acquire()
                        task = asyncio.create_task(self._dispatch(job))
                        self._in_flight[job["id"]] = task

            except Exception:
                log.exception("worker.claim_failed")

            if claimed_any:
                backoff_ms = self.poll_interval_ms
            else:
                backoff_ms = min(backoff_ms * 2, self.poll_max_backoff_ms)

            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=backoff_ms / 1000)
            except TimeoutError:
                pass

    # -- shutdown -----------------------------------------------------------

    async def shutdown(self) -> None:
        """Drain gracefully: finish what we hold, hand back what we cannot.

        This is the assignment's "graceful shutdown" requirement, and it only
        works because the container entrypoint `exec`s this process so SIGTERM
        arrives here rather than at a wrapping shell.

        Sequence: stop claiming, mark ourselves DRAINING (so the dashboard
        shows it and the reaper is lenient), wait out the grace period for
        in-flight jobs, then release anything still unfinished back to the
        queue so another worker can pick it up immediately rather than waiting
        for the visibility timeout to expire.
        """
        log.info("worker.draining", in_flight=len(self._in_flight))
        self._stopping.set()

        try:
            async with session_scope() as db:
                await db.execute(
                    update(Worker)
                    .where(Worker.id == self.id)
                    .values(status=WorkerStatus.DRAINING)
                )
        except Exception:
            log.exception("worker.draining_status_failed")

        if self._in_flight:
            tasks = list(self._in_flight.values())
            done, pending = await asyncio.wait(
                tasks, timeout=self.shutdown_grace_s
            )
            log.info("worker.drained", finished=len(done), abandoned=len(pending))

            for task in pending:
                task.cancel()

            # Anything that did not finish is handed back. Its claim increment
            # is reversed by release_jobs, so a rolling restart does not silently
            # consume every job's retry budget.
            stranded = list(self._in_flight)
            if stranded:
                try:
                    async with session_scope() as db:
                        await release_jobs(db, stranded)
                    log.warning("worker.released_stranded_jobs", count=len(stranded))
                except Exception:
                    log.exception("worker.release_failed")

        try:
            async with session_scope() as db:
                await db.execute(
                    update(Worker)
                    .where(Worker.id == self.id)
                    .values(
                        status=WorkerStatus.STOPPED,
                        stopped_at=datetime.now(UTC),
                        jobs_processed=self._processed,
                    )
                )
        except Exception:
            log.exception("worker.stop_status_failed")

        self._pool.shutdown(wait=False, cancel_futures=True)
        await dispose_engine()
        log.info("worker.stopped", jobs_processed=self._processed)

    async def run(self) -> None:
        await self.register()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stopping.set)
            except NotImplementedError:
                # Windows outside WSL. The container is Linux, so this is only
                # a convenience for running the worker directly on a dev box.
                signal.signal(sig, lambda *_: self._stopping.set())

        tasks = [
            asyncio.create_task(self.claim_loop(), name="claim"),
            asyncio.create_task(self.heartbeat_loop(), name="heartbeat"),
        ]

        await self._stopping.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        await self.shutdown()


def main() -> None:
    configure_logging(
        level=os.getenv("LOG_LEVEL", "INFO"),
        json_output=os.getenv("ENVIRONMENT", "development").lower()
        in {"production", "prod"},
    )
    asyncio.run(JobWorker().run())


if __name__ == "__main__":
    main()
