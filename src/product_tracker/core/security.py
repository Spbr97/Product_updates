"""API authentication.

A single shared key, checked against the ``X-API-Key`` header. That is the right weight
for what this is: a single-user, self-hosted tool. Per-user accounts, sessions, and scopes
belong with multi-user support, and building them now would be structure without a
requirement.

Two deliberate properties:

* **Off by default.** With no ``API_KEY`` set the API is open, which is correct for
  something bound to localhost. Setting the key turns enforcement on everywhere at once.
* **Constant-time comparison.** ``==`` on secrets leaks length and prefix through timing.
"""

from __future__ import annotations

import secrets

from .config import Settings


def is_auth_enabled(settings: Settings) -> bool:
    return settings.api_key is not None


def verify_api_key(settings: Settings, presented: str | None) -> bool:
    """Whether ``presented`` matches the configured key.

    Returns True when auth is disabled -- there is nothing to fail. A missing header with
    auth enabled is a failure, not an exemption.
    """
    if settings.api_key is None:
        return True
    if not presented:
        return False
    return secrets.compare_digest(presented, settings.api_key.get_secret_value())


def requires_key_for_reads(settings: Settings) -> bool:
    """Whether GET endpoints need a key too.

    Reads expose tracked URLs and price history, so allowing them anonymously is a
    deliberate convenience, not an oversight -- and it is switchable.
    """
    return is_auth_enabled(settings) and not settings.api_allow_anonymous_reads
