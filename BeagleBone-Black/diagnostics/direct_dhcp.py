"""One-client DHCP responder for a direct BBB-to-host Ethernet link.

It leases only 192.168.50.2 on the host's 192.168.50.0/24 link.  This is for
bootstrap only: stop it with Ctrl+C once SSH has configured the BBB's permanent
network connection.
"""

from __future__ import annotations

import ipaddress
import socket
import struct


SERVER_IP = ipaddress.IPv4Address("192.168.50.1").packed
LEASE_IP = ipaddress.IPv4Address("192.168.50.2").packed
MASK = ipaddress.IPv4Address("255.255.255.0").packed
MAGIC = b"\x63\x82\x53\x63"


def get_option(options: bytes, wanted: int) -> bytes | None:
    i = 0
    while i < len(options):
        code = options[i]
        if code == 255:
            return None
        if code == 0:
            i += 1
            continue
        if i + 1 >= len(options):
            return None
        size = options[i + 1]
        value = options[i + 2 : i + 2 + size]
        if len(value) != size:
            return None
        if code == wanted:
            return value
        i += 2 + size
    return None


def reply(request: bytes, message_type: int) -> bytes:
    # Reuse xid, flags and client hardware address from the BOOTP request.
    xid = request[4:8]
    flags = request[10:12]
    chaddr = request[28:44]
    bootp = (
        b"\x02\x01\x06\x00" + xid + b"\x00\x00" + flags
        + b"\x00" * 4 + LEASE_IP + SERVER_IP + b"\x00" * 4
        + chaddr + b"\x00" * 64 + b"\x00" * 128
    )
    options = (
        MAGIC
        + bytes((53, 1, message_type))       # DHCP message type
        + bytes((54, 4)) + SERVER_IP          # server identifier
        + bytes((1, 4)) + MASK                # subnet mask
        + bytes((51, 4)) + struct.pack("!I", 3600)  # one-hour lease
        + b"\xff"
    )
    return bootp + options


def main() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", 67))
    print("DHCP bootstrap listening: 192.168.50.2/24 (Ctrl+C to stop)", flush=True)

    while True:
        packet, source = sock.recvfrom(2048)
        if len(packet) < 240 or packet[0] != 1 or packet[1:3] != b"\x01\x06":
            continue
        if packet[236:240] != MAGIC:
            continue
        kind = get_option(packet[240:], 53)
        if kind == b"\x01":
            response_type, label = 2, "OFFER"
        elif kind == b"\x03":
            response_type, label = 5, "ACK"
        else:
            continue
        mac = ":".join(f"{byte:02x}" for byte in packet[28:34])
        sock.sendto(reply(packet, response_type), ("192.168.50.255", 68))
        print(f"{label} 192.168.50.2 -> {mac} (request from {source[0]})", flush=True)


if __name__ == "__main__":
    main()
