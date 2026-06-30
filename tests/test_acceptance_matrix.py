"""Acceptance Matrix — verifies every T-xx / NFR-Pxx ID has evidence.

For AUTOMATED entries the test checks that the referenced test function
actually exists in the source file (via regex scan of the file on disk).

For MANUAL entries the test accepts the entry as valid evidence provided it
carries a non-empty ``reason`` and a ``docs_pointer``.

The test FAILS if any acceptance ID has no entry at all.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TypedDict

import pytest

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

TESTS_DIR = Path(__file__).parent
REPO_ROOT = TESTS_DIR.parent


class AutomatedEntry(TypedDict):
    kind: str  # "automated"
    file: str  # relative to tests/
    tests: list[str]


class ManualEntry(TypedDict):
    kind: str  # "manual"
    reason: str
    docs_pointer: str


Evidence = AutomatedEntry | ManualEntry


# ---------------------------------------------------------------------------
# Evidence map
# ---------------------------------------------------------------------------

EVIDENCE: dict[str, Evidence] = {
    # ------------------------------------------------------------------
    # T-01: Mixed input (SMILES/InChI/InChIKey) preserves order+dups
    # ------------------------------------------------------------------
    "T-01": {
        "kind": "automated",
        "file": "test_batch_ingest.py",
        "tests": [
            "test_100_row_mixed_input_preserves_order_and_count",
            "test_duplicate_rows_preserved",
        ],
    },
    # ------------------------------------------------------------------
    # T-02: RDKit identity (canonical SMILES/InChIKey computed by RDKit)
    # ------------------------------------------------------------------
    "T-02": {
        "kind": "automated",
        "file": "test_input_normalization.py",
        "tests": [
            "test_smiles_aspirin",
            "test_inchi_aspirin",
            "test_inchikey_bare",
            "test_chiral_inchikey_differs_from_achiral",
            "test_chiral_canonical_smiles_preserves_stereo",
            "test_deuterated_differs_from_protio",
            "test_deuterated_inchikey_fixed",
            "test_salt_inchikey_differs_from_acid_alone",
            "test_salt_canonical_smiles_contains_both_components",
        ],
    },
    # ------------------------------------------------------------------
    # T-03: Live cache lookup (local cache hit)
    # ------------------------------------------------------------------
    "T-03": {
        "kind": "automated",
        "file": "test_classification_lookup.py",
        "tests": [
            "test_exact_live_lookup_hit",
        ],
    },
    # ------------------------------------------------------------------
    # T-05: Need structure (InChIKey-only miss → NEED_STRUCTURE)
    # ------------------------------------------------------------------
    "T-05": {
        "kind": "automated",
        "file": "test_batch_ingest.py",
        "tests": [
            "test_inchikey_only_miss_remote_disabled_sets_need_structure",
        ],
    },
    # ------------------------------------------------------------------
    # T-06: Mismatch guard (submitted structure InChIKey must match)
    # ------------------------------------------------------------------
    "T-06": {
        "kind": "automated",
        "file": "test_batch_ui.py",
        "tests": [
            "test_structure_form_mismatching_inchikey_shows_error",
            "test_api_structure_key_mismatch_leaves_need_structure",
        ],
    },
    # ------------------------------------------------------------------
    # T-07: Global dedupe (one RemoteTask per InChIKey across jobs)
    # ------------------------------------------------------------------
    "T-07": {
        "kind": "automated",
        "file": "test_batch_ingest.py",
        "tests": [
            "test_global_dedupe_one_active_remote_task",
        ],
    },
    # ------------------------------------------------------------------
    # T-08: Worker restart/stale-lock recovery
    # ------------------------------------------------------------------
    "T-08": {
        "kind": "automated",
        "file": "test_remote_queue.py",
        "tests": [
            "test_stale_lock_recovery_resets_to_ready",
        ],
    },
    # ------------------------------------------------------------------
    # T-09: Remote failure backoff/circuit breaker
    # ------------------------------------------------------------------
    "T-09": {
        "kind": "automated",
        "file": "test_remote_queue.py",
        "tests": [
            "test_retryable_error_schedules_retry_with_backoff",
            "test_circuit_opens_after_consecutive_failures",
        ],
    },
    # ------------------------------------------------------------------
    # T-10: Result fan-out (all waiting items updated)
    # ------------------------------------------------------------------
    "T-10": {
        "kind": "automated",
        "file": "test_remote_queue.py",
        "tests": [
            "test_fan_out_updates_all_waiting_items_across_jobs",
        ],
    },
    # ------------------------------------------------------------------
    # T-11: Export (dup/order/source/injection)
    # ------------------------------------------------------------------
    "T-11": {
        "kind": "automated",
        "file": "test_exports.py",
        "tests": [
            "test_row_order_preserved",
            "test_duplicate_rows_preserved",
            "test_requested_status_field_present",
            "test_csv_export_requested_classyfy_row_shape",
            "test_batch_item_and_export_keep_original_classification_after_live_supersede",
            "test_injection_escaping",
        ],
    },
    # ------------------------------------------------------------------
    # T-14: Backup restore (backup creates manifest + restore smoke test)
    # ------------------------------------------------------------------
    "T-14": {
        "kind": "automated",
        "file": "test_operations.py",
        "tests": [
            "test_backup_creates_manifest_and_event",
            "test_restore_smoke_test_logs_event",
        ],
    },
    # ------------------------------------------------------------------
    # T-15: External URL E2E (ngrok real URL — MANUAL)
    # ------------------------------------------------------------------
    "T-15": {
        "kind": "manual",
        "reason": (
            "Requires a live ngrok domain and real internet connectivity.  "
            "Cannot be automated in CI without an NGROK_AUTHTOKEN + NGROK_DOMAIN.  "
            "tests/e2e/test_ngrok_flow.py covers the same flow via Django test "
            "client; the real-URL path is exercised manually per the procedure in "
            "docs/acceptance-test-results.md."
        ),
        "docs_pointer": "docs/acceptance-test-results.md#t-15-external-url-e2e",
    },
    # ------------------------------------------------------------------
    # NFR-P01: p95 lookup < 200 ms
    # ------------------------------------------------------------------
    "NFR-P01": {
        "kind": "automated",
        "file": "test_classification_lookup.py",
        "tests": [
            "test_p95_latency_under_200ms",
        ],
    },
    # ------------------------------------------------------------------
    # NFR-P02: 10k local batch (MANUAL — no 10k-batch infra in this env)
    # ------------------------------------------------------------------
    "NFR-P02": {
        "kind": "manual",
        "reason": (
            "Requires a pre-loaded 10 000-row seed cache and a 10 000-row input "
            "file.  The seed dataset is not bundled in the test environment and "
            "the test would take > 60 s.  Tested manually using the FULL seed "
            "profile and a generated input file; results recorded in "
            "docs/acceptance-test-results.md."
        ),
        "docs_pointer": "docs/acceptance-test-results.md#nfr-p02-10k-local-batch",
    },
    # ------------------------------------------------------------------
    # NFR-P03: 100k streaming upload (MANUAL — no 100k infra)
    # ------------------------------------------------------------------
    "NFR-P03": {
        "kind": "manual",
        "reason": (
            "Requires generating a 100 000-row input file and verifying "
            "streaming upload behaviour with time and memory measurements.  "
            "Not suitable for automated CI.  Procedure documented in "
            "docs/acceptance-test-results.md."
        ),
        "docs_pointer": "docs/acceptance-test-results.md#nfr-p03-100k-streaming-upload",
    },
    # ------------------------------------------------------------------
    # NFR-P04: 10 concurrent browsers (MANUAL — no browser fleet)
    # ------------------------------------------------------------------
    "NFR-P04": {
        "kind": "manual",
        "reason": (
            "Requires a browser fleet (e.g. Playwright/Selenium) and a running "
            "application instance.  Not available in the CI environment.  "
            "Procedure documented in docs/acceptance-test-results.md."
        ),
        "docs_pointer": "docs/acceptance-test-results.md#nfr-p04-10-concurrent-browsers",
    },
    # ------------------------------------------------------------------
    # NFR-P05: Remote-outage local survival
    # ------------------------------------------------------------------
    "NFR-P05": {
        "kind": "automated",
        "file": "test_remote_queue.py",
        "tests": [
            "test_circuit_open_defers_task_local_lookup_survives",
        ],
    },
}


# ---------------------------------------------------------------------------
# Required IDs
# ---------------------------------------------------------------------------

REQUIRED_IDS = [
    "T-01", "T-02", "T-03", "T-05",
    "T-06", "T-07", "T-08", "T-09", "T-10",
    "T-11", "T-14", "T-15",
    "NFR-P01", "NFR-P02", "NFR-P03", "NFR-P04", "NFR-P05",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _function_exists_in_file(func_name: str, filepath: Path) -> bool:
    """Return True if a top-level or class-method ``def <func_name>(`` exists."""
    if not filepath.exists():
        return False
    source = filepath.read_text(encoding="utf-8")
    # Match both `def func_name(` and `    def func_name(` (class method)
    pattern = rf"def\s+{re.escape(func_name)}\s*\("
    return bool(re.search(pattern, source))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("acc_id", REQUIRED_IDS)
def test_acceptance_id_has_evidence(acc_id: str) -> None:
    """Every acceptance ID must have an entry in EVIDENCE."""
    assert acc_id in EVIDENCE, (
        f"Acceptance ID {acc_id!r} has no evidence entry in "
        "tests/test_acceptance_matrix.py EVIDENCE dict.  "
        "Add an entry or implement the test."
    )


@pytest.mark.parametrize("acc_id", REQUIRED_IDS)
def test_automated_tests_exist_on_disk(acc_id: str) -> None:
    """For AUTOMATED entries every referenced test function must exist on disk."""
    entry = EVIDENCE[acc_id]
    if entry["kind"] != "automated":
        pytest.skip(f"{acc_id} is a MANUAL entry — no function existence check needed")

    auto: AutomatedEntry = entry  # type: ignore[assignment]
    filepath = TESTS_DIR / auto["file"]
    assert filepath.exists(), (
        f"Test file {auto['file']!r} referenced by {acc_id} does not exist"
    )

    missing = [
        fn for fn in auto["tests"]
        if not _function_exists_in_file(fn, filepath)
    ]
    assert not missing, (
        f"Acceptance ID {acc_id} references test(s) not found in "
        f"{auto['file']}: {missing!r}"
    )


@pytest.mark.parametrize("acc_id", REQUIRED_IDS)
def test_manual_entries_have_reason_and_docs_pointer(acc_id: str) -> None:
    """MANUAL entries must have a non-empty reason and docs_pointer."""
    entry = EVIDENCE[acc_id]
    if entry["kind"] != "manual":
        pytest.skip(f"{acc_id} is an AUTOMATED entry")

    manual: ManualEntry = entry  # type: ignore[assignment]
    assert manual.get("reason", "").strip(), (
        f"MANUAL entry {acc_id!r} has an empty reason"
    )
    assert manual.get("docs_pointer", "").strip(), (
        f"MANUAL entry {acc_id!r} has an empty docs_pointer"
    )

    # The docs_pointer should point to an existing file (path before the # fragment)
    pointer = manual["docs_pointer"].split("#")[0].strip()
    if pointer:
        docs_path = REPO_ROOT / pointer
        assert docs_path.exists(), (
            f"MANUAL entry {acc_id!r} docs_pointer {pointer!r} does not exist on disk"
        )
