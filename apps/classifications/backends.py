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
        from apps.classifications.selectors import (
            get_live_classification,
            get_seed_classification,
        )
        from apps.classifications.services import (
            _live_row_to_result,
            _seed_row_to_result,
        )

        row = get_live_classification(inchikey)
        if row is not None:
            return _live_row_to_result(row)

        row = get_seed_classification(inchikey)
        if row is not None:
            return _seed_row_to_result(row)

        return None

    def lookup_many(
        self,
        inchikeys: list[str],
    ) -> dict[str, ClassificationLookupResult]:
        from apps.classifications.selectors import (
            get_live_classifications,
            get_seed_classifications,
        )
        from apps.classifications.services import (
            _live_row_to_result,
            _seed_row_to_result,
        )

        live = get_live_classifications(inchikeys)
        out: dict[str, ClassificationLookupResult] = {
            key: _live_row_to_result(row) for key, row in live.items()
        }

        seed_needed = [key for key in inchikeys if key not in out]
        if seed_needed:
            seed = get_seed_classifications(seed_needed)
            for key, row in seed.items():
                out[key] = _seed_row_to_result(row)

        return out
