"""Resolve an administrator's network source across one explicit trusted hop."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable


class ClientSourceError(ValueError):
    pass


class TrustedClientSource:
    """Trust Envoy's external-address header only from configured proxy CIDRs."""

    header_name = "x-envoy-external-address"

    def __init__(self, trusted_proxy_cidrs: Iterable[str]) -> None:
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for value in trusted_proxy_cidrs:
            network = ipaddress.ip_network(value, strict=True)
            if network.prefixlen == 0:
                raise ValueError("an all-address network cannot be a trusted admin proxy")
            networks.append(network)
        self._trusted_proxy_networks = tuple(networks)

    def resolve(self, *, peer: str | None, forwarded: str | None) -> str:
        if peer is None:
            raise ClientSourceError("request has no network peer")
        try:
            peer_address = ipaddress.ip_address(peer)
        except ValueError as error:
            raise ClientSourceError("request peer is not an IP address") from error
        trusted_peer = any(
            peer_address.version == network.version and peer_address in network
            for network in self._trusted_proxy_networks
        )
        if not trusted_peer:
            return peer_address.compressed
        if forwarded is None or "," in forwarded or forwarded.strip() != forwarded:
            raise ClientSourceError("trusted proxy omitted its canonical client address")
        try:
            return ipaddress.ip_address(forwarded).compressed
        except ValueError as error:
            raise ClientSourceError("trusted proxy supplied an invalid client address") from error
