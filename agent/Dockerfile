# The agent's computer: Debian with sudo, dev tools and Claude Code. Its only network is
# the internal sandbox network; the egress proxy is its only way out.
FROM python:3.12-slim-bookworm

# Pin with --build-arg CLAUDE_CODE_VERSION=x.y.z (the adapter tracks the CLI's headless interface).
ARG CLAUDE_CODE_VERSION=stable
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

RUN pip install --no-cache-dir uv \
 && python -m venv /opt/runner/venv \
 && /opt/runner/venv/bin/pip install --no-cache-dir "fastapi==0.115.*" "uvicorn==0.32.*" "anyio>=4"

RUN useradd -m -u 1000 -s /bin/bash agent \
 && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent \
 && echo 'Defaults env_keep += "HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy"' > /etc/sudoers.d/proxy \
 && chmod 0440 /etc/sudoers.d/* \
 && mkdir -p /workspace /work/_inbox /cache /home/agent/.local/bin /home/agent/.claude \
 && chown -R agent:agent /workspace /work /cache /home/agent

COPY runner/ /opt/runner/
COPY bin/ /usr/local/bin/
COPY seed/ /opt/seed/
COPY entrypoint.sh /usr/local/sbin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/assistant /usr/local/bin/assistant-hook /usr/local/sbin/entrypoint.sh /opt/runner/fake_claude.py

ENV PATH=/home/agent/.local/bin:$PATH \
    NPM_CONFIG_PREFIX=/home/agent/.local \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    WORKSPACE=/workspace \
    RUNNER_STATE_DIR=/home/agent/.runner

WORKDIR /workspace
# Port 8787 reaches the owner's LAN only through the agent-web forwarder.
EXPOSE 8787
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/sbin/entrypoint.sh"]
