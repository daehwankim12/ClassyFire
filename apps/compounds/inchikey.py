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

from typing import TypeGuard

_STANDARD_FLAG_INDEX = 23
# The flag char (index 23) is "S" for a standard InChIKey and "N" for a
# non-standard one; no other value is valid in that position.
_FLAG_CHARS = frozenset({"S", "N"})


def is_inchikey(value: str | None) -> TypeGuard[str]:
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
    if not a or not b:
        return False
    return inchikey_skeleton(a) == inchikey_skeleton(b)


def same_inchikey_modulo_flag(a: str | None, b: str | None) -> bool:
    """True iff two well-formed InChIKeys are identical EXCEPT for the
    standard/non-standard flag char (index 23, ``S`` vs ``N``).

    Every other character must be equal: the connectivity block (0-13), the
    block-2 stereo/isotope/charge hash (15-22), the InChI version char (24), AND
    the protonation block (26). Anything other than the flag differing means we
    cannot assume the same molecule, so the caller must fall back to
    NEED_STRUCTURE rather than adopt. This is stricter than
    :func:`same_inchikey_skeleton` (connectivity only): two stereoisomers share
    the connectivity block but differ in the block-2 hash and are NOT matched,
    while the same molecule's standard and non-standard keys (differing only in
    the flag) DO match. The flag char itself must be a valid ``S``/``N`` in BOTH
    keys — an arbitrary char there is a malformed key, not a std/non-std variant.
    RDKit-free; reproducible by a Go/Rust worker.
    """
    if not is_inchikey(a) or not is_inchikey(b):
        return False
    if (
        a[_STANDARD_FLAG_INDEX] not in _FLAG_CHARS
        or b[_STANDARD_FLAG_INDEX] not in _FLAG_CHARS
    ):
        return False
    return (
        a[:_STANDARD_FLAG_INDEX] == b[:_STANDARD_FLAG_INDEX]
        and a[_STANDARD_FLAG_INDEX + 1 :] == b[_STANDARD_FLAG_INDEX + 1 :]
    )
