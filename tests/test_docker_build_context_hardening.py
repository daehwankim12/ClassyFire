from __future__ import annotations

from pathlib import Path


def test_dockerignore_excludes_secret_and_artifact_paths():
    ignore = Path(".dockerignore").read_text()
    required = [
        ".git",
        ".env",
        ".env.*",
        ".venv",
        "media",
        "uploads",
        "exports",
        "export_cache",
        "backups",
        "*.dump",
        "*.sql",
        "*.sqlite3",
        "*.log",
    ]
    for pattern in required:
        assert pattern in ignore
    assert "!.env.example" in ignore


def test_dockerfile_uses_selective_copy_not_broad_copy_all():
    dockerfile = Path("Dockerfile").read_text()
    assert "COPY . ." not in dockerfile
    for directive in [
        "COPY pyproject.toml uv.lock ./",
        "COPY manage.py ./",
        "COPY apps ./apps",
        "COPY config ./config",
        "COPY templates ./templates",
        "COPY static ./static",
        "COPY deploy ./deploy",
    ]:
        assert directive in dockerfile


def test_image_secret_scan_script_checks_history_and_layers():
    script = Path("scripts/check_image_for_secrets.sh").read_text()
    assert "docker history" in script
    assert "docker save" in script
    assert "CLASSYFIRE_SENTINEL_SECRET" in script
