"""The guard that keeps the suite off the internet, tested like anything else.

A guard nobody checks is a guard that quietly stops working. These are the negative
control: they prove the block still bites, and that it does not get in the way of the
loopback connections the database-backed tests depend on.
"""

from __future__ import annotations

import socket

import pytest
from tests import netguard


class TestWhatIsRefused:
    def test_a_public_address_is_blocked(self) -> None:
        """The autouse fixture is already installed, so this connects for real -- or
        rather, does not."""
        with pytest.raises(OSError, match="blocked"), socket.socket() as sock:
            sock.connect(("93.184.216.34", 80))

        assert netguard.attempted == ["93.184.216.34"]
        netguard.attempted.clear()

    def test_connect_ex_is_blocked_too(self) -> None:
        """``connect_ex`` returns an error number instead of raising, and asyncio uses it.
        Guarding only ``connect`` would leave that door open."""
        import errno

        with socket.socket() as sock:
            assert sock.connect_ex(("93.184.216.34", 80)) == errno.ECONNREFUSED

        assert netguard.attempted == ["93.184.216.34"]
        netguard.attempted.clear()

    def test_a_hostname_is_blocked_before_it_resolves(self) -> None:
        with pytest.raises(OSError, match="blocked"), socket.socket() as sock:
            sock.connect(("www.amazon.in", 443))

        assert netguard.attempted == ["www.amazon.in"]
        netguard.attempted.clear()


class TestWhatIsPermitted:
    def test_loopback_is_left_alone(self) -> None:
        """Refused by the kernel, since nothing is listening -- but refused for the right
        reason. The db tests would all fail if this went through the guard."""
        with socket.socket() as sock:
            sock.settimeout(1)
            with pytest.raises(OSError) as caught:
                sock.connect(("127.0.0.1", 1))

        assert "blocked" not in str(caught.value)
        assert netguard.attempted == []

    def test_the_test_database_is_reachable(self) -> None:
        """Whatever host the DSN names, resolved: the guard sees addresses, not names."""
        assert "127.0.0.1" in netguard.ALLOWED_ADDRESSES
