# syntax=docker/dockerfile:1
#
# Build context is the repo root (see deploy.bat), not this directory:
# oura_sync imports from the sibling oura-common/ shared source tree, which is
# not an installed package (see oura-common/src/oura_common/__init__.py) --
# it's put on the venv's import path here via a .pth file, the same mechanism
# `pip install -e` uses under the hood.

# ── Build stage ───────────────────────────────────────────────────────────
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY oura-sync/pyproject.toml ./
COPY oura-sync/src/oura_sync/__init__.py src/oura_sync/__init__.py
RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install .

COPY oura-sync/src/ src/
RUN python -m pip install --no-deps . && \
    SITE_PACKAGES="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" && \
    echo "/opt/oura-common" > "$SITE_PACKAGES/oura-common.pth"


# ── Runtime stage ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000

# A fixed uid/gid so a Kubernetes runAsUser can match without guessing.
RUN groupadd --system --gid 10002 oura && \
    useradd --system --uid 10002 --gid oura --no-create-home --shell /usr/sbin/nologin oura

COPY --from=build /opt/venv /opt/venv
COPY oura-common/src/oura_common /opt/oura-common/oura_common

WORKDIR /app
USER 10002:10002

EXPOSE 8000

# Hypercorn is PID 1 via exec form, so it receives SIGTERM directly from the
# kubelet and can drain connections instead of being killed after the grace
# period.
CMD ["sh", "-c", "exec hypercorn 'oura_sync.app:create_app()' \
    --bind 0.0.0.0:${PORT} \
    --workers 1 \
    --error-logfile - \
    --graceful-timeout 25 \
    --keep-alive 120"]
