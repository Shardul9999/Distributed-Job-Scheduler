"""Scheduler process: a leader-elected singleton running cron and the reaper.

Both jobs this process does are fleet-wide and must happen exactly once:

    cron    every template that comes due produces one job, not N
    reaper  every orphaned job is recovered once, not raced over by N sweepers

The obvious way to get that is to run one replica and hope. This process instead
elects a leader, so you can run three replicas for availability and still have
exactly one of them acting.

**How the election works.** `pg_try_advisory_lock(key)` takes a session-scoped
lock and returns immediately -- true if we got it, false if someone else holds
it. The lock lives on the PostgreSQL *session*, so it is released automatically
the instant the connection drops: a scheduler that is killed, OOMed, or
partitioned away from the database loses leadership without anyone having to
notice or clean up. A standby's next attempt succeeds within seconds. There is
no lease to renew, no TTL to tune, and no split-brain window, because the
database is the same single source of truth that already arbitrates every claim.

This is why the lock is held on a *dedicated* connection checked out for the
process's lifetime rather than borrowed from the pool per tick. A pooled
connection returned between ticks would release the lock with it.

The trade-off, documented in DESIGN-DECISIONS.md: leadership is only as
available as PostgreSQL, which is already true of the entire system -- the
queue is the database. Introducing etcd or Redis to elect a leader would add a
second thing that can fail in order to protect against the first one failing.
"""

from __future__ import annotations

import asyncio
import os
import signal

import structlog
from sqlalchemy import text

from apps.api.core.logging import configure_logging
from apps.scheduler import cron, reaper
from packages.db import dispose_engine, get_engine, session_scope
from packages.locks import SCHEDULER_LOCK_KEY as LOCK_KEY

log = structlog.get_logger("scheduler")


class Scheduler:
    def __init__(self) -> None:
        self.tick_ms = int(os.getenv("SCHEDULER_TICK_MS", "1000"))
        self.lock_retry_s = int(os.getenv("SCHEDULER_LOCK_RETRY_S", "5"))
        self.reaper_interval_s = int(os.getenv("REAPER_INTERVAL_S", "15"))
        self.heartbeat_timeout_s = int(os.getenv("WORKER_HEARTBEAT_TIMEOUT_S", "60"))
        self.heartbeat_retention_hours = int(
            os.getenv("HEARTBEAT_RETENTION_HOURS", "24")
        )

        self._stopping = asyncio.Event()
        self._lock_conn = None
        self._is_leader = False

    # -- leader election ----------------------------------------------------

    async def _try_acquire(self) -> bool:
        """Attempt to become leader. Idempotent and safe to call in a loop."""
        if self._lock_conn is None:
            self._lock_conn = await get_engine().connect()

        got = await self._lock_conn.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}
        )
        return bool(got)

    async def _still_leader(self) -> bool:
        """Confirm the lock connection is alive.

        Leadership and the connection are the same fact, so this is just a
        liveness ping. If it raises, the session is gone -- and so, already, is
        the lock -- and we must stop acting as leader immediately rather than
        continue materialising jobs a new leader is also materialising.
        """
        try:
            await self._lock_conn.scalar(text("SELECT 1"))
            return True
        except Exception:
            log.exception("scheduler.lock_connection_lost")
            return False

    async def _release(self) -> None:
        if self._lock_conn is None:
            return
        try:
            if self._is_leader:
                await self._lock_conn.scalar(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY}
                )
        except Exception:
            # Closing the connection releases the lock anyway. Explicit unlock
            # is a courtesy that makes failover instant instead of waiting for
            # the server to notice a closed socket.
            log.warning("scheduler.unlock_failed", exc_info=True)
        finally:
            try:
                await self._lock_conn.close()
            except Exception:
                pass
            self._lock_conn = None
            self._is_leader = False

    async def _await_leadership(self) -> bool:
        """Block in standby until we win the lock or are told to stop."""
        announced = False
        while not self._stopping.is_set():
            try:
                if await self._try_acquire():
                    self._is_leader = True
                    log.info("scheduler.leader_acquired", lock_key=LOCK_KEY)
                    return True
            except Exception:
                log.exception("scheduler.lock_attempt_failed")
                # Drop the connection so the next attempt builds a fresh one;
                # a half-dead connection would fail forever.
                if self._lock_conn is not None:
                    try:
                        await self._lock_conn.close()
                    except Exception:
                        pass
                    self._lock_conn = None

            if not announced:
                log.info(
                    "scheduler.standby",
                    reason="another replica holds the scheduler lock",
                    retry_s=self.lock_retry_s,
                )
                announced = True

            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.lock_retry_s
                )
            except TimeoutError:
                pass

        return False

    # -- the work -----------------------------------------------------------

    async def _tick(self) -> None:
        """One cron pass. Failures are logged, never fatal.

        A scheduler that exits on a transient database error stops firing every
        schedule in the system; one that logs and retries next tick loses at
        most a second.
        """
        try:
            async with session_scope() as db:
                await cron.materialize_due(db)
        except Exception:
            log.exception("scheduler.cron_tick_failed")

    async def _reap(self) -> None:
        try:
            async with session_scope() as db:
                await reaper.sweep(
                    db,
                    heartbeat_timeout_s=self.heartbeat_timeout_s,
                    heartbeat_retention_hours=self.heartbeat_retention_hours,
                )
        except Exception:
            log.exception("scheduler.reaper_sweep_failed")

    async def _lead(self) -> None:
        """Run cron and the reaper for as long as we hold the lock.

        One loop rather than two tasks: the reaper runs on a multiple of the
        cron tick. Two independent loops would need their own leadership checks
        and could disagree about whether we are still leader.
        """
        log.info(
            "scheduler.leading",
            tick_ms=self.tick_ms,
            reaper_interval_s=self.reaper_interval_s,
            heartbeat_timeout_s=self.heartbeat_timeout_s,
        )

        ticks_per_sweep = max(1, int(self.reaper_interval_s * 1000 / self.tick_ms))
        tick = 0

        # Sweep immediately on taking leadership. If we are here because the
        # previous leader died, its own in-flight recovery work is waiting.
        await self._reap()

        while not self._stopping.is_set():
            if not await self._still_leader():
                self._is_leader = False
                return

            await self._tick()

            tick += 1
            if tick % ticks_per_sweep == 0:
                await self._reap()

            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.tick_ms / 1000
                )
            except TimeoutError:
                pass

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stopping.set)
            except NotImplementedError:
                signal.signal(sig, lambda *_: self._stopping.set())

        log.info("scheduler.starting", lock_key=LOCK_KEY)

        # Outer loop: a leader that loses its connection returns to standby and
        # competes again rather than exiting. Restarting the container would
        # also work, but re-entering standby keeps the process available to
        # take over the moment the database is reachable again.
        while not self._stopping.is_set():
            if not await self._await_leadership():
                break
            await self._lead()
            if not self._stopping.is_set():
                log.warning("scheduler.leadership_lost", action="returning to standby")
                await self._release()

        await self._release()
        await dispose_engine()
        log.info("scheduler.stopped")


def main() -> None:
    configure_logging(
        level=os.getenv("LOG_LEVEL", "INFO"),
        json_output=os.getenv("ENVIRONMENT", "development").lower()
        in {"production", "prod"},
    )
    asyncio.run(Scheduler().run())


if __name__ == "__main__":
    main()
