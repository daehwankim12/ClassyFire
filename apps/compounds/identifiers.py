"""RDKit-based identifier generation for ClassyFire normalization service.

Public interface
----------------
compute_identifiers(
    raw: str,
    input_type: DetectedInputType,
) -> tuple[str | None, str | None, str | None]

Returns ``(inchikey, canonical_smiles, inchi)``.

Contract (language-agnostic):
- Identifiers come ONLY from RDKit. No chemical-class prediction.
- Canonical SMILES: ``Chem.MolToSmiles(mol, isomericSmiles=True)`` — preserves
  stereochemistry and isotopes.
- Standard InChI and InChIKey via RDKit's InChI layer.
- NO salt stripping, NO neutralization, NO tautomer normalization.
- A bare InChIKey input has no structure available; returns (inchikey, None, None).
- An unparseable input returns (None, None, None).
- Never raises.

RDKit C++ warnings are suppressed via ``RDLogger.DisableLog``.
"""
from __future__ import annotations

from rdkit import Chem, RDLogger
from rdkit.Chem.inchi import InchiToInchiKey, MolFromInchi, MolToInchi

from apps.core.enums import DetectedInputType

RDLogger.DisableLog("rdApp.*")


def compute_identifiers(
    raw: str,
    input_type: DetectedInputType,
) -> tuple[str | None, str | None, str | None]:
    """Return ``(inchikey, canonical_smiles, inchi)`` for *raw*.

    Returns ``(inchikey, None, None)`` for a bare InChIKey (no structure known).
    Returns ``(None, None, None)`` on parse failure.
    """
    stripped = raw.strip()

    if input_type == DetectedInputType.INCHIKEY:
        return stripped, None, None

    mol: Chem.Mol | None = None

    if input_type == DetectedInputType.SMILES:
        mol = Chem.MolFromSmiles(stripped)
    elif input_type == DetectedInputType.INCHI:
        mol = MolFromInchi(stripped)
    elif input_type == DetectedInputType.SDF:
        # Single SDF block passed as a string.  Pass `raw` rather than
        # `stripped` so that a leading blank line (the V2000 title line) is
        # preserved — stripping it shifts all line numbers and breaks RDKit's
        # V2000 parser which expects the counts line at position 4.
        try:
            suppl = Chem.SDMolSupplier()
            suppl.SetData(raw, removeHs=False, sanitize=True)
            for m in suppl:
                if m is not None:
                    mol = m
                    break
        except Exception:
            mol = None

    if mol is None:
        return None, None, None

    try:
        canonical_smiles = Chem.MolToSmiles(mol, isomericSmiles=True)
        inchi = MolToInchi(mol)
        inchikey = InchiToInchiKey(inchi) if inchi else None
    except Exception:
        return None, None, None

    return inchikey, canonical_smiles, inchi
