# The release image for control: the API plus the built web app (served at /).
# Built from the repository root: docker build -f deploy/control.Dockerfile .
FROM node:24-slim AS web
WORKDIR /web
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/venv PYTHONUNBUFFERED=1
COPY control/pyproject.toml control/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY control/ ./
RUN uv sync --frozen --no-dev
COPY --from=web /web/dist /app/web
# Same uid as the agent user, so control can read the agent's 0600 Claude credentials
# (mounted read-only) to read subscription usage.
RUN useradd -u 1000 -m control && mkdir -p /data && chown control:control /data
USER control
ARG L0W_VERSION=edge
ENV PATH=/venv/bin:$PATH L0W_BACKEND=sqlite L0W_DATA_DIR=/data L0W_WEB_DIR=/app/web L0W_VERSION=$L0W_VERSION
LABEL org.opencontainers.image.source=https://github.com/CyrusBrown/l0wagent org.opencontainers.image.version=$L0W_VERSION
VOLUME /data
EXPOSE 8000 8001
HEALTHCHECK --interval=10s --retries=10 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/meta')"
CMD ["uvicorn", "l0w_control.app:app", "--host", "0.0.0.0", "--port", "8000"]
