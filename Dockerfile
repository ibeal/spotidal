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

RUN useradd --create-home --uid 1000 spotidal \
    && mkdir -p /data \
    && chown spotidal:spotidal /data

USER spotidal
WORKDIR /data

ENTRYPOINT ["spotidal"]
CMD ["--autorun"]
