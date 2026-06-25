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


def _volume_mounts(service: dict) -> list[tuple[str, str, str]]:
    """Return list of (source, target, opts) tuples from a service's volumes list.

    Parses "source:target" and "source:target:opts" string entries only
    (long-form dict entries are not used in this compose file for named volumes).
    """
    result = []
    for v in service.get("volumes", []):
        if isinstance(v, str):
            parts = v.split(":")
            source = parts[0] if len(parts) >= 1 else ""
            target = parts[1] if len(parts) >= 2 else ""
            opts = parts[2] if len(parts) >= 3 else ""
            result.append((source, target, opts))
    return result


def test_uploads_volume_shared_between_web_and_ingest_worker():
    """Regression test for H-04: the uploads spool volume must be a named volume
    shared across web (writer), ingest-worker (reader), and cleanup (reaper).
    Without the shared named volume each container sees its own empty /app/uploads
    and the ingest-worker never finds the file that web spooled.
    """
    data = yaml.safe_load(Path("docker-compose.yml").read_text())

    # 1. The top-level volumes map must declare the uploads volume.
    top_level_volumes = data.get("volumes", {})
    assert "uploads" in top_level_volumes, (
        "'uploads' named volume missing from top-level volumes map"
    )

    # 2. web must mount the uploads volume at /app/uploads (it spools files there).
    web_mounts = _volume_mounts(data["services"]["web"])
    web_upload_sources = [src for src, tgt, _ in web_mounts if tgt == "/app/uploads"]
    assert web_upload_sources == ["uploads"], (
        f"web must mount 'uploads' volume at /app/uploads; got: {web_upload_sources}"
    )

    # 3. ingest-worker must mount the SAME uploads volume at /app/uploads (reads files).
    ingest_mounts = _volume_mounts(data["services"]["ingest-worker"])
    ingest_upload_sources = [src for src, tgt, _ in ingest_mounts if tgt == "/app/uploads"]
    assert ingest_upload_sources == ["uploads"], (
        f"ingest-worker must mount 'uploads' volume at /app/uploads; got: {ingest_upload_sources}"
    )

    # 4. cleanup must mount the uploads volume so it can reap stale upload temps.
    cleanup_mounts = _volume_mounts(data["services"]["cleanup"])
    cleanup_upload_sources = [src for src, tgt, _ in cleanup_mounts if tgt == "/app/uploads"]
    assert cleanup_upload_sources == ["uploads"], (
        f"cleanup must mount 'uploads' volume at /app/uploads; got: {cleanup_upload_sources}"
    )
