"""Refuse any connection a test makes to somewhere that is not this machine.

The rule this enforces is that a normal test run must not depend on Amazon, Flipkart or
any other retailer being online. Grepping for stubs cannot prove that: it shows which
files *mention* a stub, not whether some path slips out to the internet anyway. Two unit
tests did exactly that -- they fetched a real robots.txt, tolerated the failure, and
passed either way, so nothing ever complained.

The two halves are both load-bearing. Blocking the socket stops the request. Reporting it
afterwards is what makes it visible, because a caller that tolerates the failure -- which
is the whole reason those two survived -- would otherwise go on passing in silence.
"""

from __future__ import annotations

import contextlib
import errno
import os
import socket
from urllib.parse import urlsplit

_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex

#: Set for the live acceptance pass, where reaching real shops is the entire point.
ALLOW_NETWORK = os.environ.get("PRODUCT_TRACKER_ALLOW_NETWORK") == "1"


def _loopback_and_database() -> frozenset[str]:
    """What a test may connect to: itself, and its own database.

    Read at import time, before the settings fixture strips the environment for each
    test. The database is resolved to addresses as well as named, because the guard sees
    the address a connection is made to, not the hostname it came from.
    """
    allowed = {"127.0.0.1", "::1", "localhost", ""}
    for dsn in (os.environ.get("TEST_DATABASE_URL"), os.environ.get("DATABASE_URL")):
        host = urlsplit(dsn).hostname if dsn else None
        if not host:
            continue
        allowed.add(host)
        # An unresolvable DSN is the caller's problem, not this guard's.
        with contextlib.suppress(OSError):
            allowed.update(info[4][0] for info in socket.getaddrinfo(host, None))
    return frozenset(allowed)


ALLOWED_ADDRESSES = _loopback_and_database()

#: Hosts the running test tried to reach. Cleared per test by the fixture.
attempted: list[str] = []


def _address_host(address: object) -> str:
    """A Unix socket path is a plain string, and never leaves the machine."""
    if isinstance(address, tuple) and address:
        return str(address[0])
    return ""


def _refused(address: object) -> bool:
    host = _address_host(address)
    if host in ALLOWED_ADDRESSES:
        return False
    attempted.append(host)
    return True


def guard_connect(self, address):  # type: ignore[no-untyped-def]
    if _refused(address):
        raise OSError(f"blocked: tests must not connect to {_address_host(address)}")
    return _REAL_CONNECT(self, address)


def guard_connect_ex(self, address):  # type: ignore[no-untyped-def]
    if _refused(address):
        return errno.ECONNREFUSED
    return _REAL_CONNECT_EX(self, address)


def install(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Poison outbound connections for the duration of one test."""
    attempted.clear()
    monkeypatch.setattr(socket.socket, "connect", guard_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guard_connect_ex)


def report() -> str | None:
    """The message for a test that reached out, or None if it behaved."""
    if not attempted:
        return None
    hosts = ", ".join(sorted(set(attempted)))
    attempted.clear()
    return (
        f"this test tried to reach the internet ({hosts}). Tests must not depend on a "
        "third-party site being up: stub the fetch. Set PRODUCT_TRACKER_ALLOW_NETWORK=1 "
        "only for the deliberate live acceptance pass."
    )
