# syntax=docker/dockerfile:1.6
# MiniUnicorn Dockerfile — multi-stage build (设计 §4.6)
#
# Stage 1 (webui-builder): uses Bun + frozen bun.lock to build the Vite frontend
#   into miniunicorn/web/dist. Node/Bun live ONLY in this stage.
# Stage 2 (runtime): Python-only image. Copies the prebuilt webui dist from
#   stage 1 and runs `uv pip install` with MINIUNICORN_SKIP_WEBUI_BUILD=1 so
#   hatch_build.py never tries to invoke bun/npm. The runtime image contains
#   no Node/Bun toolchain.

# ----------------------------------------------------------------------------
# Stage 1: webui builder
# ----------------------------------------------------------------------------
FROM oven/bun:1.2-debian AS webui-builder

WORKDIR /build

# Copy the whole webui source tree. .dockerignore excludes node_modules/ and
# any prebuilt dist/, so the layer stays small and the build is reproducible.
# We also create an empty miniunicorn/web/ directory so vite.config.ts's
# outDir (``../miniunicorn/web/dist``) has a parent to write into.
COPY webui/ ./webui/
RUN mkdir -p miniunicorn/web

# Install with frozen lockfile (reproducible) and build. vite.config.ts writes
# its outDir to ``../miniunicorn/web/dist`` (relative to webui/), which lands
# at /build/miniunicorn/web/dist — exactly the layout the runtime stage needs.
RUN cd webui && \
    bun install --frozen-lockfile && \
    bun run build

# ----------------------------------------------------------------------------
# Stage 2: Python runtime
# ----------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Install runtime dependencies (no Node.js / Bun — the runtime image must
# stay slim and the webui is already prebuilt in stage 1).
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates git bubblewrap openssh-client && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cached layer). Hatch reads the custom
# build hook from hatch_build.py even for this metadata-only install.
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md hatch_build.py ./
COPY uv.lock ./
RUN mkdir -p miniunicorn && touch miniunicorn/__init__.py && \
    uv pip install --system --no-cache . && \
    rm -rf miniunicorn

# Copy the prebuilt webui dist produced by stage 1. This MUST happen before the
# full source install below so hatch_build.py sees miniunicorn/web/dist/index.html
# and treats the webui as already built.
COPY --from=webui-builder /build/miniunicorn/web/dist ./miniunicorn/web/dist

# Copy the full Python source and install. MINIUNICORN_SKIP_WEBUI_BUILD=1
# guarantees hatch_build.py will not try to invoke bun/npm (which are absent
# from this stage) — the prebuilt dist copied above is used as-is.
# The [documents] extra supplies pypdf, python-docx, openpyxl, and
# python-pptx so the container can extract PDF/DOCX/XLSX/PPTX attachments.
ENV MINIUNICORN_SKIP_WEBUI_BUILD=1
COPY miniunicorn/ miniunicorn/
RUN uv pip install --system --no-cache ".[documents]"

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

# 设计 §4.6: healthcheck 改用公开的 /health 端点。旧版用 / 静态资源回退,
# 但 / 在 API 启用 key 时仍可访问;改用 /health 是更明确的 liveness 信号,
# 也与 docker-compose.yml 中各服务的 healthcheck 对齐。
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8765/health || exit 1

ENTRYPOINT ["entrypoint.sh"]
CMD ["status"]
