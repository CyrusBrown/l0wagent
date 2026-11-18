# The tunnel service: dials out to the hub and forwards visitors' requests to control and the files app.
# Built from the repository root: docker build -f tunnel/Dockerfile .
FROM python:3.12-slim
# cloudflared, for the accelerator (protocol/accelerators.md): a pinned release, verified by sha256 (the digests GitHub lists for the assets).
# To update: gh release view --repo cloudflare/cloudflared --json tagName,assets, then change all three ARGs.
ARG TARGETARCH
ARG CLOUDFLARED_VERSION=2026.11.1
ARG CLOUDFLARED_SHA256_AMD64=97bb3b02d23a5980fc9c15a1a270e692da1923f103f920d5b16d4931d5ab55e7
ARG CLOUDFLARED_SHA256_ARM64=b155cec3e48e489cfb8e5c602769fb8c771083018d8881d68b4d18a791567c5a
ADD https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-${TARGETARCH} /tmp/cloudflared
RUN case "$TARGETARCH" in \
      amd64) want="$CLOUDFLARED_SHA256_AMD64" ;; \
      arm64) want="$CLOUDFLARED_SHA256_ARM64" ;; \
      *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && echo "$want  /tmp/cloudflared" | sha256sum -c - \
 && install -m 0755 /tmp/cloudflared /usr/local/bin/cloudflared && rm /tmp/cloudflared
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
