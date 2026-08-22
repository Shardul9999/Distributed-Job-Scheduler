"""PostgreSQL advisory-lock keys, shared by everything that takes or observes them.

An advisory lock is just a bigint the database agrees to arbitrate. That makes
the key itself a contract: the scheduler takes it, the API reads `pg_locks` to
report whether anyone holds it, and if the two ever disagreed about the number
the dashboard would confidently report a healthy scheduler that does not exist.
Defining every key once, here, is what keeps that from being possible.
"""

from __future__ import annotations

import zlib

#: Leader election for the scheduler process (cron + reaper).
#:
#: CRC32 of a namespace string rather than Python's `hash()`, which is
#: randomised per process by PEP 456 -- two replicas would compute different
#: keys, both acquire "the" lock, and both believe they lead. Determinism is
#: the entire requirement; cryptographic strength is irrelevant.
#:
#: The `:v1` suffix is deliberate: if the scheduler's responsibilities ever
#: split, bumping it lets a new fleet elect independently of an old one still
#: running during a rollout.
SCHEDULER_LOCK_KEY = zlib.crc32(b"codity:scheduler:v1")
