import pytest
import yaml
from django.test import Client


@pytest.mark.django_db
def test_health_endpoint():
    client = Client()
    response = client.get("/health/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


def load_compose():
    with open("docker-compose.yml") as f:
        return yaml.safe_load(f)


def read_file(path: str) -> str:
    with open(path) as f:
        return f.read()


def test_web_has_external_network():
    compose = load_compose()
    web_networks = compose["services"]["web"]["networks"]
    assert "external" in web_networks


def test_worker_has_external_network():
    compose = load_compose()
    worker_networks = compose["services"]["worker"]["networks"]
    assert "external" in worker_networks


def test_compose_web_and_worker_wait_for_migrate():
    compose = load_compose()
    services = compose["services"]

    assert "migrate" in services
    assert services["migrate"]["command"] == ["python", "manage.py", "migrate", "--noinput"]
    assert services["migrate"]["restart"] == "no"

    for service_name in ["web", "worker"]:
        depends_on = services[service_name]["depends_on"]
        assert depends_on["db"]["condition"] == "service_healthy"
        assert depends_on["migrate"]["condition"] == "service_completed_successfully"


def test_documented_deploy_reruns_migrate_before_web_and_worker():
    readme = read_file("README.md")
    operations = read_file("docs/operations.md")

    expected = (
        "docker compose up -d --force-recreate migrate\n"
        "docker compose up -d web worker"
    )
    assert expected in readme
    assert expected in "\n".join(line.strip() for line in operations.splitlines())


def test_dockerfile_uses_uv_frozen_runtime_install():
    dockerfile = read_file("Dockerfile")

    assert "uv==0.11.21" in dockerfile
    assert "uv sync --frozen" in dockerfile
    assert "--no-dev" in dockerfile
    assert "pip install --no-cache-dir ." not in dockerfile
    assert '".[dev]"' not in dockerfile


def test_db_no_external_network():
    compose = load_compose()
    db_networks = compose["services"]["db"]["networks"]
    assert "external" not in db_networks


def test_backup_volume_mounted():
    compose = load_compose()
    backup_service = compose["services"]["backup"]
    volumes = backup_service.get("volumes", [])
    # Check that /backups is mounted (volume could be named or bind mount)
    mount_targets = []
    for v in volumes:
        if isinstance(v, str):
            # "source:target" or "source:target:options"
            parts = v.split(":")
            if len(parts) >= 2:
                mount_targets.append(parts[1])
        elif isinstance(v, dict):
            mount_targets.append(v.get("target", ""))
    assert "/backups" in mount_targets


def test_no_host_ports():
    compose = load_compose()
    for svc_name in ["db", "web", "worker", "backup"]:
        svc = compose["services"].get(svc_name, {})
        assert not svc.get("ports"), f"{svc_name} should not publish host ports"


def test_external_network_is_not_internal():
    compose = load_compose()
    networks = compose.get("networks", {})
    ext = networks.get("external", {})
    assert not ext.get("internal", False)


def test_internal_network_is_internal():
    compose = load_compose()
    networks = compose.get("networks", {})
    internal = networks.get("internal", {})
    assert internal.get("internal", False) is True
