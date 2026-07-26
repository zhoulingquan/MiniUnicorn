FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Install runtime dependencies (no Node.js — the WhatsApp bridge was removed).
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates git bubblewrap openssh-client && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cached layer). Hatch reads the custom build
# hook from hatch_build.py even for this metadata-only install.
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md hatch_build.py ./
COPY uv.lock ./
RUN mkdir -p miniunicorn && touch miniunicorn/__init__.py && \
    uv pip install --system --no-cache . && \
    rm -rf miniunicorn

# Copy the full source and install
COPY miniunicorn/ miniunicorn/
COPY webui/ webui/
RUN uv pip install --system --no-cache .

# Create non-root user and config directory
RUN useradd -m -u 1000 -s /bin/bash miniunicorn && \
    mkdir -p /home/miniunicorn/.miniunicorn && \
    chown -R miniunicorn:miniunicorn /home/miniunicorn /app

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

USER miniunicorn
ENV HOME=/home/miniunicorn

# WebUI/WebSocket channel port
EXPOSE 8765

# 健康检查：WebUI 静态资源在根路径返回 index.html，与 docker-compose 的 gateway healthcheck 端点一致
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8765/ || exit 1

ENTRYPOINT ["entrypoint.sh"]
CMD ["status"]
