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

FROM postgres:16-bookworm AS pg-client

FROM python:3.12-slim AS base

# ── System packages ──────────────────────────────────────────────────────────
# libpq5            : PostgreSQL client runtime library
# postgresql-client : supplies lib/client dependencies; exact pg_dump /
#                     pg_restore / psql binaries are copied from pg-client below
#                     so their major version matches the postgres:16 DB image.
# libxrender1, libxext6 : RDKit rendering (optional but avoids runtime warnings)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        postgresql-client \
        libxrender1 \
        libxext6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=pg-client /usr/lib/postgresql/16/bin/pg_dump /usr/local/bin/pg_dump
COPY --from=pg-client /usr/lib/postgresql/16/bin/pg_restore /usr/local/bin/pg_restore
COPY --from=pg-client /usr/lib/postgresql/16/bin/psql /usr/local/bin/psql
RUN pg_dump --version | grep -q ' 16\.' \
 && pg_restore --version | grep -q ' 16\.' \
 && psql --version | grep -q ' 16\.'

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
COPY scripts/run_backup_restore_smoke.sh ./scripts/

ENV PATH="/app/.venv/bin:${PATH}"

# ── Static files ─────────────────────────────────────────────────────────────
# Collect static files at build time so the web container is ready immediately.
# collectstatic does not need the deployment's signing key. Generate a temporary
# value for that process only; the real key must be injected at runtime.
ARG DJANGO_SETTINGS_MODULE=config.settings.prod
ENV DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE}

# Collect static at build with temporary env (collectstatic needs no DB).
# Failure aborts the image build so misconfigured static storage is caught
# immediately rather than silently producing a broken image.
RUN DJANGO_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(64))')" \
    POSTGRES_HOST=localhost POSTGRES_PASSWORD=placeholder \
    NGROK_DOMAIN=placeholder.example.com \
    SHARED_ACCESS_PASSWORD_HASH=placeholder \
    python manage.py collectstatic --noinput

# ── Runtime ownership ────────────────────────────────────────────────────────
# Create all writable runtime mount points in the image. Docker's named-volume
# copy-up then preserves sane ownership on fresh volumes; compose also runs a
# one-shot volume initializer for existing/root-owned volumes.
RUN mkdir -p \
        /app/staticfiles \
        /app/media \
        /app/export_files \
        /app/source_artifacts \
        /app/uploads \
 && chown -R appuser:appgroup /app

USER appuser

# ── Ports ────────────────────────────────────────────────────────────────────
# The port that Gunicorn listens on inside the container.
EXPOSE 8000

# ── Default entrypoint ───────────────────────────────────────────────────────
# Override `command:` in docker-compose.yml per service.
# `web`    → gunicorn config.wsgi:application --bind 0.0.0.0:8000
# `worker` → python manage.py runworker   (Task 9)
# --no-control-socket: appuser has no writable $HOME (--no-create-home), so
# gunicorn>=25.1's default control socket under ~/.gunicorn fails to bind.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120", "--no-control-socket"]
