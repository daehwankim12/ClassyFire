# Operations Guide

This guide covers day-to-day operational tasks for the ClassyFire Batch Cache
Web application.

---

## Real PostgreSQL Test Harness

The trusted test path runs through a real PostgreSQL 16 database.  The harness
maps `POSTGRES_TEST_*` variables into Django's `POSTGRES_*` settings, verifies
the server version, then runs pytest.

For a disposable local database managed by Docker Compose:

```bash
make postgres-test-up
make test-postgres
make postgres-test-down
```

The Compose service exposes PostgreSQL only on localhost at
`${POSTGRES_TEST_PORT:-55432}`.

If Docker daemon access is unavailable, point the harness at an already-running
PostgreSQL 16 server:

```bash
POSTGRES_TEST_HOST=127.0.0.1 \
POSTGRES_TEST_PORT=55432 \
POSTGRES_TEST_DB=cf_test_wt4 \
POSTGRES_TEST_USER=classyfire \
POSTGRES_TEST_PASSWORD=classyfire \
make test-postgres
```

On this machine, the current OS user cannot access `/var/run/docker.sock`.
Local verification therefore used the already-running PostgreSQL 16.14 instance
at `127.0.0.1:55432`.

---

## Ingest Worker

The ingest worker is a default (always-on, no profile required) Compose service
that drains staged upload jobs by running `python manage.py process_jobs` in a
restart loop.  It starts automatically with `docker compose up -d` and must be
running for uploaded batches to be processed.  The service depends on `db`
(healthy) and `migrate` (completed) before starting.

```bash
docker compose up -d           # ingest-worker starts alongside web
docker compose logs -f ingest-worker
docker compose restart ingest-worker
```

---

## Worker Management

The classification worker is a Django management command that polls the
`RemoteTask` queue, submits structures to the Wishart ClassyFire service, and
fans out results to waiting `BatchItem` rows.

### Starting the worker

```bash
docker compose exec web python manage.py run_worker
```

In the Docker Compose stack the worker runs as the `worker` service:

```bash
docker compose up -d worker
```

### Stopping the worker

```bash
docker compose stop worker
```

The worker is designed to be interrupted safely.  Any task it was processing
is returned to `READY` state by the stale-lock recovery mechanism (T-08) when
the next worker claims it.

### Stale-lock recovery

If a worker process crashes mid-claim the locked task will remain `RUNNING`
past the stale-lock threshold (default 10 minutes).  The next worker start
automatically runs the recovery pass and resets stale tasks to `READY`.

---

## Queue Monitoring

### Stats page

The stats page at `/stats/` provides a live overview of:

- Unique InChIKey count in the live cache
- Remote task queue depth (pending, running, deferred)
- Circuit breaker state (CLOSED / OPEN / HALF_OPEN)
- Last successful remote call timestamp
- Database size
- Backup status

### Admin queue actions

The `/admin/` actions panel exposes:

| Action | Description |
|---|---|
| **Pause remote** | Stops the worker from making new remote calls (sets `REMOTE_PAUSED` flag). |
| **Resume remote** | Clears the `REMOTE_PAUSED` flag. |
| **Retry deferred** | Resets `DEFERRED` tasks to `READY` for immediate retry. |
| **Retry retry-wait** | Resets `RETRY_WAIT` tasks to `READY`. |

### Remote-pause flag

The `REMOTE_PAUSED` admin flag prevents the worker from making new outbound
calls without stopping the worker process.  Useful during planned maintenance
of the Wishart service.  Local cache lookups and ingest continue normally while
the flag is set.

Pending tasks accumulate and are processed when the pause is cleared.

---

## Backup and Restore

### Running a backup

```bash
docker compose exec web python manage.py run_backup
```

This command:
1. Dumps all application tables to a compressed archive in the configured
   backup directory (default: `BACKUP_DIR` env var or `./backups/`).
2. Writes a JSON manifest listing the tables, row counts, and SHA-256 checksums.
3. Records a `BACKUP_OK` system event visible on the stats page.
4. Applies retention policy: keeps 7 daily, 4 weekly, and 12 monthly backups.

### Portable backup bundle

Use a portable bundle when you need to copy a backup out to an external disk,
another host, or a remote storage tool. The bundle contains one backup manifest
and every dump/artifact archive referenced by that manifest.

```bash
# Create a regular backup first.
docker compose exec web python manage.py run_backup

# Package the newest backup into one tarball.
docker compose exec web python manage.py export_backup_bundle \
  --output /backups/classyfire-portable-backup.tar.gz

# Copy the tarball from the web container to the host.
WEB=$(docker compose ps -q web)
docker cp "$WEB:/backups/classyfire-portable-backup.tar.gz" ./classyfire-portable-backup.tar.gz
```

To import a bundle back into a backup store:

```bash
# Copy the bundle into the web container.
WEB=$(docker compose ps -q web)
docker cp ./classyfire-portable-backup.tar.gz "$WEB:/tmp/classyfire-portable-backup.tar.gz"

# Verify and stage the bundle into /backups.
docker compose exec web python manage.py import_backup_bundle \
  /tmp/classyfire-portable-backup.tar.gz
```

Import is non-destructive: it verifies checksums and stages files in the backup
directory, but it does not restore the live database. Run the explicit restore
procedure only after stopping writers and confirming the imported manifest.

### Restore smoke test

To verify the most recent backup is readable:

```bash
docker compose exec web python manage.py restore_smoke_test
```

This reads the manifest, confirms all listed files are present with matching
checksums, and logs a `RESTORE_SMOKE_OK` event.

### Full restore

To restore from a backup (data loss operation — use with caution):

```bash
# 1. Identify the backup archive to restore
ls backups/

# 2. Stop the worker to prevent writes during restore
docker compose stop worker

# 3. Restore using Django management command
docker compose exec web python manage.py restore_from_backup \
  --backup-file backups/<archive_name>.tar.gz

# 4. Restart services
docker compose up -d
```

### Backup schedule

Configure automated backups via cron or a scheduler.  A daily backup at 02:00
is recommended:

```cron
0 2 * * * docker compose -f /path/to/docker-compose.yml exec -T web \
  python manage.py run_backup >> /var/log/classyfire-backup.log 2>&1
```

---

## Stats Page (`/stats/`)

The stats page provides real-time operational metrics:

| Metric | Description |
|---|---|
| Unique InChIKeys (live) | Compounds with a live Wishart classification. |
| Cache hit rate | Fraction of job items resolved from the live cache. |
| Remote queue | Pending / running / deferred task counts. |
| Circuit state | CLOSED (healthy), OPEN (outage), HALF_OPEN (recovering). |
| DB size | PostgreSQL database size in MB. |
| Backup status | Timestamp of the last successful backup. |

For automation, emit a machine-readable Gate 3 metrics snapshot:

```bash
docker compose exec web python manage.py emit_metrics_snapshot --json
```

The JSON includes remote queue state counts and due-work lag, provider circuit
and rate-bucket state, remote attempt classifications, mismatch event counts,
export artifact totals, and disk usage.  The command uses aggregate queries only
and is safe to run from monitoring jobs.

---

## Disk Guard

The application monitors disk usage and automatically pauses uploads when the
disk is critically full.

| Usage threshold | Action |
|---|---|
| < 70% | Normal operation |
| 70–84% | Warning logged; admin notified via stats page |
| 85–94% | Critical warning; new job submission may be paused |
| >= 95% | Emergency; upload endpoint returns 503 |

Monitor disk usage:

```bash
df -h /
```

Backup retention is applied after every backup run:

```bash
docker compose exec web python manage.py run_backup
```

Expired upload secrets, completed jobs, and export cache files can be removed
manually:

```bash
docker compose run --rm cleanup
```

---

## Admin Actions (`/admin/`)

The Django admin is available at `/admin/` for staff users.  Key admin actions:

- **BatchJob** — view job details, inspect item counts, manually cancel jobs.
- **RemoteTask** — inspect queue, reset stuck tasks.
- **SystemEvent** — audit log of all administrative actions.

### Pausing and resuming remote classification

From `/admin/`:

1. Navigate to **Admin Actions**.
2. Click **Pause remote** to set the `REMOTE_PAUSED` flag.
3. Click **Resume remote** to clear it.

Alternatively via the management command:

```bash
docker compose exec web python manage.py admin_pause_remote
docker compose exec web python manage.py admin_resume_remote
```

---

## Update Process

1. Pull the new image or code:
   ```bash
   git pull origin main
   docker compose pull
   docker compose build migrate web worker
   ```
2. Force-recreate the one-shot migration service with the rebuilt image, then
   recreate application services:
   ```bash
   docker compose up --force-recreate --abort-on-container-exit migrate
   docker compose up -d --build web worker
   ```
3. Verify: check `/stats/` and run the backup smoke test.

---

## Failure Response

### Worker not processing tasks

1. Check worker logs: `docker compose logs worker`
2. Inspect queue depth on `/stats/`
3. If circuit is OPEN: wait for recovery or use **Retry deferred** admin action
4. Restart worker: `docker compose restart worker`

### Database connection errors

1. Check PostgreSQL container: `docker compose ps db`
2. Review logs: `docker compose logs db`
3. Confirm `POSTGRES_*` env vars in `.env`

### Remote API unreachable

The circuit breaker will open after repeated failures.  Local lookups continue
normally.  Tasks are deferred and will be retried when the circuit recovers or
when **Retry deferred** is triggered manually.

### Disk full

1. Check disk: `df -h /`
2. Run backup retention: `docker compose exec web python manage.py run_backup`
3. Remove expired uploads and exports: `docker compose run --rm cleanup`
4. Consider moving the backup directory to a larger volume

`cleanup_expired` removes transient web-job data only: completed/failed batch
job pages and their linked upload artifacts after `JOB_RETENTION_DAYS`
(default 7), plus expired export files after `EXPORT_RETENTION_DAYS`
(default 7). It does not delete the ClassyFire classification cache tables
(`classyfire_live` or `classification_history`).

---

## Gate 3 Load Probe

The Gate 3 load probe is a manual PostgreSQL sanity check for query plans under
large row counts. It is dry-run by default and only prints the SQL plus the
explicit `PG*` environment-derived `psql` command:

```bash
PGHOST=127.0.0.1 PGPORT=55432 PGDATABASE=classyfire PGUSER=classyfire \
  uv run --locked scripts/gate3_load_probe.py --rows 1000000 --dry-run
```

To execute against a disposable or staging database, pass `--execute`. The probe
uses a temporary table and records EXPLAIN output on stdout:

```bash
PGHOST=127.0.0.1 PGPORT=55432 PGDATABASE=classyfire_probe PGUSER=classyfire \
  uv run --locked scripts/gate3_load_probe.py --rows 1000000 --execute \
  | tee gate3-load-probe-$(date +%Y%m%d).log
```

Use `--explain-only` when the temporary table was prepared separately and only
the EXPLAIN statements should be printed or executed.
