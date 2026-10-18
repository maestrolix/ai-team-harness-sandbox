# syntax=docker/dockerfile:1
FROM node:24.21.0-bookworm-slim AS base
WORKDIR /opt/team
ARG CODEX_VERSION=0.163.0
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends python3 python3-venv git gh ca-certificates
RUN --mount=type=cache,target=/root/.npm npm install --global @openai/codex@${CODEX_VERSION}

FROM base AS builder
RUN python3 -m venv /opt/build-env
COPY harness/pyproject.toml harness/README.md /build/harness/
COPY harness/src/ /build/harness/src/
RUN --mount=type=cache,target=/root/.cache/pip \
    /opt/build-env/bin/pip wheel --no-deps --wheel-dir /wheels /build/harness

FROM base AS runtime
LABEL org.opencontainers.image.title="AI Team Harness"
LABEL org.opencontainers.image.source="https://github.com/maestrolix/ai-team-harness-sandbox"
RUN python3 -m venv /opt/harness-env
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    /opt/harness-env/bin/pip install --no-index /wheels/*.whl
ENV PATH="/opt/harness-env/bin:${PATH}"
RUN mkdir -p /state /workspace /home/node/.codex && chown -R node:node /state /workspace /home/node/.codex
USER node
ENTRYPOINT ["harness", "run"]
