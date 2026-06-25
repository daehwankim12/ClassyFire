from pathlib import Path

import yaml


def test_compose_has_default_ingest_worker_service():
    data = yaml.safe_load(Path("docker-compose.yml").read_text())
    services = data["services"]
    worker = services["ingest-worker"]

    assert worker["command"] == ["python", "manage.py", "process_jobs"]
    assert "profiles" not in worker
    assert worker["restart"] == "unless-stopped"
    assert worker["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert "internal" in worker["networks"]
    assert "external" in worker["networks"]


def test_cleanup_service_runs_by_default():
    data = yaml.safe_load(Path("docker-compose.yml").read_text())
    services = data["services"]
    assert "cleanup" in services
    cleanup = services["cleanup"]
    assert cleanup.get("profiles") in (None, [])
    command = cleanup["command"]
    assert "cleanup" in " ".join(command)
    assert cleanup["restart"] == "unless-stopped"
