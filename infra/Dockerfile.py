FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" DAGSTER_HOME=/dagster_home FAIRLINE_ROOT=/app

WORKDIR /app
COPY py/pyproject.toml py/uv.lock py/
RUN cd py && uv sync --frozen --no-dev --no-install-project
COPY py/ py/
RUN cd py && uv sync --frozen --no-dev
COPY config/ config/
COPY db/ db/
COPY infra/dagster/ infra/dagster/
RUN mkdir -p /dagster_home && cp infra/dagster/dagster.yaml /dagster_home/
