import ipaddress
import logging
import os
import socket
import struct
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("DNSResolver")

_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_DNS_LOCK = threading.Lock()
_DNS_CACHE: Dict[str, Tuple[str, float]] = {}
_CACHE_TTL = 300  # 5 minutes
_MAX_CACHE_SIZE = 256
_INSTALLED = False

PUBLIC_DNS_SERVERS = [
    "8.8.8.8",  # Google Primary
    "1.1.1.1",  # Cloudflare Primary
    "9.9.9.9",  # Quad9
    "8.8.4.4",  # Google Secondary
    "1.0.0.1",  # Cloudflare Secondary
]


def _is_ip_or_local(host: str) -> bool:
    """Checks whether the host is an IP literal, loopback, or local domain."""
    if not host or not isinstance(host, str):
        return True

    clean_host = host.strip().lower()
    if clean_host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "broadcasthost"):
        return True
    if clean_host.endswith(".local") or clean_host.endswith(".localhost"):
        return True

    try:
        ipaddress.ip_address(clean_host)
        return True
    except ValueError:
        return False


def _build_dns_query(hostname: str) -> Tuple[bytes, bytes]:
    """Builds a raw RFC 1035 standard DNS A-record query packet with random transaction ID."""
    transaction_id = os.urandom(2)
    flags = b"\x01\x00"  # Standard query with recursion desired (RD=1)
    counts = b"\x00\x01\x00\x00\x00\x00\x00\x00"  # QDCOUNT=1, ANCOUNT=0, NSCOUNT=0, ARCOUNT=0

    qname = b""
    for part in hostname.strip(".").split("."):
        encoded = part.encode("ascii", errors="ignore")
        if not encoded:
            continue
        qname += bytes([len(encoded)]) + encoded
    qname += b"\x00"

    qtype_qclass = b"\x00\x01\x00\x01"  # Type A (IPv4), Class IN (Internet)
    return transaction_id, transaction_id + flags + counts + qname + qtype_qclass


def _parse_dns_response(data: bytes, tx_id: bytes, query_qname_len: int) -> List[str]:
    """Parses IPv4 A-records from raw DNS answer packets with validation."""
    if len(data) < 12:
        return []

    # Validate Transaction ID
    if data[:2] != tx_id:
        return []

    # Check QR flag (bit 7 of byte 2 must be 1 for response) and RCODE (lower 4 bits of byte 3)
    qr_flag = (data[2] & 0x80) != 0
    rcode = data[3] & 0x0F
    if not qr_flag or rcode != 0:
        return []

    ancount = int.from_bytes(data[6:8], "big")
    if ancount == 0:
        return []

    # Skip header (12 bytes) + Question section (qname + 4 bytes for type/class)
    pos = 12 + query_qname_len + 4
    ips: List[str] = []

    for _ in range(ancount):
        if pos >= len(data):
            break

        # Handle compression pointers (0xC0) or label sequences
        while pos < len(data):
            if (data[pos] & 0xC0) == 0xC0:
                pos += 2
                break
            elif data[pos] == 0:
                pos += 1
                break
            else:
                pos += 1 + data[pos]

        if pos + 10 > len(data):
            break

        rtype = int.from_bytes(data[pos : pos + 2], "big")
        rdlength = int.from_bytes(data[pos + 8 : pos + 10], "big")
        pos += 10

        if rtype == 1 and rdlength == 4 and pos + 4 <= len(data):  # Type A
            ip = ".".join(str(b) for b in data[pos : pos + 4])
            ips.append(ip)

        pos += rdlength

    return ips


def _query_public_dns(hostname: str, timeout: float = 1.5) -> List[str]:
    """Queries public DNS servers directly over UDP port 53 with fallback rotation."""
    tx_id, packet = _build_dns_query(hostname)
    qname_len = sum(len(p) + 1 for p in hostname.strip(".").split(".")) + 1

    for dns_ip in PUBLIC_DNS_SERVERS:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            sock.sendto(packet, (dns_ip, 53))
            response, _ = sock.recvfrom(1024)
            ips = _parse_dns_response(response, tx_id, qname_len)
            if ips:
                return ips
        except Exception:
            continue
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
    return []


def resilient_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """
    Hook into Python's socket.getaddrinfo.
    1. Tries standard system/OS resolver.
    2. If system DNS fails (gaierror), resolves directly via Public DNS (Google/Cloudflare/Quad9).
    """
    try:
        return _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)
    except socket.gaierror:
        # Ignore IP literals and local loopback domains
        if not isinstance(host, str) or _is_ip_or_local(host):
            raise

        clean_host = host.strip().lower()
        now = time.time()

        # Check in-memory DNS cache
        with _DNS_LOCK:
            if clean_host in _DNS_CACHE:
                cached_ip, expiry = _DNS_CACHE[clean_host]
                if now < expiry:
                    return _ORIGINAL_GETADDRINFO(cached_ip, port, family, type, proto, flags)
                else:
                    _DNS_CACHE.pop(clean_host, None)

        # Fallback to direct public DNS resolution
        resolved_ips = _query_public_dns(clean_host)
        if resolved_ips:
            selected_ip = resolved_ips[0]
            with _DNS_LOCK:
                # Evict oldest entries if cache limit is reached
                if len(_DNS_CACHE) >= _MAX_CACHE_SIZE:
                    oldest_key = min(_DNS_CACHE, key=lambda k: _DNS_CACHE[k][1])
                    _DNS_CACHE.pop(oldest_key, None)
                _DNS_CACHE[clean_host] = (selected_ip, now + _CACHE_TTL)

            return _ORIGINAL_GETADDRINFO(selected_ip, port, family, type, proto, flags)

        raise


def install_resilient_dns():
    """Patches socket.getaddrinfo globally across Python with idempotency safeguards."""
    global _INSTALLED, _ORIGINAL_GETADDRINFO
    with _DNS_LOCK:
        if _INSTALLED:
            return
        if socket.getaddrinfo != resilient_getaddrinfo:
            _ORIGINAL_GETADDRINFO = socket.getaddrinfo
            socket.getaddrinfo = resilient_getaddrinfo
        _INSTALLED = True