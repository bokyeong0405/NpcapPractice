"""analyzer: raw_packets → parsed_events.

Reads base64-encoded frames from the `raw_packets` Redis stream via a consumer
group, parses Ethernet/IP/TCP/UDP/ICMP, and republishes structured events to
the `parsed_events` stream.

L7 fields (l7_kind, l7_value) are reserved in the schema but left empty here;
they are populated in stage 4.
"""

import base64
import logging
import os
import signal
import socket
import time

import dpkt
import redis

LOG = logging.getLogger("analyzer")

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")
SRC_STREAM = os.environ.get("SRC_STREAM", "raw_packets")
DST_STREAM = os.environ.get("DST_STREAM", "parsed_events")
GROUP = os.environ.get("GROUP", "analyzer-cg")
CONSUMER = os.environ.get("CONSUMER", "analyzer-1")
DST_MAXLEN = int(os.environ.get("DST_MAXLEN", "100000"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "100"))
BLOCK_MS = int(os.environ.get("BLOCK_MS", "5000"))
LOG_INTERVAL_SEC = float(os.environ.get("LOG_INTERVAL_SEC", "5"))

PROTO_NAMES = {1: "ICMP", 6: "TCP", 17: "UDP", 58: "ICMPv6"}


def proto_name(num: int) -> str:
    return PROTO_NAMES.get(num, f"IP_{num}")


def parse_frame(raw: bytes):
    """Return parsed L3/L4 fields, or None for non-IP / unparseable frames."""
    eth = dpkt.ethernet.Ethernet(raw)
    payload = eth.data

    if isinstance(payload, dpkt.ip.IP):
        src_ip = ".".join(str(b) for b in payload.src)
        dst_ip = ".".join(str(b) for b in payload.dst)
        proto = proto_name(payload.p)
        l4 = payload.data
    elif isinstance(payload, dpkt.ip6.IP6):
        src_ip = socket.inet_ntop(socket.AF_INET6, payload.src)
        dst_ip = socket.inet_ntop(socket.AF_INET6, payload.dst)
        proto = proto_name(payload.nxt)
        l4 = payload.data
    else:
        return None

    sport = dport = 0
    if isinstance(l4, dpkt.tcp.TCP) or isinstance(l4, dpkt.udp.UDP):
        sport, dport = l4.sport, l4.dport

    return {
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "proto": proto,
        "sport": sport,
        "dport": dport,
    }


def ensure_group(r: redis.Redis):
    try:
        r.xgroup_create(SRC_STREAM, GROUP, id="0", mkstream=True)
        LOG.info("created consumer group %s on %s", GROUP, SRC_STREAM)
    except redis.ResponseError as e:
        if "BUSYGROUP" in str(e):
            LOG.info("consumer group %s already exists on %s", GROUP, SRC_STREAM)
        else:
            raise


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    LOG.info("connecting to %s", REDIS_URL)
    r = redis.Redis.from_url(REDIS_URL, decode_responses=False)
    r.ping()
    ensure_group(r)

    stop = False

    def handle_sig(signum, _frame):
        nonlocal stop
        stop = True
        LOG.info("signal %s received, stopping", signum)

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    parsed = skipped = failed = 0
    last_log = time.monotonic()

    LOG.info(
        "consuming %s as %s/%s -> %s (maxlen~%d)",
        SRC_STREAM, GROUP, CONSUMER, DST_STREAM, DST_MAXLEN,
    )

    while not stop:
        resp = r.xreadgroup(
            GROUP, CONSUMER,
            streams={SRC_STREAM: ">"},
            count=BATCH_SIZE,
            block=BLOCK_MS,
        )
        if not resp:
            continue

        ack_ids = []
        pipe = r.pipeline(transaction=False)

        for _stream_name, messages in resp:
            for msg_id, fields in messages:
                ts_ns = fields.get(b"ts_ns", b"0").decode()
                iface = fields.get(b"iface", b"").decode(errors="replace")
                snap_len = fields.get(b"snap_len", b"0").decode()
                raw_b64 = fields.get(b"raw_b64", b"")

                try:
                    raw = base64.b64decode(raw_b64)
                    parsed_fields = parse_frame(raw)
                except Exception as e:
                    failed += 1
                    LOG.warning("parse failed for %s: %s", msg_id, e)
                    ack_ids.append(msg_id)
                    continue

                if parsed_fields is None:
                    skipped += 1
                    ack_ids.append(msg_id)
                    continue

                pkt_size = int(snap_len) if snap_len else 0
                pipe.xadd(
                    DST_STREAM,
                    {
                        "ts_ns": ts_ns,
                        "iface": iface,
                        "src_ip": parsed_fields["src_ip"],
                        "dst_ip": parsed_fields["dst_ip"],
                        "sport": parsed_fields["sport"],
                        "dport": parsed_fields["dport"],
                        "proto": parsed_fields["proto"],
                        "packet_size": pkt_size,
                        # Reserved for stage 4 (DNS/HTTP/TLS SNI)
                        "l7_kind": "",
                        "l7_value": "",
                    },
                    maxlen=DST_MAXLEN,
                    approximate=True,
                )
                ack_ids.append(msg_id)
                parsed += 1

        try:
            pipe.execute()
            if ack_ids:
                r.xack(SRC_STREAM, GROUP, *ack_ids)
        except Exception as e:
            LOG.error("publish/ack batch failed: %s", e)

        now = time.monotonic()
        if now - last_log >= LOG_INTERVAL_SEC:
            LOG.info("parsed=%d skipped=%d failed=%d", parsed, skipped, failed)
            last_log = now

    LOG.info("stopped. parsed=%d skipped=%d failed=%d", parsed, skipped, failed)


if __name__ == "__main__":
    main()
