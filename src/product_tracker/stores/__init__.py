"""Store adapters.

Each adapter implements the ``StoreAdapter`` interface so the tracking engine never
imports a concrete store. See ``catalogue.py`` for the static store list.
"""

from .catalogue import GENERIC_SLUG, KNOWN_STORES, get_store_info

__all__ = ["GENERIC_SLUG", "KNOWN_STORES", "get_store_info"]
