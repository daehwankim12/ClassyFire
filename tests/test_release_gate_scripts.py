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


def test_secret_scan_scripts_fail_on_missing_args():
    image_script = Path("scripts/check_image_for_secrets.sh").read_text()
    log_script = Path("scripts/check_proxy_logs_for_secrets.sh").read_text()
    assert "set -euo pipefail" in image_script
    assert "set -euo pipefail" in log_script
    assert "CLASSYFIRE_SENTINEL_SECRET" in image_script
    assert "CLASSYFIRE_SENTINEL_SECRET" in log_script


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
    assert '-F "input_file=@${upload_file};filename=${upload_name};type=text/csv"' in script
    assert '-F "file=@${upload_file};filename=${upload_name};type=text/csv"' not in script


def test_secret_route_exerciser_records_dynamic_secrets_for_log_scan():
    script = Path("scripts/exercise_secret_routes.sh").read_text()
    log_script = Path("scripts/check_proxy_logs_for_secrets.sh").read_text()

    for needle in [
        "CLASSYFIRE_SECRET_OUTPUT",
        "printf '%s\\n' \"$result_secret\"",
        "printf '%s\\n' \"$token\"",
        "urllib.parse.quote",
    ]:
        assert needle in script
    assert "--secret-file FILE" in log_script
    assert 'if [ "${1:-}" = "--secret-file" ]; then' in log_script


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
