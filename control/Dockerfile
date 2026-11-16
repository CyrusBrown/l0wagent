# control: the trusted server (owner API, agent API, run manager, usage gate).
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/venv PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
# Same uid as the agent user, so control can read the agent's 0600 Claude credentials
# (mounted read-only) to read subscription usage.
RUN useradd -u 1000 -m control && mkdir -p /data && chown control:control /data
USER control
ENV PATH=/venv/bin:$PATH L0W_BACKEND=sqlite L0W_DATA_DIR=/data
VOLUME /data
EXPOSE 8000 8001
CMD ["uvicorn", "l0w_control.app:app", "--host", "0.0.0.0", "--port", "8000"]
