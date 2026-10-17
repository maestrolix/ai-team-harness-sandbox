# syntax=docker/dockerfile:1
FROM node:24.21.0-bookworm-slim AS runtime
WORKDIR /opt/team
ARG CODEX_VERSION=0.163.0
LABEL org.opencontainers.image.title="Small AI team harness"
LABEL org.opencontainers.image.source="https://github.com/maestrolix/ai-team-harness-sandbox"
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends python3 git gh ca-certificates
RUN --mount=type=cache,target=/root/.npm npm install --global @openai/codex@${CODEX_VERSION}
RUN mkdir -p /state /workspace /home/node/.codex && chown -R node:node /state /workspace /home/node/.codex
COPY --chown=1000:1000 harness/ ./harness/
COPY --chown=1000:1000 tests/ ./tests/
USER node
ENTRYPOINT ["python3", "/opt/team/harness/observer.py"]
