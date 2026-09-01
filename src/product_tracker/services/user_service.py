"""Accounts, API keys, and who a request belongs to.

The model here is that **listings are shared and intent is private**. A product row and its
price history are global; subscriptions, groups, and alert rules belong to a user. Two
people watching the same Flipkart URL therefore cost that retailer one fetch, not two, and
each still keeps their own alerts.

Authentication is off until somebody turns it on. With no ``API_KEY`` configured and no
user holding a key, every request resolves to the default account -- which is right for a
tool bound to localhost, and keeps single-user installs working unchanged. Creating the
first user with a key switches enforcement on everywhere at once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.security import generate_api_key, hash_api_key, verify_api_key
from ..db.models import Subscription, User
from ..domain.errors import DuplicateError, NotFoundError, ValidationError
from ..repositories.products import ProductRepository
from ..repositories.users import SubscriptionRepository, UserRepository

#: The account created by migration 0007, which owns everything that predates multi-user.
DEFAULT_USER_EMAIL = "local@localhost"

# Deliberately permissive. This is an identifier for a self-hosted tool, not a signup form,
# and rejecting an address someone actually uses is worse than accepting an odd one.
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True, slots=True)
class CreatedUser:
    """A new account and its key.

    The key exists in this object and nowhere else -- only its hash is stored -- so this is
    the single moment it can be shown to anyone.
    """

    user: User
    api_key: str


def normalise_email(email: str) -> str:
    return email.strip().casefold()


def create_user(
    session: Session,
    *,
    email: str,
    name: str | None = None,
    is_admin: bool = False,
    with_key: bool = True,
) -> CreatedUser:
    """Create an account, and by default issue it a key."""
    address = normalise_email(email)
    if not _EMAIL_PATTERN.match(address):
        raise ValidationError(f"{email!r} does not look like an email address")

    repo = UserRepository(session)
    if repo.get_by_email(address) is not None:
        raise DuplicateError("user", address)

    key = generate_api_key() if with_key else ""
    user = repo.add(
        User(
            email=address,
            name=(name or None),
            api_key_hash=hash_api_key(key) if with_key else None,
            is_active=True,
            is_admin=is_admin,
        )
    )
    return CreatedUser(user=user, api_key=key)


def get_user(session: Session, identifier: int | str) -> User:
    """Look a user up by id or email."""
    repo = UserRepository(session)
    found = (
        repo.get(identifier) if isinstance(identifier, int) else repo.get_by_email(identifier)
    )
    if found is None:
        raise NotFoundError("user", identifier)
    return found


def rotate_key(session: Session, identifier: int | str) -> CreatedUser:
    """Issue a new key and invalidate the old one immediately.

    There is no overlap window: the previous key stops working the moment this returns. For
    a gradual rollover, the deployment-wide ``API_KEY`` setting accepts a comma-separated
    list; per-user keys are individual credentials and a compromised one should die at once.
    """
    user = get_user(session, identifier)
    key = generate_api_key()
    user.api_key_hash = hash_api_key(key)
    session.flush()
    return CreatedUser(user=user, api_key=key)


def set_active(session: Session, identifier: int | str, *, active: bool) -> User:
    """Enable or disable an account without deleting anything it owns."""
    user = get_user(session, identifier)
    user.is_active = active
    session.flush()
    return user


def delete_user(session: Session, identifier: int | str) -> None:
    """Remove an account, its subscriptions, its groups and its alerts.

    Tracked listings and price history are untouched: they are shared, and may well be
    watched by somebody else. A listing nobody watches simply stops having subscribers --
    ``products remove`` is what stops tracking it.
    """
    UserRepository(session).delete(get_user(session, identifier))


def default_user(session: Session) -> User:
    """The account requests fall back to when authentication is not enabled."""
    user = UserRepository(session).get_by_email(DEFAULT_USER_EMAIL)
    if user is None:
        # Only reachable if someone deleted the seeded account; recreate rather than fail.
        user = UserRepository(session).add(
            User(email=DEFAULT_USER_EMAIL, name="Local", is_active=True, is_admin=True)
        )
    return user


def auth_enabled(session: Session, settings: Settings) -> bool:
    """Whether a credential is required at all.

    True once either a deployment-wide ``API_KEY`` is set or any active user holds a key.
    """
    if settings.api_key is not None:
        return True
    return UserRepository(session).any_key_configured()


def resolve_user(session: Session, settings: Settings, presented: str | None) -> User | None:
    """The account a presented key belongs to, or None when it authenticates nobody.

    Order matters. The deployment-wide ``API_KEY`` is checked first and maps to the default
    account, so an existing single-user install keeps working exactly as before after
    per-user keys arrive.
    """
    if not auth_enabled(session, settings):
        return default_user(session)

    if not presented:
        return None

    # A deployment-wide key authenticates as the default account.
    if settings.api_key is not None and verify_api_key(settings, presented):
        return default_user(session)

    return UserRepository(session).get_by_key_hash(hash_api_key(presented))


# --- Subscriptions ----------------------------------------------------------------------


def subscribe(session: Session, user_id: int, product_id: int) -> Subscription:
    """Start watching a listing. Watching it twice is a no-op, not an error."""
    if ProductRepository(session).get(product_id) is None:
        raise NotFoundError("product", product_id)

    repo = SubscriptionRepository(session)
    existing = repo.for_user_and_product(user_id, product_id)
    if existing is not None:
        return existing
    return repo.add(Subscription(user_id=user_id, product_id=product_id))


def assert_subscribed(session: Session, user_id: int, product_id: int) -> None:
    """Refuse a listing this user does not watch, as though it did not exist.

    Product ids are sequential, so without this an authenticated user can walk 1, 2, 3...
    and read every URL, price and history row anyone else is tracking. NotFoundError rather
    than a permission error, for the same reason groups use it: a 403 confirms the id is
    real and turns the walk into an enumeration oracle.
    """
    if not SubscriptionRepository(session).is_subscribed(user_id, product_id):
        raise NotFoundError("Product", product_id)


def unsubscribe(session: Session, user_id: int, product_id: int) -> bool:
    """Stop watching a listing. Returns whether there was anything to remove.

    The listing itself keeps being tracked while anyone else still watches it, and its
    history is never touched.
    """
    repo = SubscriptionRepository(session)
    existing = repo.for_user_and_product(user_id, product_id)
    if existing is None:
        return False
    repo.delete(existing)
    return True
