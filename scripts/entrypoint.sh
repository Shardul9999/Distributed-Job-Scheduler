#!/usr/bin/env bash
# Container entrypoint: wait for PostgreSQL, apply migrations, then exec the
# requested service.
#
# `exec` on the final line matters. Without it, the service runs as a child of
# this script and SIGTERM is delivered to bash rather than to the worker -- which
# would silently break graceful shutdown, one of the assignment's explicit
# reliability requirements. With exec, the service replaces this shell and
# becomes PID 1, receiving signals directly.

set -euo pipefail

HOST="${POSTGRES_HOST:-postgres}"
PORT="${POSTGRES_PORT:-5432}"
RUN_MIGRATIONS="${RUN_MIGRATIONS:-false}"

echo "[entrypoint] waiting for postgres at ${HOST}:${PORT}..."

# Poll with a bounded number of attempts rather than looping forever, so a
# misconfigured host surfaces as a failed container instead of one that hangs
# in "starting" indefinitely.
for attempt in $(seq 1 60); do
    if python -c "
import socket, sys
s = socket.socket()
s.settimeout(1)
try:
    s.connect(('${HOST}', ${PORT}))
    s.close()
except OSError:
    sys.exit(1)
" 2>/dev/null; then
        echo "[entrypoint] postgres is accepting connections"
        break
    fi
    if [ "${attempt}" -eq 60 ]; then
        echo "[entrypoint] postgres did not become reachable in 60s" >&2
        exit 1
    fi
    sleep 1
done

# Only one service applies migrations. If every replica ran `alembic upgrade`
# on boot they would race for the alembic_version row; the API owns it and the
# workers simply wait for the schema to exist.
if [ "${RUN_MIGRATIONS}" = "true" ]; then
    echo "[entrypoint] applying migrations..."
    alembic upgrade head
    echo "[entrypoint] migrations applied"
fi

echo "[entrypoint] starting: $*"
exec "$@"
