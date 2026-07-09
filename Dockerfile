FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Install dependencies first so they're cached separately from app code
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY spotidal ./spotidal
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

# supercronic runs the periodic --autorun schedule inside the container;
# it's built for containers (logs jobs to stdout, forwards signals cleanly on `docker stop`).
ARG SUPERCRONIC_VERSION=v0.2.47
ARG SUPERCRONIC_SHA1SUM=712d2ece75da6f6e530192a151488578153e4e96
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && curl -fsSLo /usr/local/bin/supercronic \
        "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
    && echo "${SUPERCRONIC_SHA1SUM}  /usr/local/bin/supercronic" | sha1sum -c - \
    && chmod +x /usr/local/bin/supercronic \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /app/entrypoint.sh
COPY report-sync.sh /app/report-sync.sh
RUN chmod +x /app/entrypoint.sh /app/report-sync.sh

RUN useradd --create-home --uid 1000 spotidal \
    && mkdir -p /data \
    && chown spotidal:spotidal /data

USER spotidal
WORKDIR /data

# Standard 5-field cron syntax; defaults to every 6 hours
ENV CRON_SCHEDULE="0 */6 * * *"

ENTRYPOINT ["/app/entrypoint.sh"]
