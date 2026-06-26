from __future__ import annotations

from pathlib import Path


def test_restore_smoke_command_supports_export_download_mode():
    from apps.operations.management.commands.restore_smoke_test import (
        Command,
        _client_host,
    )

    parser = Command().create_parser("manage.py", "restore_smoke_test")
    options = parser.parse_args(
        [
            "--check-export-token",
            "smoke-export-token",
            "--export-dir",
            "/tmp/restored/export_files",
        ]
    )

    assert options.check_export_token == "smoke-export-token"
    assert options.export_dir == "/tmp/restored/export_files"
    assert _client_host(["localhost", "www.localhost"]) == "localhost"
    assert _client_host(["*", "example.com"]) == "example.com"


def test_restore_smoke_command_checks_export_download_view():
    command = Path(
        "apps/operations/management/commands/restore_smoke_test.py"
    ).read_text()

    assert "/api/exports/{token}" in command
    assert "Client()" in command
    assert "HTTP_HOST=_client_host(settings.ALLOWED_HOSTS)" in command
    assert "secure=True" in command
    assert "streaming_content" in command
    assert "ExportArtifact.objects.filter(token=token).update" in command


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
    assert '"${base}/stats/"' in script
    assert "/api/stats/" not in script
    for needle in [
        "?secret=",
        "X-Job-Secret",
        "/claim/",
        "/api/exports/",
        "sentinel-upload-filename",
    ]:
        assert needle in script


def test_secret_route_exerciser_does_not_follow_secure_cookie_redirect_locally():
    script = Path("scripts/exercise_secret_routes.sh").read_text()

    assert 'claim_status="$(curl -sS -o /dev/null -w' in script
    assert 'test "$claim_status" = "303"' in script
    assert '-L "${base}/jobs/${job_id}/claim/${result_secret}/"' not in script


def test_backup_restore_smoke_requires_export_download_check():
    script = Path("scripts/run_backup_restore_smoke.sh").read_text()

    assert "--check-export-token" in script
    assert "--export-dir" in script
    assert "_compute_rows_signature(job)" in script
    assert "hashlib.sha256(b'smoke').hexdigest()" not in script
    assert "not yet supported" not in script
    assert "skipping HTTP check" not in script
