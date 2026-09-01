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

import hashlib
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


#: Prefix on generated keys. Makes one recognisable in a log or a config file, and makes
#: leaked-secret scanners able to spot it.
API_KEY_PREFIX = "pt_"

#: 32 bytes of entropy. Long enough that guessing is not a threat model.
_KEY_BYTES = 32


def generate_api_key() -> str:
    """A fresh key. Shown to its owner once and never recoverable afterwards."""
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(_KEY_BYTES)}"


def hash_api_key(key: str) -> str:
    """The value stored for a key.

    A plain SHA-256, deliberately, not bcrypt or argon2. Those exist to make *guessing*
    expensive, which matters for passwords because people choose weak ones. An API key here
    is 256 bits from ``secrets``, so there is nothing to guess and a slow hash would only
    add latency to every single authenticated request.

    What this does buy is that a database dump, a backup, or a support query contains no
    usable credential.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
