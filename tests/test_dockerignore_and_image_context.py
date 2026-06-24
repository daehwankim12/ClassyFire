from pathlib import Path


def test_dockerignore_excludes_secrets_and_artifacts():
    content = Path(".dockerignore").read_text()
    required = {
        ".git",
        ".env",
        ".env.*",
        "!.env.example",
        ".venv",
        "export_cache",
        "media",
        "uploads",
        "exports",
        "backups",
        "*.dump",
        "*.sql",
    }
    for pattern in required:
        assert pattern in content


def test_dockerfile_uses_locked_runtime_install_and_selected_copy():
    dockerfile = Path("Dockerfile").read_text()
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "uv sync --locked --no-dev --no-editable" in dockerfile
    assert "COPY . ." not in dockerfile
    assert "|| echo" not in dockerfile
