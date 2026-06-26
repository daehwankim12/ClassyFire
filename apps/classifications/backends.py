from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from apps.classifications.services import ClassificationLookupResult


class ClassificationLookupBackend(Protocol):
    """Swappable current-classification lookup backend.

    The default implementation reads PostgreSQL. Alternative implementations
    such as LMDB or RocksDB must preserve exact 27-character InChIKey semantics
    and return the same public ``ClassificationLookupResult`` objects.
    """

    def lookup_one(self, inchikey: str) -> ClassificationLookupResult | None:
        """Return the current classification for one exact InChIKey."""

    def lookup_many(
        self,
        inchikeys: list[str],
    ) -> dict[str, ClassificationLookupResult]:
        """Return current classifications for unique exact InChIKeys."""


class PostgresClassificationLookupBackend:
    """Current lookup backend backed by the existing PostgreSQL tables/views."""

    def lookup_one(self, inchikey: str) -> ClassificationLookupResult | None:
        from apps.classifications.selectors import get_current_classification
        from apps.classifications.services import _current_row_to_result

        row = get_current_classification(inchikey)
        if row is None:
            return None
        return _current_row_to_result(row)

    def lookup_many(
        self,
        inchikeys: list[str],
    ) -> dict[str, ClassificationLookupResult]:
        from apps.classifications.selectors import get_current_classifications
        from apps.classifications.services import _current_row_to_result

        rows = get_current_classifications(inchikeys)
        return {key: _current_row_to_result(row) for key, row in rows.items()}
