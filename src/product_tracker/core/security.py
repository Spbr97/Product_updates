"""API authentication.

A shared key, checked against the ``X-API-Key`` header. That is the right weight for what
this is: a single-user, self-hosted tool. Per-user accounts, sessions, and scopes belong
with multi-user support, and building them now would be structure without a requirement.

Three deliberate properties:

* **Off by default.** With no ``API_KEY`` set the API is open, which is correct for
  something bound to localhost. Setting the key turns enforcement on everywhere at once.
* **Constant-time comparison.** ``==`` on secrets leaks length and prefix through timing.
* **More than one key may be valid.** ``API_KEY`` accepts a comma-separated list, so a key
  can be rotated without downtime: add the new one, redeploy, move clients over, drop the
  old one. With a single key, rotation means a window where every client is broken -- which
  in practice means the key never gets rotated.
"""

from __future__ import annotations

import secrets

from .config import Settings


def valid_keys(settings: Settings) -> tuple[str, ...]:
    """Every key currently accepted. Empty when auth is disabled."""
    if settings.api_key is None:
        return ()
    raw = settings.api_key.get_secret_value()
    # Keys must not contain commas. Generated keys are hex or base64, so this costs
    # nothing and buys a list without a second setting.
    return tuple(key.strip() for key in raw.split(",") if key.strip())


def is_auth_enabled(settings: Settings) -> bool:
    return bool(valid_keys(settings))


def verify_api_key(settings: Settings, presented: str | None) -> bool:
    """Whether ``presented`` matches any configured key.

    Returns True when auth is disabled -- there is nothing to fail. A missing header with
    auth enabled is a failure, not an exemption.

    Every candidate is compared even after a match, so the time taken does not reveal
    which key matched or how many are configured.
    """
    keys = valid_keys(settings)
    if not keys:
        return True
    if not presented:
        return False

    matched = False
    for key in keys:
        if secrets.compare_digest(presented, key):
            matched = True
    return matched


def requires_key_for_reads(settings: Settings) -> bool:
    """Whether GET endpoints need a key too.

    Reads expose tracked URLs and price history, so allowing them anonymously is a
    deliberate convenience, not an oversight -- and it is switchable.
    """
    return is_auth_enabled(settings) and not settings.api_allow_anonymous_reads
