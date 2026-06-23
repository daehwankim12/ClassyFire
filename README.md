# ClassyFire Batch Cache Web

A Django batch web application for managing and processing chemical data with RDKit and PostgreSQL. Supports compound import, caching, and REST APIs.

## Quick Start

### Installation

1. Clone the repository
2. Create and activate a virtual environment:
   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install -e ".[dev]"
   ```

### Development

Run tests:
```bash
make test
```

Format code:
```bash
make format
```

Check code style:
```bash
make lint
```

Type check:
```bash
make typecheck
```

### Database Setup

1. Ensure PostgreSQL is running
2. Configure `.env` with your database credentials
3. Run migrations:
   ```bash
   python manage.py migrate
   ```

## Seed Data Management

The application includes commands for downloading, verifying, and importing chemical seed data:

### Download seeds
```bash
make seed-download
```

### Verify seeds
```bash
make seed-verify
```

### Import seeds (resumable)
```bash
make seed-import
```

### Audit seed data
```bash
make seed-audit
```

### Activate seeds in cache
```bash
make seed-activate
```

## Ngrok Runbook

For exposing the local development server to the internet:

1. Install ngrok: `brew install ngrok` (or your OS equivalent)
2. Start the Django development server: `python manage.py runserver`
3. In another terminal, create a tunnel:
   ```bash
   ngrok http 8000
   ```
4. Use the generated public URL for webhook testing or external integrations

## Backup and Restore

### Backup the database
```bash
pg_dump -U postgres classyfire > backup.sql
```

### Restore from backup
```bash
psql -U postgres classyfire < backup.sql
```

## Project Structure

- `apps/` - Django applications
- `config/` - Project configuration and settings
- `manage.py` - Django management script
- `Makefile` - Common development commands
- `pyproject.toml` - Project metadata and dependencies

## Acceptance Workflow

1. Implement feature in feature branch
2. Run all tests: `make test`
3. Run linters: `make lint`
4. Run type checker: `make typecheck`
5. Create pull request with feature branch
6. Merge to main after review approval

## License

MIT
