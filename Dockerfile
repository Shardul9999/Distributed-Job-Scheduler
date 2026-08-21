# One image, several services.
#
# The API, workers, scheduler and reaper all run the same code and the same
# dependencies -- they differ only in the command they are started with. Building
# one image instead of four means a single build, a single layer cache, and no
# possibility of the worker running a different version of the shared schema
# than the API does.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # The repo root is the import root, so `packages.db` and `apps.api` resolve
    # without a packaging step. Avoids editable-install complexity in a monorepo.
    PYTHONPATH=/app

WORKDIR /app

# Build toolchain for any dependency without a manylinux wheel (argon2-cffi
# needs cffi). Installed and removed in one layer so it never reaches the
# final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies are copied and installed before application code. Docker caches
# this layer, so editing a Python file rebuilds in seconds rather than
# reinstalling the whole dependency tree.
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt

COPY . .

# Run as a non-root user. If the process is ever compromised, it should not own
# the filesystem it is running on.
RUN useradd --create-home --uid 1000 codity \
    && chown -R codity:codity /app
USER codity

EXPOSE 8000

# Overridden per service in docker-compose.yml.
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]


# ---------------------------------------------------------------------------
# Development target: adds test tooling and enables autoreload.
# ---------------------------------------------------------------------------
FROM base AS dev

USER root
RUN pip install -r requirements-dev.txt
USER codity

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
