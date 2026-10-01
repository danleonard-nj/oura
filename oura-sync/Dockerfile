# syntax=docker/dockerfile:1
#
# Build context is this directory (see deploy.bat). The application is the
# plain source tree under app/, imported as `app.*` -- there is no package to
# build or install, only requirements.
#
# config.json is copied into the image and read by framework's Configuration
# from the working directory. It is never committed: CI/CD (or deploy.bat,
# from your local copy) puts it in the build context. The build fails without
# one rather than producing an image that cannot start.
#
# `framework` comes from the private py-packages feed. Its token is passed as
# a BuildKit secret, not a build arg, so it never lands in an image layer or
# the build history:
#
#   docker build --secret id=artifact_feed_token,env=ARTIFACT_FEED_TOKEN .

# ── Build stage ───────────────────────────────────────────────────────────
# Dependencies are installed into a virtualenv in their own layer, so editing
# application code does not invalidate the dependency cache.
FROM python:3.12-slim AS build

ARG ARTIFACT_FEED=py-packages

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN --mount=type=secret,id=artifact_feed_token,required=true \
    python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install -r requirements.txt \
        --extra-index-url "https://${ARTIFACT_FEED}:$(cat /run/secrets/artifact_feed_token)@pkgs.dev.azure.com/dcl525/_packaging/${ARTIFACT_FEED}/pypi/simple/"


# ── Runtime stage ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/srv \
    PORT=8000

# A fixed uid/gid so a Kubernetes runAsUser can match without guessing.
RUN groupadd --system --gid 10002 oura && \
    useradd --system --uid 10002 --gid oura --no-create-home --shell /usr/sbin/nologin oura

COPY --from=build /opt/venv /opt/venv
COPY app/ /srv/app/
COPY config.json /srv/config.json

WORKDIR /srv
USER 10002:10002

EXPOSE 8000

# Hypercorn is PID 1 via exec form, so it receives SIGTERM directly from the
# kubelet and can drain connections instead of being killed after the grace
# period. --graceful-timeout bounds that drain; keep it below the pod's
# terminationGracePeriodSeconds.
#
# No --access-logfile: CorrelationIdMiddleware logs every request itself, as
# structured JSON tagged with the same correlation id as its audit rows,
# which hypercorn's own plain-text access log can't do. --error-logfile stays
# -- it is hypercorn's own startup/worker-crash log, not covered by that
# middleware.
CMD ["sh", "-c", "exec hypercorn 'app.app:create_app()' \
    --bind 0.0.0.0:${PORT} \
    --workers 1 \
    --error-logfile - \
    --graceful-timeout 25 \
    --keep-alive 120"]
