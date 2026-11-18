# The tunnel service: dials out to the hub and forwards visitors' requests to control and the files app.
# Built from the repository root: docker build -f tunnel/Dockerfile .
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/venv PYTHONUNBUFFERED=1
# ../protocol/py must resolve from /tunnel (the l0wproto path source)
COPY protocol/py /protocol/py
WORKDIR /tunnel
COPY tunnel/pyproject.toml tunnel/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY tunnel/ ./
RUN uv sync --frozen --no-dev
RUN useradd -u 1002 -m tunnel
USER tunnel
ENV PATH=/venv/bin:$PATH
CMD ["python", "-m", "l0w_tunnel"]
