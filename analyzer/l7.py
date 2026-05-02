"""L7 protocol parsers: DNS query, HTTP request, TLS SNI.

All parsers return empty strings (or empty tuples) on any failure — a
non-matching packet, partial segment, or non-handshake-first-segment is
treated as "no L7 info for this packet" rather than an error.
"""

import dpkt


def _to_str(v) -> str:
    if isinstance(v, bytes):
        return v.decode("ascii", errors="replace")
    return v or ""


def parse_dns(payload: bytes) -> str:
    """Return the first query name for a DNS *query*, or empty string."""
    try:
        dns = dpkt.dns.DNS(payload)
        if dns.qr == dpkt.dns.DNS_Q and dns.qd:
            return _to_str(dns.qd[0].name)
    except Exception:
        pass
    return ""


def parse_http(payload: bytes) -> tuple[str, str, str]:
    """Return (host, method, path) for an HTTP request, or three empty strings.

    Responses (HTTP/x.x ...) and mid-stream segments fail to unpack and are
    treated as no-match.
    """
    if len(payload) < 14:  # smallest possible "GET / HTTP/1.0\r\n"
        return ("", "", "")
    try:
        req = dpkt.http.Request(payload)
        host = req.headers.get("host", "")
        return (_to_str(host), _to_str(req.method), _to_str(req.uri))
    except Exception:
        return ("", "", "")


def parse_tls_sni(payload: bytes) -> str:
    """Extract the SNI host_name from a TLS ClientHello.

    Returns "" if the segment is not a TLS handshake / ClientHello, if the
    ClientHello spans multiple segments, or if there's no SNI extension.
    """
    if len(payload) < 5:
        return ""
    if payload[0] != 0x16:                  # TLS record type: handshake
        return ""
    record_len = int.from_bytes(payload[3:5], "big")
    if len(payload) < 5 + record_len:
        return ""
    handshake = payload[5:5 + record_len]
    if len(handshake) < 4 or handshake[0] != 0x01:  # ClientHello
        return ""
    hs_len = int.from_bytes(handshake[1:4], "big")
    if len(handshake) < 4 + hs_len:
        return ""
    body = handshake[4:4 + hs_len]

    try:
        # legacy_version(2) + random(32)
        i = 2 + 32
        if i + 1 > len(body):
            return ""
        sid_len = body[i]
        i += 1 + sid_len

        if i + 2 > len(body):
            return ""
        cs_len = int.from_bytes(body[i:i + 2], "big")
        i += 2 + cs_len

        if i + 1 > len(body):
            return ""
        cm_len = body[i]
        i += 1 + cm_len

        if i + 2 > len(body):
            return ""
        ext_total_len = int.from_bytes(body[i:i + 2], "big")
        i += 2
        ext_end = i + ext_total_len

        while i + 4 <= ext_end:
            ext_type = int.from_bytes(body[i:i + 2], "big")
            ext_data_len = int.from_bytes(body[i + 2:i + 4], "big")
            ext_data_start = i + 4
            ext_data_end = ext_data_start + ext_data_len

            if ext_type == 0x0000:  # server_name
                # name_list_len(2) + (name_type(1) + name_len(2) + name)*
                if ext_data_start + 2 > ext_data_end:
                    return ""
                j = ext_data_start + 2
                while j + 3 <= ext_data_end:
                    name_type = body[j]
                    j += 1
                    name_len = int.from_bytes(body[j:j + 2], "big")
                    j += 2
                    if j + name_len > ext_data_end:
                        return ""
                    if name_type == 0:  # host_name
                        return body[j:j + name_len].decode("ascii", errors="replace")
                    j += name_len
                return ""

            i = ext_data_end
    except (IndexError, ValueError):
        return ""
    return ""
