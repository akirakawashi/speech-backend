# syntax=docker/dockerfile:1

# One build target per deployable: the API, the pipeline worker, the migrations.
#
# The stages are the Speech Analytics stages of the `aicistent-backend`
# Dockerfile, carried over with their names, so `docker build --target ...` and
# the compose file keep working unchanged.
#
# Build one with:
#   docker build --target speech-ms -t aicistent-speech-ms:local .

FROM python:3.12-slim AS source

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache

WORKDIR /app

# Created in the shared stage so every target runs as the same non-root uid, and
# so a target cannot forget to create it.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /tmp \
       --shell /usr/sbin/nologin app

COPY pyproject.toml README.md ./
COPY src ./src


# --- speech-ms -------------------------------------------------------------
# HTTP surface for the call-recording search UI. It faces a browser (through
# its own nginx edge), so it carries no service key and no mTLS -- the user is
# proven by a Keycloak token.
FROM source AS speech-ms

RUN pip install --no-cache-dir ".[speech-ms]" \
    && python -m pip check \
    && python -c "from src.di.http import create_app; assert create_app"

# Injected by the release pipeline. Placed after the install so a new commit
# does not invalidate the dependency layer, and read at runtime by /version.
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
ENV AICISTENT_GIT_COMMIT=$GIT_COMMIT
ENV AICISTENT_BUILD_TIME=$BUILD_TIME

EXPOSE 8096
USER 10001:10001
CMD ["uvicorn", "src.di.http:create_app", "--factory", "--host", "0.0.0.0", "--port", "8096"]


# --- speech-worker ---------------------------------------------------------
# Same source, no web server: the pipeline workers, the daily ingest run and the
# operational jobs. The one image in this file that needs a binary rather than a
# Python package -- ffmpeg splits the two call legs and downmixes for Whisper.
#
# The import check names the worker module rather than an ASGI app: this image
# starts no web server, and building one at import time would only prove that a
# server it never runs can be constructed.
FROM source AS speech-worker

USER root
RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir ".[speech-worker]" \
    && python -m pip check \
    && python -c "from src.presentation.cli.worker import main; assert main" \
    && python -c "from src.presentation.cli.daily_ingest import main; assert main" \
    && ffmpeg -version > /dev/null

# Injected by the release pipeline. Placed after the install so a new commit
# does not invalidate the dependency layer, and read at runtime by /version.
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
ENV AICISTENT_GIT_COMMIT=$GIT_COMMIT
ENV AICISTENT_BUILD_TIME=$BUILD_TIME

USER 10001:10001
# Overridden per container: each worker lane and every operational job is the
# same image with a different module and different arguments.
CMD ["python", "-m", "src.presentation.cli.worker", "--lanes", "daily"]


# --- migrations ------------------------------------------------------------
# Runs the Alembic CLI and no service code, so it is the only image that carries
# Alembic and the migration scripts.
#
# In the monorepo this stage also carried the main application's tree, and its
# default command migrated that one. Here the only tree is Speech Analytics, so
# the default command names it.
FROM source AS migrations

COPY alembic.ini ./
COPY alembic ./alembic

RUN pip install --no-cache-dir ".[migrations]" \
    && python -m pip check \
    && alembic --version

# Injected by the release pipeline. Placed after the install so a new commit
# does not invalidate the dependency layer, and read at runtime by /version.
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
ENV AICISTENT_GIT_COMMIT=$GIT_COMMIT
ENV AICISTENT_BUILD_TIME=$BUILD_TIME

USER 10001:10001
CMD ["alembic", "-c", "alembic.ini", "upgrade", "head"]
