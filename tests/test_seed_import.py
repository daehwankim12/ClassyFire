"""Tests for the ChemOnt and seed import pipeline (Task 4).

All tests are fixture-driven against the test DB (test_cf_test_wt4).
No mocks — behaviour is asserted against real DB state.

Fixture files used:
  - tests/fixtures/chemont_dictionary.tsv
  - tests/fixtures/seed_sample.tsv.zst  (3 data rows)
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
import zstandard as zstd
from django.db import connection

FIXTURES_DIR = Path(__file__).parent / "fixtures"
CHEMONT_TSV = FIXTURES_DIR / "chemont_dictionary.tsv"
SEED_ZST = FIXTURES_DIR / "seed_sample.tsv.zst"

INCHIKEY_RE = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _import_chemont(replace: bool = True):
    """Import the test ChemOnt fixture."""
    from apps.chemont.importers import import_chemont
    return import_chemont(CHEMONT_TSV, replace=replace)


def _create_seed_import(source: Path):
    """Create a SeedImport row for *source* and return it."""
    from apps.classifications.seed_manifest import compute_sha256, create_seed_import_row
    manifest = {
        "doi": None,
        "record_id": "test",
        "filename": source.name,
        "checksum": compute_sha256(source),
        "license": "CC-BY-4.0",
        "expected_row_count": 3,
    }
    return create_seed_import_row(manifest)


def _stream_import(seed_import_id: int, *, resume: bool = False):
    """Run stream_import with the test fixture."""
    from apps.classifications.seed_stream import stream_import
    return stream_import(
        SEED_ZST,
        seed_import_id,
        "fixture",
        target_table="classyfire_seed",
        resume=resume,
    )


# ---------------------------------------------------------------------------
# TEST: Checksum validation
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_checksum_matches():
    """SHA-256 of the test fixture must match the value computed at import time."""
    from apps.classifications.seed_manifest import compute_sha256
    digest = compute_sha256(SEED_ZST)
    # Must be a 64-char hex string.
    assert re.match(r"^[0-9a-f]{64}$", digest), f"Bad digest: {digest!r}"
    # Must be stable (same call twice).
    assert compute_sha256(SEED_ZST) == digest


@pytest.mark.django_db(transaction=True)
def test_checksum_stored_in_seed_import():
    """The SeedImport row stores the checksum of the source file."""
    from apps.classifications.seed_manifest import compute_sha256
    si = _create_seed_import(SEED_ZST)
    assert si.checksum == compute_sha256(SEED_ZST)


@pytest.mark.django_db(transaction=True)
def test_checksum_mismatch_detected(tmp_path):
    """Verify command raises if expected checksum does not match."""
    from django.core.management import call_command
    from django.core.management.base import CommandError

    bad_file = tmp_path / "bad.tsv.zst"
    bad_file.write_bytes(b"not-the-real-content")
    with pytest.raises(CommandError, match="mismatch"):
        call_command(
            "seed_verify",
            str(bad_file),
            expected="0" * 64,
        )


# ---------------------------------------------------------------------------
# TEST: ChemOnt parent integrity
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_import_chemont_all_nodes():
    """All rows in chemont_dictionary.tsv are loaded."""
    _import_chemont()
    from apps.chemont.models import ChemOntNode
    assert ChemOntNode.objects.count() == 6  # 6 data rows


@pytest.mark.django_db(transaction=True)
def test_import_chemont_parent_refs():
    """Every node with a parent_id has its parent FK set correctly."""
    _import_chemont()
    from apps.chemont.models import ChemOntNode
    root = ChemOntNode.objects.get(chemont_id="CHEMONT:0000000")
    assert root.parent is None

    organic = ChemOntNode.objects.get(chemont_id="CHEMONT:0000001")
    assert organic.parent_id == root.numeric_id

    lipids = ChemOntNode.objects.get(chemont_id="CHEMONT:0000002")
    assert lipids.parent_id == organic.numeric_id


@pytest.mark.django_db(transaction=True)
def test_import_chemont_bad_parent_raises(tmp_path):
    """TSV with an unknown parent_id raises ChemOntImportError."""
    from apps.chemont.importers import ChemOntImportError, import_chemont

    bad_tsv = tmp_path / "bad.tsv"
    bad_tsv.write_text(
        "chemont_id\tname\tparent_id\tontology_version\n"
        "CHEMONT:0001000\tOrphan\tCHEMONT:9999999\t2.1\n"
    )
    with pytest.raises(ChemOntImportError, match="Parent reference integrity"):
        import_chemont(bad_tsv)


# ---------------------------------------------------------------------------
# TEST: Streaming TSV import
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_stream_import_rows_in_seed_table():
    """Three seed rows from the fixture land in classyfire_seed after import."""
    _import_chemont()
    si = _create_seed_import(SEED_ZST)
    result = _stream_import(si.pk)

    assert result["rows_written"] == 3
    assert result["rows_skipped"] == 0
    assert result["duplicates"] == 0

    from apps.classifications.models import ClassyfireSeed
    assert ClassyfireSeed.objects.count() == 3


@pytest.mark.django_db(transaction=True)
def test_stream_import_inchikeys_correct():
    """The imported InChIKeys match expected values."""
    _import_chemont()
    si = _create_seed_import(SEED_ZST)
    _stream_import(si.pk)

    from apps.classifications.models import ClassyfireSeed
    keys = set(ClassyfireSeed.objects.values_list("inchikey", flat=True))
    assert "RDHQFKQIGNGIED-MRVPVSSYSA-N" in keys
    assert "WQZGKKKJIJFFOK-GASJEMHNSA-N" in keys
    assert "LFQSCWFLJHTTHZ-UHFFFAOYSA-N" in keys


@pytest.mark.django_db(transaction=True)
def test_stream_import_seed_source_id():
    """Every imported row carries the correct seed_source_id (SeedImport PK)."""
    _import_chemont()
    si = _create_seed_import(SEED_ZST)
    _stream_import(si.pk)

    from apps.classifications.models import ClassyfireSeed
    source_ids = set(ClassyfireSeed.objects.values_list("seed_source_id", flat=True))
    assert source_ids == {si.pk}


# ---------------------------------------------------------------------------
# TEST: Duplicate InChIKey reporting
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_duplicate_inchikey_detected(tmp_path):
    """A .tsv.zst with a duplicate InChIKey is reported in the audit."""
    _import_chemont()

    # Build a TSV with a duplicate.
    rows = [
        "inchikey\tinchikey14\tpubchem_cid\tzinc_id\trepresentative_smiles\t"
        "kingdom_id\tsuperclass_id\tclass_id\tsubclass_id\tdirect_parent_id\tother_labels",
        "RDHQFKQIGNGIED-MRVPVSSYSA-N\tRDHQFKQIGNGIED\t5988\t\t\t0\t1\t5\t\t\t",
        "RDHQFKQIGNGIED-MRVPVSSYSA-N\tRDHQFKQIGNGIED\t5988\t\t\t0\t1\t5\t\t\t",  # dup
    ]
    content = "\n".join(rows) + "\n"
    cctx = zstd.ZstdCompressor()
    dup_zst = tmp_path / "dup.tsv.zst"
    dup_zst.write_bytes(cctx.compress(content.encode()))

    from apps.classifications.seed_manifest import compute_sha256, create_seed_import_row
    from apps.classifications.seed_stream import stream_import
    manifest = {
        "doi": None,
        "record_id": "test",
        "filename": dup_zst.name,
        "checksum": compute_sha256(dup_zst),
        "license": None,
        "expected_row_count": None,
    }
    si = create_seed_import_row(manifest)
    result = stream_import(
        dup_zst,
        si.pk,
        "test-dup",
        target_table="classyfire_seed",
    )
    # One row written, one duplicate skipped via ON CONFLICT DO NOTHING.
    assert result["rows_written"] == 1
    assert result["duplicates"] == 1


# ---------------------------------------------------------------------------
# TEST: Checkpoint / resume — no duplicates
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_checkpoint_resume_no_duplicates():
    """Interrupt then resume results in exactly 3 rows (no duplicates)."""
    _import_chemont()

    from apps.classifications.seed_manifest import compute_sha256, create_seed_import_row
    from apps.classifications.seed_stream import stream_import

    manifest = {
        "doi": None,
        "record_id": "test",
        "filename": SEED_ZST.name,
        "checksum": compute_sha256(SEED_ZST),
        "license": None,
        "expected_row_count": 3,
    }
    si = create_seed_import_row(manifest)

    # Simulate first import of 2 rows (checkpoint = 2).
    # We do this by importing all 3 then faking the checkpoint at 2.
    r1 = stream_import(SEED_ZST, si.pk, "fixture", target_table="classyfire_seed")
    assert r1["rows_written"] == 3

    # Reset: delete all rows and set checkpoint at 2 (simulate partial import).
    from apps.classifications.models import ClassyfireSeed
    ClassyfireSeed.objects.all().delete()

    # Fake checkpoint at offset 2 (2 rows already "done").
    si.refresh_from_db()
    ar = si.audit_result or {}
    ar["checkpoint"] = {"offset": 2}
    si.audit_result = ar
    si.actual_row_count = 0
    si.import_status = "IMPORTED"
    si.save(update_fields=["audit_result", "actual_row_count", "import_status"])

    # Now run with resume=True — should only import row 3.
    r2 = stream_import(
        SEED_ZST, si.pk, "fixture", target_table="classyfire_seed", resume=True
    )
    assert r2["rows_written"] == 1  # Only the 3rd row.
    assert r2["rows_skipped"] == 2

    # Total in DB: 1 (only the resumed row).
    assert ClassyfireSeed.objects.count() == 1

    # Running resume again should not add duplicates.
    stream_import(
        SEED_ZST, si.pk, "fixture", target_table="classyfire_seed", resume=True
    )
    # checkpoint is now at 3 (all rows processed), so nothing new.
    assert ClassyfireSeed.objects.count() == 1


# ---------------------------------------------------------------------------
# TEST: Dry-run summary (no writes)
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_dry_run_no_writes():
    """Dry-run parses all rows without inserting any into classyfire_seed."""
    _import_chemont()
    si = _create_seed_import(SEED_ZST)

    from apps.classifications.seed_stream import stream_import
    result = stream_import(
        SEED_ZST,
        si.pk,
        "fixture",
        target_table="classyfire_seed",
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["rows_written"] == 0

    from apps.classifications.models import ClassyfireSeed
    assert ClassyfireSeed.objects.count() == 0


# ---------------------------------------------------------------------------
# TEST: Atomic current-view activation
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_seed_activate_view_serves_rows():
    """After seed_activate, classyfire_seed_current returns seeded rows."""
    _import_chemont()
    si = _create_seed_import(SEED_ZST)
    _stream_import(si.pk)

    # Run audit — sets import_status to AUDIT_PASS.
    from django.core.management import call_command
    call_command("seed_audit", seed_import_id=si.pk)

    si.refresh_from_db()
    assert si.import_status == "AUDIT_PASS", f"Expected AUDIT_PASS, got {si.import_status}"

    # Activate.
    call_command("seed_activate", seed_import_id=si.pk, version="fixture")
    si.refresh_from_db()
    assert si.import_status == "ACTIVE"
    assert si.activated_table == "classyfire_seed"

    # View must exist and return our 3 rows.
    with connection.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM classyfire_seed_current")
        count = cur.fetchone()[0]
    assert count == 3, f"Expected 3 rows via view, got {count}"


@pytest.mark.django_db(transaction=True)
def test_seed_activate_view_survives_multiple_calls():
    """Calling seed_activate twice (idempotent) does not break the view."""
    _import_chemont()
    si = _create_seed_import(SEED_ZST)
    _stream_import(si.pk)

    from django.core.management import call_command
    call_command("seed_audit", seed_import_id=si.pk)
    call_command("seed_activate", seed_import_id=si.pk, version="fixture")

    # Force second activation with --force.
    si.import_status = "AUDIT_PASS"
    si.save(update_fields=["import_status"])
    call_command("seed_activate", seed_import_id=si.pk, version="fixture2", force=True)

    with connection.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM classyfire_seed_current")
        count = cur.fetchone()[0]
    assert count == 3


# ---------------------------------------------------------------------------
# TEST: Zenodo metadata offline / dry-run
# ---------------------------------------------------------------------------

def test_zenodo_dry_run_returns_manifest():
    """Dry-run Zenodo resolve returns a placeholder without network access."""
    from apps.classifications.seed_manifest import resolve_zenodo_record
    manifest = resolve_zenodo_record(20472700, dry_run=True)
    assert manifest["dry_run"] is True
    assert manifest["record_id"] == "20472700"
    assert manifest["doi"].startswith("10.5281/zenodo.")
    assert manifest["filename"].endswith(".tsv.zst")


# ---------------------------------------------------------------------------
# TEST: seed_download --dry-run management command
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_seed_download_dry_run():
    """seed_download --dry-run prints manifest without writing DB."""
    from django.core.management import call_command

    from apps.operations.models import SeedImport

    before = SeedImport.objects.count()
    call_command("seed_download", dry_run=True)
    after = SeedImport.objects.count()
    assert after == before  # No DB write.


# ---------------------------------------------------------------------------
# TEST: Audit catches bad InChIKey
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_audit_catches_bad_inchikey(tmp_path):
    """Audit marks AUDIT_FAIL when a row has a malformed InChIKey."""
    _import_chemont()

    # Build a TSV with a bad InChIKey.
    rows_txt = (
        "inchikey\tinchikey14\tpubchem_cid\tzinc_id\trepresentative_smiles\t"
        "kingdom_id\tsuperclass_id\tclass_id\tsubclass_id\tdirect_parent_id\tother_labels\n"
        "BADINCHIKEY_BAD-BADBADBAD-X\tBADINNCHIKEY_B\t\t\t\t0\t1\t5\t\t\t\n"
    )
    cctx = zstd.ZstdCompressor()
    bad_zst = tmp_path / "bad.tsv.zst"
    bad_zst.write_bytes(cctx.compress(rows_txt.encode()))

    from apps.classifications.seed_manifest import compute_sha256, create_seed_import_row
    from apps.classifications.seed_stream import stream_import
    manifest = {
        "doi": None,
        "record_id": "test",
        "filename": bad_zst.name,
        "checksum": compute_sha256(bad_zst),
        "license": None,
        "expected_row_count": None,
    }
    si = create_seed_import_row(manifest)
    stream_import(bad_zst, si.pk, "test-bad", target_table="classyfire_seed")

    from django.core.management import call_command
    call_command("seed_audit", seed_import_id=si.pk)

    si.refresh_from_db()
    assert si.import_status == "AUDIT_FAIL"
    assert si.audit_result["checks"]["inchikey_format"]["bad_count"] >= 1


# ---------------------------------------------------------------------------
# TEST: seed_activate refuses non-AUDIT_PASS without --force
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_seed_activate_refuses_non_audit_pass():
    """seed_activate raises CommandError if import_status != AUDIT_PASS."""
    _import_chemont()
    si = _create_seed_import(SEED_ZST)
    _stream_import(si.pk)
    # import_status is IMPORTED, not AUDIT_PASS.

    from django.core.management import call_command
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="AUDIT_PASS"):
        call_command("seed_activate", seed_import_id=si.pk)
