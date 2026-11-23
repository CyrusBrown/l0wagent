# The ops image: `ops` (extensions and update requests), `ops-docker` (the Docker socket, three fixed commands) and the
# one-shot updater (`l0w-updater TAG`) are all this image with different commands.
# Built from the repository root: docker build -f ops/Dockerfile .

# The Docker CLI and the Compose plugin, from Docker's official image (pinned by digest). Only the updater runs them; neither
# `ops` nor `ops-docker` does (they speak HTTP to each other and, for ops-docker, to the socket with Python).
# To update: docker buildx imagetools inspect docker:<version>-cli, then change the tag and the digest.
FROM docker:27.5.1-cli@sha256:851f91d241214e7c6db86513b270d58776379aacc5eb9c4a87e5b47115e3065c AS dockercli

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/venv PYTHONUNBUFFERED=1
# The relative path source ../protocol/py must resolve, so the layout mirrors the repo.
COPY protocol/py /protocol/py
COPY ops/ /ops/
WORKDIR /ops
RUN uv sync --frozen --no-dev
# `ops` runs as this user by default. compose.yaml runs it as the host user instead (L0W_UID/L0W_GID) so that what it writes
# into extensions/ belongs to whoever owns that folder. ops-docker runs as root (the socket is root's): see l0w_ops/docker.py.
RUN useradd -u 1001 -m ops
USER ops
ENV PATH=/venv/bin:$PATH HOME=/tmp OPS_PORT=8795
EXPOSE 8795
HEALTHCHECK --interval=10s --retries=10 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8795/healthz')"
CMD ["python", "-m", "l0w_ops"]
