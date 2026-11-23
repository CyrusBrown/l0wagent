# The agent's computer: Debian with sudo, dev tools, Claude Code and opencode. Its only network is
# the internal sandbox network; the egress proxy is its only way out.
FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.source=https://github.com/CyrusBrown/l0wagent

# Pin with --build-arg CLAUDE_CODE_VERSION=x.y.z (the adapter tracks the CLI's headless interface).
ARG CLAUDE_CODE_VERSION=stable
# opencode (for opencode Go) is pinned: its `run --format json` events are what the adapter reads.
ARG OPENCODE_VERSION=1.18.31
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl wget git jq ripgrep fd-find tini sudo procps psmisc less nano vim-tiny \
      unzip zip xz-utils bzip2 file tree sqlite3 build-essential pkg-config \
      nodejs npm openssh-client rsync dnsutils htop tmux bash-completion util-linux cron \
 && rm -rf /var/lib/apt/lists/*

# Claude Code (native build), installed system-wide; updates come from rebuilding the image.
RUN curl -fsSL https://claude.ai/install.sh | bash -s -- ${CLAUDE_CODE_VERSION} \
 && cp -L /root/.local/bin/claude /usr/local/bin/claude \
 && rm -rf /root/.local/share/claude /root/.local/bin/claude \
 && /usr/local/bin/claude --version

# opencode, installed system-wide from npm (the package ships a native binary per platform).
RUN npm install -g --prefix /usr/local "opencode-ai@${OPENCODE_VERSION}" \
 && /usr/local/bin/opencode --version

RUN pip install --no-cache-dir uv \
 && python -m venv /opt/runner/venv \
 && /opt/runner/venv/bin/pip install --no-cache-dir "fastapi==0.115.*" "uvicorn==0.32.*" "anyio>=4"

RUN useradd -m -u 1000 -s /bin/bash agent \
 && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent \
 && echo 'Defaults env_keep += "HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy"' > /etc/sudoers.d/proxy \
 && chmod 0440 /etc/sudoers.d/* \
 && mkdir -p /workspace /work/_inbox /cache /home/agent/.local/bin /home/agent/.claude \
 && ln -s /work/_inbox/_ext /external \
 && chown -R agent:agent /workspace /work /cache /home/agent

# The reviewer's account (S24): no sudo, its own home. The agent's home is 0700 (the entrypoint
# re-applies it on the volume), so the reviewer reads /workspace and /work only as "other".
RUN useradd -m -u 1001 -s /bin/bash reviewer \
 && chmod 0755 /home/reviewer \
 && chmod 0700 /home/agent \
 && git config --system --add safe.directory /workspace \
 && git config --system --add safe.directory '/work/*'

COPY runner/ /opt/runner/
COPY bin/ /usr/local/bin/
COPY seed/ /opt/seed/
# Extensions' managed folders: control writes /work/_inbox/_ext/<name>/<folder>/, the agent reads them as
# /external/<name>/<folder>/ (read-only like the rest of /work).
COPY entrypoint.sh /usr/local/sbin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/assistant /usr/local/bin/assistant-hook /usr/local/sbin/entrypoint.sh /opt/runner/fake_claude.py /opt/runner/fake_opencode.py

ENV PATH=/home/agent/.local/bin:$PATH \
    NPM_CONFIG_PREFIX=/home/agent/.local \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    WORKSPACE=/workspace \
    RUNNER_STATE_DIR=/home/agent/.runner

WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/sbin/entrypoint.sh"]
