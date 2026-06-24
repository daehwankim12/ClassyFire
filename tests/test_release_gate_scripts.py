from pathlib import Path


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
