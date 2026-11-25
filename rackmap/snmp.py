"""A very small SNMP v2c client: GET and GETNEXT over UDP, enough to ask a device what it is
and to read a router's table of addresses. No dependency; only the parts of BER that SNMP uses.
"""

from __future__ import annotations

import ipaddress
import secrets
import socket

SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
ARP_TABLE = "1.3.6.1.2.1.4.22.1.2"  # ipNetToMediaPhysAddress: ...<ifIndex>.<a>.<b>.<c>.<d> = MAC

_INTEGER, _OCTETS, _NULL, _OID, _SEQUENCE = 0x02, 0x04, 0x05, 0x06, 0x30
_GET, _GETNEXT, _RESPONSE = 0xA0, 0xA1, 0xA2
_END_MARKERS = (0x80, 0x81, 0x82)  # noSuchObject, noSuchInstance, endOfMibView


class SnmpError(Exception):
    pass


def _length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _length(len(value)) + value


def _int(n: int) -> bytes:
    raw = n.to_bytes(n.bit_length() // 8 + 1, "big", signed=True)
    return _tlv(_INTEGER, raw)


def _oid(dotted: str) -> bytes:
    parts = [int(p) for p in dotted.strip(".").split(".")]
    if len(parts) < 2:
        raise SnmpError("An object identifier needs at least two parts.")
    out = bytearray([parts[0] * 40 + parts[1]])
    for part in parts[2:]:
        chunk = [part & 0x7F]
        part >>= 7
        while part:
            chunk.append(0x80 | (part & 0x7F))
            part >>= 7
        out.extend(reversed(chunk))
    return _tlv(_OID, bytes(out))


def encode_request(community: str, pdu_type: int, request_id: int, oid: str) -> bytes:
    varbind = _tlv(_SEQUENCE, _oid(oid) + _tlv(_NULL, b""))
    pdu = _tlv(pdu_type, _int(request_id) + _int(0) + _int(0) + _tlv(_SEQUENCE, varbind))
    return _tlv(_SEQUENCE, _int(1) + _tlv(_OCTETS, community.encode()) + pdu)  # version 1 = v2c


def _read(data: bytes, at: int) -> tuple[int, bytes, int]:
    """Return (tag, value, index after) for the element starting at `at`."""
    if at + 2 > len(data):
        raise SnmpError("Reply cut short.")
    tag = data[at]
    first = data[at + 1]
    at += 2
    if first & 0x80:
        count = first & 0x7F
        if count == 0 or count > 4 or at + count > len(data):
            raise SnmpError("Reply has a length this client can't read.")
        size = int.from_bytes(data[at:at + count], "big")
        at += count
    else:
        size = first
    if at + size > len(data):
        raise SnmpError("Reply cut short.")
    return tag, data[at:at + size], at + size


def _decode_oid(raw: bytes) -> str:
    if not raw:
        return ""
    parts = [raw[0] // 40, raw[0] % 40]
    value = 0
    for byte in raw[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(value)
            value = 0
    return ".".join(str(p) for p in parts)


def decode_response(data: bytes, request_id: int | None = None) -> tuple[str, int, bytes]:
    """Return (oid, value tag, value bytes) from a response holding one variable."""
    tag, message, _ = _read(data, 0)
    if tag != _SEQUENCE:
        raise SnmpError("Not an SNMP reply.")
    _, _version, at = _read(message, 0)
    _, _community, at = _read(message, at)
    tag, pdu, _ = _read(message, at)
    if tag != _RESPONSE:
        raise SnmpError("Not an SNMP response.")
    _, rid, at = _read(pdu, 0)
    if request_id is not None and int.from_bytes(rid, "big", signed=True) != request_id:
        raise SnmpError("Reply to a different request.")
    _, status, at = _read(pdu, at)
    _, _index, at = _read(pdu, at)
    if int.from_bytes(status, "big") != 0:
        raise SnmpError("The device reported an error for that request.")
    _, bindings, _ = _read(pdu, at)
    _, binding, _ = _read(bindings, 0)
    tag, oid_raw, at = _read(binding, 0)
    if tag != _OID:
        raise SnmpError("Reply is missing its object identifier.")
    value_tag, value, _ = _read(binding, at)
    return _decode_oid(oid_raw), value_tag, value


def _ask(host: str, community: str, pdu_type: int, oid: str, timeout: float, port: int = 161) -> tuple[str, int, bytes]:
    request_id = secrets.randbelow(2**31 - 1) + 1
    packet = encode_request(community, pdu_type, request_id, oid)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.sendto(packet, (host, port))
            data, _ = sock.recvfrom(65535)
        except OSError as exc:
            raise SnmpError("No SNMP answer.") from exc
    return decode_response(data, request_id)


def get_text(host: str, community: str, oid: str, timeout: float = 1.0) -> str | None:
    """One value as text, or None if the device has nothing there. Raises SnmpError if it doesn't answer."""
    _, tag, value = _ask(host, community, _GET, oid, timeout)
    if tag in _END_MARKERS or tag == _NULL:
        return None
    return value.decode("utf-8", errors="replace").strip()


def arp_table(host: str, community: str, timeout: float = 2.0, limit: int = 4096) -> dict[str, str]:
    """Walk a router's address table. Returns {ip: 'aa:bb:cc:dd:ee:ff'}."""
    found: dict[str, str] = {}
    oid = ARP_TABLE
    for _ in range(limit):
        next_oid, tag, value = _ask(host, community, _GETNEXT, oid, timeout)
        if tag in _END_MARKERS or not next_oid.startswith(ARP_TABLE + ".") or next_oid == oid:
            break
        oid = next_oid
        parts = next_oid.split(".")
        if tag == _OCTETS and len(value) == 6 and len(parts) >= 4:
            try:
                ip = str(ipaddress.ip_address(".".join(parts[-4:])))
            except ValueError:
                continue
            found[ip] = ":".join(f"{b:02x}" for b in value)
    return found
