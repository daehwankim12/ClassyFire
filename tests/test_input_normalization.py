"""Tests for the ClassyFire input normalization pipeline.

All expected InChIKey / canonical SMILES / InChI values were computed with the
project venv's RDKit (2026.03.3) and are hard-coded for determinism (T-02).

Test coverage:
- Detection: InChIKey, InChI, SMILES, empty input
- normalize_row: mixed types, type_override, external_id propagation
- Invalid row isolation: bad SMILES → error row, others unaffected
- Stereo preservation: L-Ala vs achiral Ala differ in InChIKey
- Isotope preservation: [2H]-isobutane InChIKey differs from protio form
- Salt preservation: CC(=O)O.[Na+] InChIKey differs from acetic acid alone
- Streaming parsers: TXT, CSV, TSV, SDF
- Row index preservation
- External ID preservation
- No class-prediction API in any imported module
"""
from __future__ import annotations

import importlib
import inspect
import io
from typing import ClassVar

import pytest

from apps.compounds.detection import detect_input_type
from apps.compounds.parsers import parse_csv_tsv, parse_sdf, parse_txt
from apps.compounds.validators import normalize_row
from apps.core.enums import DetectedInputType

# ---------------------------------------------------------------------------
# Fixed RDKit identity fixtures (computed with venv RDKit 2026.03.3)
# ---------------------------------------------------------------------------

ASPIRIN_SMILES = "CC(=O)Oc1ccccc1C(=O)O"
ASPIRIN_CANONICAL = "CC(=O)Oc1ccccc1C(=O)O"
ASPIRIN_INCHIKEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
ASPIRIN_INCHI = "InChI=1S/C9H8O4/c1-6(10)13-8-5-3-2-4-7(8)9(11)12/h2-5H,1H3,(H,11,12)"

L_ALA_SMILES = "N[C@@H](C)C(=O)O"
L_ALA_CANONICAL = "C[C@H](N)C(=O)O"
L_ALA_INCHIKEY = "QNAYBMKLOCPYGJ-REOHCLBHSA-N"

ACH_ALA_SMILES = "NC(C)C(=O)O"
ACH_ALA_CANONICAL = "CC(N)C(=O)O"
ACH_ALA_INCHIKEY = "QNAYBMKLOCPYGJ-UHFFFAOYSA-N"

ISOTOPE_SMILES = "[2H]C(C)(C)C"
ISOTOPE_INCHIKEY = "NNPPMTNAJDCUHE-QYKNYGDISA-N"
PROTIO_ISOBUTANE_SMILES = "CC(C)C"

SALT_SMILES = "CC(=O)O.[Na+]"
SALT_INCHIKEY = "VMHLLURERBWHNL-UHFFFAOYSA-N"
ACOH_SMILES = "CC(=O)O"
ACOH_INCHIKEY = "QTBSBXVTEAMEQO-UHFFFAOYSA-N"

KNOWN_INCHIKEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"


# ---------------------------------------------------------------------------
# Detection tests
# ---------------------------------------------------------------------------

class TestDetection:
    def test_detects_inchikey(self):
        assert detect_input_type(KNOWN_INCHIKEY) == DetectedInputType.INCHIKEY

    def test_detects_inchi(self):
        assert detect_input_type(ASPIRIN_INCHI) == DetectedInputType.INCHI

    def test_detects_smiles(self):
        assert detect_input_type(ASPIRIN_SMILES) == DetectedInputType.SMILES

    def test_detects_smiles_with_whitespace(self):
        assert detect_input_type(f"  {ASPIRIN_SMILES}  ") == DetectedInputType.SMILES

    def test_empty_string_is_unknown(self):
        assert detect_input_type("") == DetectedInputType.UNKNOWN

    def test_whitespace_only_is_unknown(self):
        assert detect_input_type("   ") == DetectedInputType.UNKNOWN

    def test_partial_inchikey_is_smiles(self):
        # Only 14+10+1 = full match; partial length → SMILES
        assert detect_input_type("BSYNRYMUTXBXSQ-UHFFFAOYSA") == DetectedInputType.SMILES

    def test_lowercase_inchikey_is_smiles(self):
        # InChIKey must be uppercase
        assert detect_input_type("bsynrymutxbxsq-uhfffaoysa-n") == DetectedInputType.SMILES


# ---------------------------------------------------------------------------
# normalize_row — happy paths
# ---------------------------------------------------------------------------

class TestNormalizeRow:
    def test_smiles_aspirin(self):
        row = normalize_row(ASPIRIN_SMILES)
        assert row.detected_type == DetectedInputType.SMILES
        assert row.inchikey == ASPIRIN_INCHIKEY
        assert row.canonical_smiles == ASPIRIN_CANONICAL
        assert row.inchi == ASPIRIN_INCHI
        assert row.original_input == ASPIRIN_SMILES
        assert row.error is None

    def test_inchi_aspirin(self):
        row = normalize_row(ASPIRIN_INCHI)
        assert row.detected_type == DetectedInputType.INCHI
        assert row.inchikey == ASPIRIN_INCHIKEY
        assert row.canonical_smiles == ASPIRIN_CANONICAL
        assert row.error is None

    def test_inchikey_bare(self):
        row = normalize_row(KNOWN_INCHIKEY)
        assert row.detected_type == DetectedInputType.INCHIKEY
        assert row.inchikey == KNOWN_INCHIKEY
        assert row.canonical_smiles is None
        assert row.inchi is None
        assert row.error is None

    def test_external_id_propagated(self):
        row = normalize_row(ASPIRIN_SMILES, external_id="CPD-001")
        assert row.external_id == "CPD-001"

    def test_external_id_none_by_default(self):
        row = normalize_row(ASPIRIN_SMILES)
        assert row.external_id is None

    def test_type_override_forces_inchi(self):
        # If we pass an InChI but force SMILES, it should fail (parse error).
        row = normalize_row(ASPIRIN_INCHI, type_override=DetectedInputType.SMILES)
        assert row.error is not None
        assert row.inchikey is None

    def test_type_override_inchikey(self):
        # Force inchikey type → no structure, just returns the key
        row = normalize_row(KNOWN_INCHIKEY, type_override=DetectedInputType.INCHIKEY)
        assert row.inchikey == KNOWN_INCHIKEY
        assert row.canonical_smiles is None
        assert row.error is None


# ---------------------------------------------------------------------------
# Stereo preservation
# ---------------------------------------------------------------------------

class TestStereoPreservation:
    def test_chiral_inchikey_differs_from_achiral(self):
        l_ala = normalize_row(L_ALA_SMILES)
        ach_ala = normalize_row(ACH_ALA_SMILES)
        assert l_ala.inchikey == L_ALA_INCHIKEY
        assert ach_ala.inchikey == ACH_ALA_INCHIKEY
        assert l_ala.inchikey != ach_ala.inchikey

    def test_chiral_canonical_smiles_preserves_stereo(self):
        row = normalize_row(L_ALA_SMILES)
        assert row.canonical_smiles == L_ALA_CANONICAL
        # Must contain stereo mark
        assert "@" in row.canonical_smiles


# ---------------------------------------------------------------------------
# Isotope preservation
# ---------------------------------------------------------------------------

class TestIsotopePreservation:
    def test_deuterated_differs_from_protio(self):
        from apps.compounds.identifiers import compute_identifiers
        protio_ik, _, _ = compute_identifiers(PROTIO_ISOBUTANE_SMILES, DetectedInputType.SMILES)
        row = normalize_row(ISOTOPE_SMILES)
        assert row.inchikey == ISOTOPE_INCHIKEY
        assert row.inchikey != protio_ik

    def test_deuterated_inchikey_fixed(self):
        row = normalize_row(ISOTOPE_SMILES)
        assert row.inchikey == ISOTOPE_INCHIKEY


# ---------------------------------------------------------------------------
# Salt preservation (no stripping by default)
# ---------------------------------------------------------------------------

class TestSaltPreservation:
    def test_salt_inchikey_differs_from_acid_alone(self):
        salt_row = normalize_row(SALT_SMILES)
        acid_row = normalize_row(ACOH_SMILES)
        assert salt_row.inchikey == SALT_INCHIKEY
        assert acid_row.inchikey == ACOH_INCHIKEY
        assert salt_row.inchikey != acid_row.inchikey

    def test_salt_canonical_smiles_contains_both_components(self):
        row = normalize_row(SALT_SMILES)
        assert row.canonical_smiles is not None
        assert "." in row.canonical_smiles


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------

class TestErrorIsolation:
    def test_invalid_smiles_returns_error_row(self):
        row = normalize_row("not_a_valid_smiles_!!!")
        assert row.error is not None
        assert row.inchikey is None
        assert row.detected_type == DetectedInputType.SMILES

    def test_invalid_row_does_not_affect_valid_rows(self):
        inputs = [ASPIRIN_SMILES, "INVALID!!!", L_ALA_SMILES]
        rows = [normalize_row(s) for s in inputs]
        assert rows[0].inchikey == ASPIRIN_INCHIKEY
        assert rows[1].error is not None
        assert rows[2].inchikey == L_ALA_INCHIKEY

    def test_empty_raw_returns_unknown_with_error(self):
        row = normalize_row("")
        assert row.detected_type == DetectedInputType.UNKNOWN

    def test_original_input_always_preserved(self):
        bad = "INVALID!!!"
        row = normalize_row(bad)
        assert row.original_input == bad


# ---------------------------------------------------------------------------
# Mixed batch (InChIKey + SMILES + InChI)
# ---------------------------------------------------------------------------

class TestMixedBatch:
    def test_mixed_line_types(self):
        inputs = [KNOWN_INCHIKEY, ASPIRIN_SMILES, ASPIRIN_INCHI]
        expected_types = [
            DetectedInputType.INCHIKEY,
            DetectedInputType.SMILES,
            DetectedInputType.INCHI,
        ]
        for raw, expected_type in zip(inputs, expected_types, strict=True):
            row = normalize_row(raw)
            assert row.detected_type == expected_type


# ---------------------------------------------------------------------------
# Streaming parser: TXT
# ---------------------------------------------------------------------------

class TestParseTxt:
    def _make_stream(self, text: str) -> io.BytesIO:
        return io.BytesIO(text.encode("utf-8"))

    def test_yields_all_lines(self):
        stream = self._make_stream(f"{ASPIRIN_SMILES}\n{L_ALA_SMILES}\n{KNOWN_INCHIKEY}\n")
        rows = list(parse_txt(stream))
        assert len(rows) == 3
        assert rows[0]["raw"] == ASPIRIN_SMILES
        assert rows[1]["raw"] == L_ALA_SMILES
        assert rows[2]["raw"] == KNOWN_INCHIKEY

    def test_row_index_preserved(self):
        stream = self._make_stream(f"{ASPIRIN_SMILES}\n{L_ALA_SMILES}\n")
        rows = list(parse_txt(stream))
        assert rows[0]["row_index"] == 0
        assert rows[1]["row_index"] == 1

    def test_blank_lines_skipped(self):
        stream = self._make_stream(f"{ASPIRIN_SMILES}\n\n\n{L_ALA_SMILES}\n")
        rows = list(parse_txt(stream))
        assert len(rows) == 2

    def test_generator_does_not_load_all(self):
        stream = self._make_stream(f"{ASPIRIN_SMILES}\n{L_ALA_SMILES}\n")
        gen = parse_txt(stream)
        first = next(gen)
        assert first["raw"] == ASPIRIN_SMILES


# ---------------------------------------------------------------------------
# Streaming parser: CSV / TSV
# ---------------------------------------------------------------------------

class TestParseCsvTsv:
    def _csv_stream(self, text: str) -> io.BytesIO:
        return io.BytesIO(text.encode("utf-8"))

    def test_csv_basic(self):
        csv_text = "smiles,name\nCC(=O)O,acetic acid\nCC(=O)Oc1ccccc1C(=O)O,aspirin\n"
        rows = list(parse_csv_tsv(self._csv_stream(csv_text), smiles_col="smiles"))
        assert len(rows) == 2
        assert rows[0]["raw"] == "CC(=O)O"
        assert rows[1]["raw"] == "CC(=O)Oc1ccccc1C(=O)O"

    def test_csv_external_id(self):
        csv_text = "smiles,id\nCC(=O)O,CPD-001\n"
        rows = list(parse_csv_tsv(self._csv_stream(csv_text), smiles_col="smiles", id_col="id"))
        assert rows[0]["external_id"] == "CPD-001"

    def test_csv_row_index(self):
        csv_text = "smiles\nCC(=O)O\nCC(N)C(=O)O\n"
        rows = list(parse_csv_tsv(self._csv_stream(csv_text)))
        assert rows[0]["row_index"] == 0
        assert rows[1]["row_index"] == 1

    def test_tsv_auto_detect(self):
        tsv_text = "smiles\tid\nCC(=O)O\tCPD-001\n"
        rows = list(parse_csv_tsv(self._csv_stream(tsv_text), smiles_col="smiles", id_col="id"))
        assert rows[0]["raw"] == "CC(=O)O"
        assert rows[0]["external_id"] == "CPD-001"

    def test_explicit_tsv_delimiter(self):
        tsv_text = "smiles\tid\nCC(=O)O\tCPD-001\n"
        rows = list(
            parse_csv_tsv(self._csv_stream(tsv_text), smiles_col="smiles", id_col="id", delimiter="\t")
        )
        assert rows[0]["raw"] == "CC(=O)O"

    def test_csv_integer_col_index(self):
        csv_text = "smiles,id\nCC(=O)O,CPD-001\n"
        rows = list(parse_csv_tsv(self._csv_stream(csv_text), smiles_col=0, id_col=1))
        assert rows[0]["raw"] == "CC(=O)O"
        assert rows[0]["external_id"] == "CPD-001"

    def test_csv_default_first_col(self):
        csv_text = "smiles,name\nCC(=O)O,acetic acid\n"
        rows = list(parse_csv_tsv(self._csv_stream(csv_text)))
        assert rows[0]["raw"] == "CC(=O)O"


# ---------------------------------------------------------------------------
# Streaming parser: SDF
# ---------------------------------------------------------------------------

_SDF_RECORD = """\

  Mrv2211 01012300002D

  3  2  0  0  0  0            999 V2000
    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.2990    0.7500    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    2.5981    0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0  0  0  0
  2  3  1  0  0  0  0
M  END
> <EXTERNAL_ID>
CPD-001

$$$$
"""

_SDF_TWO_RECORDS = _SDF_RECORD + """\

  Mrv2211 01012300002D

  3  2  0  0  0  0            999 V2000
    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.2990    0.7500    0.0000 N   0  0  0  0  0  0  0  0  0  0  0  0
    2.5981    0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0  0  0  0
  2  3  1  0  0  0  0
M  END
> <EXTERNAL_ID>
CPD-002

$$$$
"""


class TestParseSdf:
    def _stream(self, text: str) -> io.BytesIO:
        return io.BytesIO(text.encode("utf-8"))

    def test_yields_one_record(self):
        rows = list(parse_sdf(self._stream(_SDF_RECORD)))
        assert len(rows) == 1

    def test_external_id_from_sdf_property(self):
        rows = list(parse_sdf(self._stream(_SDF_RECORD)))
        assert rows[0].get("external_id") == "CPD-001"

    def test_row_index_preserved(self):
        rows = list(parse_sdf(self._stream(_SDF_TWO_RECORDS)))
        assert len(rows) == 2
        assert rows[0]["row_index"] == 0
        assert rows[1]["row_index"] == 1

    def test_external_id_second_record(self):
        rows = list(parse_sdf(self._stream(_SDF_TWO_RECORDS)))
        assert rows[1].get("external_id") == "CPD-002"

    def test_raw_block_nonempty(self):
        rows = list(parse_sdf(self._stream(_SDF_RECORD)))
        assert len(rows[0]["raw"]) > 0


# ---------------------------------------------------------------------------
# No class-prediction API guard
# ---------------------------------------------------------------------------

class TestNoPredictionAPI:
    """Ensure none of the normalization modules import any classifier."""

    # Terms that would indicate a classifier API being called (not just named).
    # We check for import of classifier modules or API calls, not mere mentions
    # of the project name in docstrings.
    _FORBIDDEN_PATTERNS: ClassVar[list[str]] = [
        "import classyfire",
        "classyfire_api",
        "ClassyFireAPI",
        "from apps.chemont",
        "from apps.classifications",
    ]

    @pytest.mark.parametrize("module_name", [
        "apps.compounds.detection",
        "apps.compounds.identifiers",
        "apps.compounds.parsers",
        "apps.compounds.validators",
    ])
    def test_no_classifier_import(self, module_name: str):
        mod = importlib.import_module(module_name)
        source = inspect.getsource(mod)
        for forbidden in self._FORBIDDEN_PATTERNS:
            assert forbidden.lower() not in source.lower(), (
                f"{module_name} must not contain '{forbidden}'"
            )
