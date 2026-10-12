# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.11.18 AS uv

FROM python:3.12-slim AS dependencies
COPY --from=uv /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home \
       --home-dir /tmp --shell /usr/sbin/nologin app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

FROM dependencies AS source
COPY README.md alembic.ini openapi.speech.json ./
COPY src ./src
COPY alembic ./alembic

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# API сохраняет прежнюю точку входа и порт.
FROM source AS speech-ms
RUN uv pip check \
    && python -c "from src.di.http import create_app; assert create_app"

ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
ENV AICISTENT_GIT_COMMIT=$GIT_COMMIT \
    AICISTENT_BUILD_TIME=$BUILD_TIME

EXPOSE 8096
USER 10001:10001
CMD ["uvicorn", "src.di.http:create_app", "--factory", "--host", "0.0.0.0", "--port", "8096"]

# Совместимая сборка сохранённых команд. Основной воркер собирается отдельно
# в speech-worker; удаление прежнего кода в эту задачу не входит.
FROM source AS speech-worker
RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && uv pip check \
    && python -c "from src.presentation.cli.worker import main; assert main" \
    && python -c "from src.presentation.cli.daily_ingest import main; assert main" \
    && ffmpeg -version > /dev/null

ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
ENV AICISTENT_GIT_COMMIT=$GIT_COMMIT \
    AICISTENT_BUILD_TIME=$BUILD_TIME

USER 10001:10001
CMD ["python", "-m", "src.presentation.cli.worker", "--lanes", "daily"]

FROM source AS migrations
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --group migrations --no-editable
RUN uv pip check && alembic --version

ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
ENV AICISTENT_GIT_COMMIT=$GIT_COMMIT \
    AICISTENT_BUILD_TIME=$BUILD_TIME

USER 10001:10001
CMD ["alembic", "-c", "alembic.ini", "upgrade", "head"]
