# openclaw-drone: ONE Pi-ready image for any drone node (scout / rover / painter).
#
# Plug a Raspberry Pi (or any Docker host) into the swarm: this image adapts to
# whatever drone it is via env (AGENT_ID/ROLE/TASK), joins the hub over HTTP,
# and streams its thinking to the dashboard. The brain is the proven Python
# §7-loop wrapper by default; it is also PicoClaw-ready (see WITH_PICOCLAW).
#
# Multi-arch: python:3.12-slim publishes linux/arm64 (Pi 4/5) + linux/amd64, so
#   docker buildx build --platform linux/arm64,linux/amd64 -f drone/Dockerfile .
# produces images that run on a Pi and on a dev box alike. Stdlib-only brain ->
# no pip install, no package index needed at build time.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/agent
WORKDIR /app

COPY agent/ /app/agent/
COPY souls/ /app/souls/
COPY drone/ /app/drone/
COPY test_fixtures/ /app/test_fixtures/

# Optional: bake the PINNED PicoClaw binary in for the native brain (brief §11:
# pin a verified Sipeed commit/tag, never pull "latest"). Off by default so the
# image builds with no network. Provide the binary at build time, e.g.:
#   docker buildx build --build-arg WITH_PICOCLAW=1 \
#     --build-arg PICOCLAW_VERSION=v0.2.9 -f drone/Dockerfile .
# and drop the matching arch binary into drone/bin/picoclaw-<arch> beforehand.
ARG WITH_PICOCLAW=0
ARG PICOCLAW_VERSION=v0.2.9
ARG TARGETARCH
RUN if [ "$WITH_PICOCLAW" = "1" ] && [ -f "/app/drone/bin/picoclaw-${TARGETARCH}" ]; then \
        install -m 0755 "/app/drone/bin/picoclaw-${TARGETARCH}" /usr/local/bin/picoclaw && \
        echo "baked PicoClaw ${PICOCLAW_VERSION} (${TARGETARCH})"; \
    else echo "PicoClaw not baked (WITH_PICOCLAW=$WITH_PICOCLAW); wrapper brain only"; fi

ENV FIXTURES=/app/test_fixtures BRAIN=wrapper
ENTRYPOINT ["sh", "/app/drone/entrypoint.sh"]
