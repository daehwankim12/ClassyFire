from __future__ import annotations

from pathlib import Path


def test_release_workflow_runs_proxy_runtime_traffic():
    workflow = Path(".github/workflows/release-gates.yml").read_text()
    assert "docker compose up -d --build" in workflow
    assert "scripts/exercise_secret_routes.sh" in workflow
    assert "scripts/check_proxy_logs_for_secrets.sh" in workflow


def test_release_workflow_runs_real_backup_restore():
    workflow = Path(".github/workflows/release-gates.yml").read_text()
    assert "scripts/run_backup_restore_smoke.sh" in workflow
    assert "pg_restore" in workflow or "restore_smoke_test" in workflow


def test_release_gate_workflow_has_required_jobs():
    workflow = Path(".github/workflows/release-gates.yml").read_text()
    for name in [
        "lint-type-test",
        "postgres-integration",
        "docker-build-secret-scan",
        "proxy-log-redaction",
        "backup-restore-smoke",
    ]:
        assert name in workflow


def test_secret_scan_scripts_fail_on_missing_args():
    image_script = Path("scripts/check_image_for_secrets.sh").read_text()
    log_script = Path("scripts/check_proxy_logs_for_secrets.sh").read_text()
    assert "set -euo pipefail" in image_script
    assert "set -euo pipefail" in log_script
    assert "CLASSYFIRE_SENTINEL_SECRET" in image_script
    assert "CLASSYFIRE_SENTINEL_SECRET" in log_script


def test_release_workflow_validates_compose_and_consumes_generated_secrets():
    workflow = Path(".github/workflows/release-gates.yml").read_text()
    # The compose configuration is rendered/validated before bringing services up.
    assert "docker compose config" in workflow
    # The exerciser emits the runtime-generated secrets to a file...
    assert "CLASSYFIRE_SECRET_OUTPUT" in workflow
    # ...which the log checker consumes literally.
    assert "--secret-file" in workflow
    # The backup/restore smoke runs inside an app+pg-client-capable service.
    assert (
        "docker compose run --rm web bash scripts/run_backup_restore_smoke.sh"
        in workflow
    )


def test_secret_route_exerciser_hits_sensitive_routes():
    script = Path("scripts/exercise_secret_routes.sh").read_text()
    for needle in [
        "?secret=",
        "X-Job-Secret",
        "/claim/",
        "/api/exports/",
        "sentinel-upload-filename",
    ]:
        assert needle in script
