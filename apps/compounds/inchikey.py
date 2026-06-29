"""Pure InChIKey string helpers — standard/non-standard flag and skeleton block.

An InChIKey is ``AAAAAAAAAAAAAA-BBBBBBBBFV-P``:
  * block 1 (14 chars): the connectivity/skeleton hash — SHARED by a compound's
    standard and non-standard InChIKeys;
  * block 2 (10 chars): 8 hash chars + a flag char + a version char. The flag
    char (0-indexed position 23 of the full key) is ``S`` for a STANDARD InChIKey
    (derived from standard InChI, ``InChI=1S/...``) and ``N`` for a non-standard
    one.

InChIKey is a one-way hash, so a non-standard key cannot be converted to its
standard form without the structure. These deterministic, DB-free, RDKit-free
string ops let callers (a) detect a non-standard key and (b) compare two keys at
the connectivity-skeleton level. A Go/Rust worker can reproduce them.
"""
from __future__ import annotations

_STANDARD_FLAG_INDEX = 23


def is_inchikey(value: str | None) -> bool:
    """Loose shape check: 27 chars with hyphens at positions 14 and 25."""
    return (
        isinstance(value, str)
        and len(value) == 27
        and value[14] == "-"
        and value[25] == "-"
    )


def is_standard_inchikey(value: str | None) -> bool:
    """True iff a well-formed InChIKey is STANDARD (flag char ``S``).

    A value that is not a well-formed InChIKey is treated as standard (``True``)
    so callers need not special-case non-key strings.
    """
    if not is_inchikey(value):
        return True
    return value[_STANDARD_FLAG_INDEX] == "S"


def inchikey_skeleton(value: str) -> str:
    """The first (connectivity) block — shared across a compound's standard and
    non-standard InChIKeys."""
    return value.split("-", 1)[0]


def same_inchikey_skeleton(a: str | None, b: str | None) -> bool:
    """True iff both are non-empty and share the connectivity (first) block."""
    return bool(a) and bool(b) and inchikey_skeleton(a) == inchikey_skeleton(b)
