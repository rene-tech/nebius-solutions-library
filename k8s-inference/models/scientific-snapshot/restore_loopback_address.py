#!/usr/bin/env python3
"""Recreate a captured single-Pod socket address inside a fresh Pod only.

The caller supplies the donor Pod's recorded IPv4 address. Run in an isolated
restore Pod init container with NET_ADMIN, never hostNetwork. This does not
alter host interfaces, CNI, Services or address allocation. New captures should
prefer explicit loopback worker addressing to avoid this compatibility step.
"""

import argparse
import errno
import ipaddress
import json
import socket
import struct


def add_address(address: str) -> str:
    packed = ipaddress.IPv4Address(address).packed
    interface = socket.if_nametoindex("lo")
    # RTM_NEWADDR / ifaddrmsg, followed by IFA_LOCAL and IFA_ADDRESS attributes.
    body = struct.pack("BBBBI", socket.AF_INET, 32, 0, 0, interface)
    body += struct.pack("HH4s", 8, 2, packed) + struct.pack("HH4s", 8, 1, packed)
    request = struct.pack("IHHII", 16 + len(body), 20, 1 | 4 | 512 | 1024, 1, 0) + body
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as channel:
        channel.settimeout(5)
        channel.bind((0, 0))
        channel.send(request)
        response = channel.recv(4096)
    _, kind, _, _, _ = struct.unpack("IHHII", response[:16])
    if kind != 2:
        raise RuntimeError("network namespace did not acknowledge the address")
    result = struct.unpack("i", response[16:20])[0]
    if result not in (0, -errno.EEXIST):
        raise OSError(-result, "cannot recreate captured Pod-local address")
    return "created" if result == 0 else "already-present"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address")
    args = parser.parse_args()
    print(json.dumps({"event": "captured_pod_address", "address": args.address,
                      "interface": "lo", "status": add_address(args.address)}))


if __name__ == "__main__":
    main()
