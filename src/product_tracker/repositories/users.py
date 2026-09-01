"""Data access for users and their subscriptions."""

from __future__ import annotations

from sqlalchemy import exists, func, select

from ..db.models import Product, Subscription, User
from .base import Repository


class UserRepository(Repository[User]):
    model = User

    def get_by_email(self, email: str) -> User | None:
        # Addresses are stored and compared lowercased; see ``user_service.normalise_email``.
        stmt = select(User).where(User.email == email.strip().casefold())
        return self.session.execute(stmt).scalar_one_or_none()

    def get_by_key_hash(self, key_hash: str) -> User | None:
        """Resolve an account from a presented key's hash.

        Indexed by the unique constraint on ``api_key_hash``, so this is one index lookup
        on every authenticated request.
        """
        stmt = select(User).where(User.api_key_hash == key_hash, User.is_active.is_(True))
        return self.session.execute(stmt).scalar_one_or_none()

    def list_all(self) -> list[User]:
        return list(self.session.execute(select(User).order_by(User.id)).scalars().all())

    def any_key_configured(self) -> bool:
        """Whether any account has a key at all.

        This is what turns API authentication on: creating the first user with a key locks
        the API down, which is a deliberate and security-relevant transition.

        Deliberately **not** filtered to active accounts. It reads as the natural thing to
        do and it is a hole: deactivating the only account that holds a key would make this
        return False, switch authentication off, and open the API to anonymous callers --
        the exact opposite of what disabling an account is meant to achieve. A deactivated
        key must stop authenticating (``get_by_key_hash`` enforces that) without also
        unlocking the door for everyone else.
        """
        stmt = select(exists().where(User.api_key_hash.is_not(None)))
        return bool(self.session.execute(stmt).scalar())

    def subscription_counts(self) -> dict[int, int]:
        """user_id -> how many listings they watch, in one query."""
        stmt = select(Subscription.user_id, func.count(Subscription.id)).group_by(
            Subscription.user_id
        )
        return {row[0]: int(row[1]) for row in self.session.execute(stmt)}


class SubscriptionRepository(Repository[Subscription]):
    model = Subscription

    def get(self, entity_id: int) -> Subscription | None:
        return self.session.get(Subscription, entity_id)

    def for_user_and_product(self, user_id: int, product_id: int) -> Subscription | None:
        stmt = select(Subscription).where(
            Subscription.user_id == user_id, Subscription.product_id == product_id
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def is_subscribed(self, user_id: int, product_id: int) -> bool:
        stmt = select(
            exists().where(
                Subscription.user_id == user_id, Subscription.product_id == product_id
            )
        )
        return bool(self.session.execute(stmt).scalar())

    def product_ids_for(self, user_id: int) -> list[int]:
        stmt = select(Subscription.product_id).where(Subscription.user_id == user_id)
        return list(self.session.execute(stmt).scalars().all())

    def subscriber_count(self, product_id: int) -> int:
        """How many users watch this listing.

        Used before deleting a product: the last subscriber leaving is the only point at
        which it is safe to stop tracking something, because the listing is shared.
        """
        stmt = (
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.product_id == product_id)
        )
        return int(self.session.execute(stmt).scalar_one())

    def products_for(self, user_id: int) -> list[Product]:
        stmt = (
            select(Product)
            .join(Subscription, Subscription.product_id == Product.id)
            .where(Subscription.user_id == user_id)
            .order_by(Product.id)
        )
        return list(self.session.execute(stmt).scalars().unique().all())

    def has_active_subscriber(self, product_id: int) -> bool:
        """Whether anyone still wants this listing checked.

        The scheduler runs one job per listing, however many people watch it, so the
        listing stays active while a single subscriber has not paused it.
        """
        stmt = select(
            exists().where(
                Subscription.product_id == product_id, Subscription.paused.is_(False)
            )
        )
        return bool(self.session.execute(stmt).scalar())
