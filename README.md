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
| `proxy` | Caddy reverse proxy (publishes ports 80 and 443) |
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

## Seed Dataset (Local Classification Cache)

A pre-loaded seed dataset significantly reduces the number of remote calls.
Three profiles are available:

| Profile | Description | Size (approx.) |
|---|---|---|
| `FULL` | Complete InChIKey-deduplicated ClassyFire collection | ~4 GB |
| `LITE` | Most-common compounds only | ~400 MB |
| `CUSTOM` | User-supplied subset | varies |

### Download

```bash
make seed-download
```

Downloads the selected seed profile from Zenodo (record 20472700).

### Verify integrity

```bash
make seed-verify
```

Checks SHA-256 checksums.

### Import (resumable)

```bash
make seed-import
```

Streams rows into the seed staging table.  Import can be safely interrupted and
resumed — the checkpoint mechanism prevents duplicate rows.

### Audit

```bash
make seed-audit
```

Validates InChIKey format and ChemOnt hierarchy consistency.  A seed that fails
audit cannot be activated.

### Activate

```bash
make seed-activate
```

Promotes the audited seed to the active classification cache.  This operation
is idempotent and does not overwrite live (Wishart) results.

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

- **Local cache lookups continue normally.** Compounds already in the local or
  seed cache are served instantly.
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

# Pull updated images
docker compose pull

# Recreate app containers; web/worker wait for the one-shot migrate service
docker compose up -d web worker
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
- [docs/operations.md](docs/operations.md) — Worker management, backup/restore, seed swap, admin actions
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

The pre-loaded seed dataset is derived from:

> InChIKey-Deduplicated ClassyFire/ChemOnt Label Collection v3  
> Zenodo record 20472700  
> License: CC BY 4.0

See [NOTICE.md](NOTICE.md) for full attribution details.
