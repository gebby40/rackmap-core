"""Shared address checks."""

from __future__ import annotations

import ipaddress


def is_public_ip(host: str) -> bool:
    """True for a literal IP address that is not on a private network. Host names are not
    judged here (False): they are resolved when connecting."""
    try:
        return not ipaddress.ip_address(host).is_private
    except ValueError:
        return False
