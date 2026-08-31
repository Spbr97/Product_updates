"""Store repository."""

from __future__ import annotations

from sqlalchemy import select

from ..db.models import Store
from ..domain.models import StoreInfo
from .base import Repository


class StoreRepository(Repository[Store]):
    model = Store

    def get_by_slug(self, slug: str) -> Store | None:
        return self.session.execute(select(Store).where(Store.slug == slug)).scalar_one_or_none()

    def list_all(self, *, enabled_only: bool = False) -> list[Store]:
        stmt = select(Store).order_by(Store.name)
        if enabled_only:
            stmt = stmt.where(Store.enabled.is_(True))
        return list(self.session.execute(stmt).scalars())

    def sync_from_registry(self, stores: list[StoreInfo]) -> tuple[int, int]:
        """Reconcile the table with the adapters compiled into the application.

        Returns ``(created, updated)``. Rows are never deleted here: a store may be
        retired from code while products still reference it, and dropping the row would
        break that foreign key. Retire by disabling instead.
        """
        created = updated = 0
        for info in stores:
            existing = self.get_by_slug(info.slug)
            if existing is None:
                self.session.add(
                    Store(
                        slug=info.slug,
                        name=info.display_name,
                        domains=list(info.domains),
                        adapter_key=info.adapter_key,
                        enabled=True,
                    )
                )
                created += 1
                continue

            changes = {
                "name": info.display_name,
                "domains": list(info.domains),
                "adapter_key": info.adapter_key,
            }
            if any(getattr(existing, key) != value for key, value in changes.items()):
                for key, value in changes.items():
                    setattr(existing, key, value)
                updated += 1

        self.session.flush()
        return created, updated
