# The chat runtime: Claude Code for subscription chat turns, in a jail. No volumes, no secrets at
# rest; its only network is `chat`, so it reaches the provider through egress and nothing else.
FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.source=https://github.com/CyrusBrown/l0wagent

# Keep this in step with agent/Dockerfile (the stream-json interface is what control reads).
ARG CLAUDE_CODE_VERSION=stable
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 PYTHONUNBUFFERED=1 DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl tini \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://claude.ai/install.sh | bash -s -- ${CLAUDE_CODE_VERSION} \
 && cp -L /root/.local/bin/claude /usr/local/bin/claude \
 && rm -rf /root/.local/share/claude /root/.local/bin/claude \
 && /usr/local/bin/claude --version

RUN pip install --no-cache-dir "fastapi==0.115.*" "uvicorn==0.32.*" "anyio>=4"

RUN useradd -m -u 1000 -s /usr/sbin/nologin chat
COPY runtime.py /opt/chat-runtime/runtime.py
USER chat
WORKDIR /tmp
ENV TMPDIR=/tmp
EXPOSE 8770
ENTRYPOINT ["/usr/bin/tini", "--", "python", "/opt/chat-runtime/runtime.py"]
