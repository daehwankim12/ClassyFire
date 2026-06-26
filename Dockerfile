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
# postgresql-client : pg_dump / pg_restore / psql — required by the in-container
#                     backup/restore smoke (docker compose run --rm web
#                     scripts/run_backup_restore_smoke.sh) and operator restores.
# libxrender1, libxext6 : RDKit rendering (optional but avoids runtime warnings)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        postgresql-client \
        libxrender1 \
        libxext6 \
    && rm -rf /var/lib/apt/lists/*

# ── Non-root user ────────────────────────────────────────────────────────────
RUN groupadd --gid 1001 appgroup \
 && useradd  --uid 1001 --gid 1001 --no-create-home --shell /bin/false appuser

# ── Working directory ────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ──────────────────────────────────────────────────────
# Copy only the dependency manifests first so Docker can cache this layer
# independently from application source changes.
COPY pyproject.toml uv.lock ./

# Install only runtime dependencies in the production image. Developer tools
# are installed by local workflows through `uv run --locked --extra dev`.
# --no-install-project: this layer runs BEFORE the application source is copied
# (for layer caching), so install ONLY third-party deps here. The app itself is
# imported from /app (WORKDIR) at runtime by gunicorn/manage.py, not from an
# installed wheel — building the root project now (no source present) would
# fail or produce an empty wheel.
RUN pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir uv==0.11.21 \
 && uv sync --locked --no-dev --no-editable --no-install-project

# ── Application source ───────────────────────────────────────────────────────
# Copy only the source files needed at runtime; secrets, venv, dumps, etc.
# are excluded via .dockerignore and selective COPY directives below.
COPY manage.py ./
COPY apps ./apps
COPY config ./config
COPY templates ./templates
COPY static ./static
COPY deploy ./deploy

ENV PATH="/app/.venv/bin:${PATH}"

# ── Static files ─────────────────────────────────────────────────────────────
# Collect static files at build time so the web container is ready immediately.
# DJANGO_SECRET_KEY is set to a dummy value; collectstatic does not need a real
# one.  The real key is injected at runtime via env_file / environment.
ARG DJANGO_SECRET_KEY=build-time-placeholder-not-for-use
ARG DJANGO_SETTINGS_MODULE=config.settings.prod
ENV DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY} \
    DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE}

# Collect static at build with placeholder env (collectstatic needs no DB).
# Failure aborts the image build so misconfigured static storage is caught
# immediately rather than silently producing a broken image.
RUN POSTGRES_HOST=localhost POSTGRES_PASSWORD=placeholder \
    NGROK_DOMAIN=placeholder.example.com \
    SHARED_ACCESS_PASSWORD_HASH=placeholder \
    python manage.py collectstatic --noinput

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
