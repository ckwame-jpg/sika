"""Small query helpers shared across services.

SQLite caps the number of host parameters in a single statement (999
before SQLite 3.32, 32766 after). A market-refresh cycle can feed
thousands of tickers / market ids into an ``IN (...)`` filter, which
overflows that cap with ``sqlite3.OperationalError: too many SQL
variables`` and crash-loops the refresh job. Route any variable-length
``IN`` list through :func:`chunked` so each statement stays under the
floor. Postgres has a far higher limit, so chunking is a harmless no-op
overhead there.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TypeVar

_T = TypeVar("_T")

# Conservative floor: well under SQLite's historical 999-variable limit,
# leaving headroom for the statement's other bound parameters.
IN_CHUNK_SIZE = 500


def chunked(values: Iterable[_T], size: int = IN_CHUNK_SIZE) -> Iterator[list[_T]]:
    """Yield ``values`` as lists of at most ``size`` items.

    An empty input yields nothing, so ``for chunk in chunked(ids)`` is a
    safe drop-in for ``if ids: ... in_(ids)``.
    """
    if size <= 0:
        raise ValueError(f"size must be > 0, got {size}")
    items = list(values)
    for start in range(0, len(items), size):
        yield items[start : start + size]
