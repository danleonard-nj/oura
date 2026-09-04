# syntax=docker/dockerfile:1

# ── Build stage ───────────────────────────────────────────────────────────
# Dependencies are installed into a virtualenv in their own layer, so editing
# application code does not invalidate the dependency cache.
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml ./
COPY src/oura_mcp/__init__.py src/oura_mcp/__init__.py
RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install .

COPY src/ src/
RUN python -m pip install --no-deps .


# ── Runtime stage ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000

# A fixed uid/gid so a Kubernetes runAsUser can match without guessing.
RUN groupadd --system --gid 10001 oura && \
    useradd --system --uid 10001 --gid oura --no-create-home --shell /usr/sbin/nologin oura

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
USER 10001:10001

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
CMD ["sh", "-c", "exec hypercorn 'oura_mcp.app:create_app()' \
    --bind 0.0.0.0:${PORT} \
    --workers 1 \
    --error-logfile - \
    --graceful-timeout 25 \
    --keep-alive 120"]
