# =============================================================================
# ClassyFire Batch Web — Runtime image
#
# This single image is used by both the `web` service (gunicorn) and the
# `worker` service (manage.py runworker, added in Task 9).  The CMD is
# intentionally left unset so docker-compose.yml can supply the command for
# each service.
#
# Build:
#   docker build -t classyfire .
# =============================================================================

FROM python:3.12-slim AS base

# ── System packages ──────────────────────────────────────────────────────────
# libpq5            : PostgreSQL client runtime library
# libxrender1, libxext6 : RDKit rendering (optional but avoids runtime warnings)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libxrender1 \
        libxext6 \
    && rm -rf /var/lib/apt/lists/*

# ── Non-root user ────────────────────────────────────────────────────────────
RUN groupadd --gid 1001 appgroup \
 && useradd  --uid 1001 --gid 1001 --no-create-home --shell /bin/false appuser

# ── Working directory ────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ──────────────────────────────────────────────────────
# ── Application source ───────────────────────────────────────────────────────
COPY . .

# Install only runtime dependencies in the production image. Developer tools
# are installed by local workflows through `uv run --locked --extra dev`.
RUN pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir .

# ── Static files ─────────────────────────────────────────────────────────────
# Collect static files at build time so the web container is ready immediately.
# DJANGO_SECRET_KEY is set to a dummy value; collectstatic does not need a real
# one.  The real key is injected at runtime via env_file / environment.
ARG DJANGO_SECRET_KEY=build-time-placeholder-not-for-use
ARG DJANGO_SETTINGS_MODULE=config.settings.prod
ENV DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY} \
    DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE}

# Collect static at build with placeholder env (collectstatic needs no DB).
# A failure must remain VISIBLE in the build log rather than be silently
# swallowed, but it should not abort the image build — missing static assets
# surface and are recoverable at runtime via `collectstatic` on start.
RUN POSTGRES_HOST=localhost POSTGRES_PASSWORD=placeholder \
    NGROK_DOMAIN=placeholder.example.com \
    SHARED_ACCESS_PASSWORD_HASH=placeholder \
    python manage.py collectstatic --noinput 2>&1 \
    || echo "WARN: collectstatic failed at build time — run it at container start; check STORAGES/whitenoise config"

# ── Runtime ownership ────────────────────────────────────────────────────────
RUN chown -R appuser:appgroup /app

USER appuser

# ── Ports ────────────────────────────────────────────────────────────────────
# The port that Gunicorn listens on inside the container.
EXPOSE 8000

# ── Default entrypoint ───────────────────────────────────────────────────────
# Override `command:` in docker-compose.yml per service.
# `web`    → gunicorn config.wsgi:application --bind 0.0.0.0:8000
# `worker` → python manage.py runworker   (Task 9)
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120"]
