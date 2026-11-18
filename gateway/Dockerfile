# The hub gateway image. Built from the repository root: docker build -f gateway/Dockerfile .
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/venv PYTHONUNBUFFERED=1
# The relative path source ../protocol/py must resolve, so the layout mirrors the repo.
COPY protocol/py /protocol/py
COPY gateway/ /gateway/
WORKDIR /gateway
RUN uv sync --frozen --no-dev
RUN useradd -u 1001 -m gateway && mkdir -p /data && chown gateway:gateway /data
USER gateway
ENV PATH=/venv/bin:$PATH GATEWAY_DATA_DIR=/data GATEWAY_PORT=8780
VOLUME /data
EXPOSE 8780
HEALTHCHECK --interval=10s --retries=10 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8780/healthz')"
CMD ["python", "-m", "l0w_gateway"]
