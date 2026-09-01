"""Adapter registry -- the only place that knows which adapters exist.

The tracking engine asks the registry for an adapter and gets back a
:class:`~product_tracker.stores.base.StoreAdapter`. Registering a new store is a one-line
change here plus the adapter module; nothing else in the application is touched.
"""

from __future__ import annotations

from functools import lru_cache

from ..domain.errors import NoAdapterError
from ..domain.models import StoreInfo
from .amazon import AmazonAdapter
from .base import StoreAdapter
from .flipkart import FlipkartAdapter
from .generic import GenericStoreAdapter


class StoreRegistry:
    """Resolves a URL to the adapter that should handle it.

    Named adapters are consulted in order and the first match wins; the fallback adapter
    is always considered last, so adding one can never shadow a specific integration.
    """

    def __init__(self, adapters: list[StoreAdapter] | None = None) -> None:
        candidates = adapters if adapters is not None else _build_default_adapters()
        # Sort rather than trust the caller: a fallback placed first would swallow every
        # URL and silently disable every named adapter.
        self._adapters = sorted(candidates, key=lambda a: a.is_fallback)
        self._by_slug = {adapter.slug: adapter for adapter in self._adapters}

    @property
    def adapters(self) -> tuple[StoreAdapter, ...]:
        return tuple(self._adapters)

    def resolve(self, url: str) -> StoreAdapter:
        """Return the adapter for ``url``.

        Raises :class:`NoAdapterError` only when nothing matches, which with the generic
        fallback registered means the URL had no host at all.
        """
        for adapter in self._adapters:
            if adapter.can_handle_url(url):
                return adapter
        raise NoAdapterError(f"no store adapter accepts {url!r}")

    def get(self, slug: str) -> StoreAdapter:
        try:
            return self._by_slug[slug]
        except KeyError as exc:
            raise NoAdapterError(f"no adapter registered with slug {slug!r}") from exc

    def list_stores(self) -> list[StoreInfo]:
        return [adapter.store_info() for adapter in self._adapters]


def _build_default_adapters() -> list[StoreAdapter]:
    """Every adapter compiled into this build. Add new stores here."""
    return [AmazonAdapter(), FlipkartAdapter(), GenericStoreAdapter()]


@lru_cache(maxsize=1)
def default_registry() -> StoreRegistry:
    """The process-wide registry. Adapters are stateless, so one instance is enough."""
    return StoreRegistry()
