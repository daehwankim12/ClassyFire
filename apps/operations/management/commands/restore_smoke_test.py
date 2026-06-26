"""Management command: restore latest backup into a smoke-test DB and verify."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test import Client


def _client_host(allowed_hosts: Sequence[str]) -> str:
    for host in allowed_hosts:
        if host and host != "*":
            return host
    return "localhost"


def _find_export_file(export_dir: Path, token: str) -> Path:
    matches = [
        path
        for path in export_dir.rglob(f"{token}.*")
        if path.is_file() and not path.name.endswith(".meta.json")
    ]
    if not matches:
        raise CommandError(f"restored export file not found for token: {token}")
    if len(matches) > 1:
        raise CommandError(f"multiple restored export files found for token: {token}")
    return matches[0]


def _response_bytes(response) -> bytes:
    if hasattr(response, "streaming_content"):
        return b"".join(response.streaming_content)
    return bytes(response.content)


class Command(BaseCommand):
    help = "Restore the latest backup into a separate DB and run a smoke check."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--backup-dir", default=None)
        parser.add_argument(
            "--target-db",
            default=None,
            help="Target DB name (default: settings.RESTORE_SMOKE_DB or classyfire_smoke_test).",
        )
        parser.add_argument(
            "--check-export-token",
            default=None,
            help="Download /api/exports/<token> from the active DB and verify bytes.",
        )
        parser.add_argument(
            "--export-dir",
            default=None,
            help="Restored export directory containing <token>.* for download verification.",
        )

    def handle(self, *args, **options) -> None:
        export_token = options.get("check_export_token")
        if export_token:
            self._check_export_download(
                token=export_token,
                export_dir=Path(options["export_dir"]) if options["export_dir"] else None,
            )
            return

        from apps.operations.backup import restore_smoke_test

        backup_dir = Path(options["backup_dir"]) if options["backup_dir"] else None
        target_db = options.get("target_db")

        result = restore_smoke_test(backup_dir=backup_dir, target_db=target_db)

        if result.get("status") == "ok":
            self.stdout.write(self.style.SUCCESS(f"Smoke test passed: {result}"))
        else:
            self.stderr.write(self.style.ERROR(f"Smoke test failed: {result}"))

    def _check_export_download(self, *, token: str, export_dir: Path | None) -> None:
        from apps.batches.models import ExportArtifact

        expected_bytes: bytes | None = None
        if export_dir is not None:
            export_path = _find_export_file(export_dir, token)
            expected_bytes = export_path.read_bytes()
            ExportArtifact.objects.filter(token=token).update(path=str(export_path))

        response = Client().get(
            f"/api/exports/{token}",
            HTTP_HOST=_client_host(settings.ALLOWED_HOSTS),
            secure=True,
        )
        if response.status_code != 200:
            raise CommandError(
                f"/api/exports/{token} returned HTTP {response.status_code}"
            )

        actual_bytes = _response_bytes(response)
        if not actual_bytes:
            raise CommandError(f"/api/exports/{token} returned an empty body")
        if expected_bytes is not None and actual_bytes != expected_bytes:
            raise CommandError(f"/api/exports/{token} body did not match restored file")

        self.stdout.write(
            self.style.SUCCESS(
                f"Export download smoke passed: /api/exports/{token}"
            )
        )
