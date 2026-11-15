# control: the trusted server. In M0 it serves the mock backend.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/venv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
ENV PATH=/venv/bin:$PATH L0W_BACKEND=mock
EXPOSE 8000
CMD ["uvicorn", "l0w_control.app:app", "--host", "0.0.0.0", "--port", "8000"]
