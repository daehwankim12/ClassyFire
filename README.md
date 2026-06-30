# ClassyFire Batch Cache Web

A self-hosted Django web application that accepts batches of chemical
identifiers (SMILES, InChI, InChIKey), classifies them using a local cache,
and optionally forwards unresolved compounds to the
[Wishart Lab ClassyFire API](https://classyfire.wishartlab.com/).

Classifications are cached locally so repeated queries are served instantly.
The application works fully offline for any compound already in the local cache
and degrades gracefully when the remote API is unreachable.

---

## Quick Start — Fresh Linux PC

### 1. Prerequisites

- Docker Engine 24+ with the Compose plugin (`docker compose version`)
- A domain or subdomain pointed at this machine (optional — only required for
  the ngrok dev-domain feature)

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

```dotenv
POSTGRES_PASSWORD=<strong_random_password>
DJANGO_SECRET_KEY=<50+-character_random_string>
```

### 3. Start the stack

```bash
docker compose up -d
```

This starts seven services:

| Service | Role |
|---|---|
| `db` | PostgreSQL 16 (internal only, no host port) |
| `migrate` | One-shot Django migration runner |
| `web` | Gunicorn (internal only, accessed via proxy) |
| `worker` | Classification worker (`python manage.py runworker`) |
| `backup` | Daily backup cron |
| `proxy` | Caddy reverse proxy (internal-only; reached by ngrok over Docker network) |
| `ngrok-agent` | Optional ngrok tunnel (requires NGROK_AUTHTOKEN) |

`web` and `worker` wait for the one-shot `migrate` service before starting.

### 4. Collect static files

```bash
docker compose exec web python manage.py collectstatic --noinput
```

### 5. Create an admin user (optional)

```bash
docker compose exec web python manage.py createsuperuser
```

### 6. Verify

Open `http://localhost/` (or your domain) in a browser.  The submit page
should load.

---

## Classification Cache (Seedless, On-Demand)

There is no pre-loaded seed dataset. The classification cache (`classyfire_live`)
starts empty and fills on demand:

1. A lookup for an InChIKey checks `classyfire_live`.
2. On a miss, the remote worker queries the Wishart ClassyFire service — an
   entity `GET` first, then a structure-submit `POST` for compounds Wishart has
   not seen — and writes the result back into `classyfire_live`.
3. Subsequent lookups for the same key are served instantly from the cache.

No download/import/activate steps are required. Run the worker
(`make` targets / `manage.py runworker`) and the cache populates as jobs are
processed.

---

## ngrok Dev Domain Setup

The ngrok agent exposes your local instance at a stable public HTTPS URL.

### 1. Obtain an ngrok account and auth token

Sign up at [ngrok.com](https://ngrok.com) and copy your auth token from the
dashboard.

### 2. Reserve a dev domain (ngrok paid plan)

In the ngrok dashboard, reserve a static domain (e.g.
`your-name.ngrok-free.app`).

### 3. Configure `.env`

```dotenv
NGROK_AUTHTOKEN=<your_ngrok_auth_token>
NGROK_DOMAIN=<your_domain>.ngrok-free.app
```

### 4. Start the stack

```bash
docker compose up -d
```

The `ngrok-agent` container reads `NGROK_AUTHTOKEN` and `NGROK_DOMAIN` and
connects automatically.  Your application is now reachable at
`https://<NGROK_DOMAIN>/`.

---

## Backup and Restore

### Run a backup

```bash
docker compose exec web python manage.py run_backup
```

Creates a compressed archive plus a JSON manifest (tables, row counts,
checksums) in the configured backup directory.  Applies retention policy
(7 daily, 4 weekly, 12 monthly).

### Restore smoke test

```bash
docker compose exec web python manage.py restore_smoke_test
```

Verifies the most recent backup is readable and checksums match.

### Full restore (data loss operation)

```bash
# Stop the worker
docker compose stop worker

# Restore
docker compose exec web python manage.py restore_from_backup \
  --backup-file backups/<archive_name>.tar.gz

# Restart
docker compose up -d
```

See [docs/operations.md](docs/operations.md) for the full runbook.

---

## Remote-Outage Behaviour

When the Wishart ClassyFire API is unreachable:

- **Local cache lookups continue normally.** Compounds already in the live cache
  are served instantly.
- **The circuit breaker opens** after repeated consecutive failures, preventing
  further outbound calls until the API recovers.
- **Pending remote tasks are deferred** — they are not lost.  When the circuit
  closes (or an admin clicks **Retry deferred** in `/admin/`), deferred tasks
  are retried automatically.

Monitor circuit state on the `/stats/` page.

---

## Update Process

```bash
# Pull latest code
git pull origin main

# Pull updated images, then rebuild local app services
docker compose pull
docker compose build migrate web worker

# Rerun migrations with the rebuilt image, then recreate app containers
docker compose up --force-recreate --abort-on-container-exit migrate
docker compose up -d --build web worker
```

---

## Failure Response

| Symptom | First step |
|---|---|
| Worker not processing tasks | `docker compose logs worker`; check circuit state on `/stats/` |
| Database connection errors | `docker compose ps db`; check `POSTGRES_*` vars in `.env` |
| Remote API unreachable | Circuit breaker will self-heal; use **Retry deferred** if needed |
| Disk full / upload rejected | `df -h /`; prune old backups and run `docker compose run --rm cleanup`; check disk guard threshold |
| Missing classifications | Check NEED_STRUCTURE items; provide structures via the UI or API |

See [docs/operations.md](docs/operations.md) for detailed procedures.

---

## Development

### Install dependencies and run commands

```bash
UV_CACHE_DIR=/tmp/uv-cache-classyfire uv run --locked --extra dev python -m pytest --version
```

The Makefile uses the same `uv run --locked --extra dev` workflow for tests,
linting, formatting, and type checks.

### Run tests

```bash
make test
```

### Lint / format / type-check

```bash
make lint
make format
make typecheck
```

---

## Documentation

- [docs/user-guide.md](docs/user-guide.md) — How to submit compounds, read results, handle NEED_STRUCTURE, export
- [docs/operations.md](docs/operations.md) — Worker management, backup/restore, admin actions
- [docs/api/openapi.yaml](docs/api/openapi.yaml) — Full OpenAPI 3 spec
- [docs/acceptance-test-results.md](docs/acceptance-test-results.md) — Acceptance evidence for all T-xx / NFR-Pxx IDs
- [docs/state-machines.md](docs/state-machines.md) — BatchItem and RemoteTask state diagrams

---

## License

MIT

## Attribution

When using ClassyFire classifications, please cite:

> Djoumbou Feunang Y, Eisner R, Knox C, Chepelev L, Hastings J, Owen G,
> Fahy E, Steinbeck C, Wishart DS.
> ClassyFire: automated chemical classification with a comprehensive,
> computable taxonomy.
> *J Cheminformatics.* 2016;8:61.
> DOI: [10.1186/s13321-016-0174-y](https://doi.org/10.1186/s13321-016-0174-y)

See [NOTICE.md](NOTICE.md) for full attribution details.
