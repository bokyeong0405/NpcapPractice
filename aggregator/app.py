"""aggregator: parsed_events -> TimescaleDB.

Reads parsed events from the `parsed_events` Redis stream, aggregates packets
into time-windowed counters (5-tuple / proto / talker / port), and upserts
to TimescaleDB hypertables.

Idempotency: each window is *fully* accumulated in memory before flush, then
written via INSERT ... ON CONFLICT (time, dims) DO UPDATE SET = EXCLUDED.
On at-least-once redelivery the same window produces the same sums and is
written to the same row, so re-flush is a no-op.
"""

import logging
import os
import signal
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg
import redis

LOG = logging.getLogger("aggregator")

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")
PG_DSN = os.environ.get("PG_DSN", "postgresql://pkt:pkt@timescaledb:5432/pkt")
SRC_STREAM = os.environ.get("SRC_STREAM", "parsed_events")
GROUP = os.environ.get("GROUP", "aggregator-cg")
CONSUMER = os.environ.get("CONSUMER", "aggregator-1")
WINDOW_SECONDS = int(os.environ.get("WINDOW_SECONDS", "10"))
GRACE_SECONDS = int(os.environ.get("GRACE_SECONDS", "5"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "500"))
BLOCK_MS = int(os.environ.get("BLOCK_MS", "1000"))
LOG_INTERVAL_SEC = float(os.environ.get("LOG_INTERVAL_SEC", "5"))


def make_counter():
    return [0, 0]  # [packets, bytes]


@dataclass
class WindowAgg:
    flows: dict = field(default_factory=lambda: defaultdict(make_counter))
    proto: dict = field(default_factory=lambda: defaultdict(make_counter))
    talkers: dict = field(default_factory=lambda: defaultdict(make_counter))
    ports: dict = field(default_factory=lambda: defaultdict(make_counter))
    msg_ids: list = field(default_factory=list)


def align_window(ts_ns: int, window_sec: int) -> datetime:
    sec = ts_ns // 1_000_000_000
    aligned = (sec // window_sec) * window_sec
    return datetime.fromtimestamp(aligned, tz=timezone.utc)


def add_packet(agg: WindowAgg, fields: dict, msg_id: str):
    src_ip = fields["src_ip"]
    dst_ip = fields["dst_ip"]
    sport = int(fields.get("sport") or 0)
    dport = int(fields.get("dport") or 0)
    proto = fields["proto"]
    size = int(fields.get("packet_size") or 0)

    f = agg.flows[(src_ip, dst_ip, sport, dport, proto)]
    f[0] += 1
    f[1] += size

    p = agg.proto[proto]
    p[0] += 1
    p[1] += size

    s = agg.talkers[(src_ip, "src")]
    s[0] += 1
    s[1] += size
    d = agg.talkers[(dst_ip, "dst")]
    d[0] += 1
    d[1] += size

    if sport > 0:
        sp = agg.ports[(sport, "src", proto)]
        sp[0] += 1
        sp[1] += size
    if dport > 0:
        dp = agg.ports[(dport, "dst", proto)]
        dp[0] += 1
        dp[1] += size

    agg.msg_ids.append(msg_id)


SQL_FLOWS = """
INSERT INTO flows (time, src_ip, dst_ip, sport, dport, proto, packets, bytes)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (time, src_ip, dst_ip, sport, dport, proto) DO UPDATE
   SET packets = EXCLUDED.packets,
       bytes   = EXCLUDED.bytes
"""

SQL_PROTO = """
INSERT INTO protocol_stats (time, proto, packets, bytes)
VALUES (%s, %s, %s, %s)
ON CONFLICT (time, proto) DO UPDATE
   SET packets = EXCLUDED.packets,
       bytes   = EXCLUDED.bytes
"""

SQL_TALKERS = """
INSERT INTO top_talkers (time, ip, role, packets, bytes)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (time, ip, role) DO UPDATE
   SET packets = EXCLUDED.packets,
       bytes   = EXCLUDED.bytes
"""

SQL_PORTS = """
INSERT INTO port_traffic (time, port, role, proto, packets, bytes)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (time, port, role, proto) DO UPDATE
   SET packets = EXCLUDED.packets,
       bytes   = EXCLUDED.bytes
"""


def flush_window(conn, window_start: datetime, agg: WindowAgg) -> int:
    with conn.cursor() as cur:
        if agg.flows:
            cur.executemany(SQL_FLOWS, [
                (window_start, k[0], k[1], k[2], k[3], k[4], v[0], v[1])
                for k, v in agg.flows.items()
            ])
        if agg.proto:
            cur.executemany(SQL_PROTO, [
                (window_start, k, v[0], v[1])
                for k, v in agg.proto.items()
            ])
        if agg.talkers:
            cur.executemany(SQL_TALKERS, [
                (window_start, k[0], k[1], v[0], v[1])
                for k, v in agg.talkers.items()
            ])
        if agg.ports:
            cur.executemany(SQL_PORTS, [
                (window_start, k[0], k[1], k[2], v[0], v[1])
                for k, v in agg.ports.items()
            ])
    conn.commit()
    return len(agg.msg_ids)


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

    LOG.info("connecting redis %s", REDIS_URL)
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    r.ping()

    LOG.info("connecting pg")
    conn = psycopg.connect(PG_DSN, autocommit=False)

    ensure_group(r)

    stop = False

    def handle_sig(signum, _frame):
        nonlocal stop
        stop = True
        LOG.info("signal %s received, stopping", signum)

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    windows: dict[datetime, WindowAgg] = {}
    total_packets = 0
    total_flushed_msgs = 0
    last_log = time.monotonic()

    LOG.info(
        "consuming %s as %s/%s, window=%ds grace=%ds",
        SRC_STREAM, GROUP, CONSUMER, WINDOW_SECONDS, GRACE_SECONDS,
    )

    while not stop:
        resp = r.xreadgroup(
            GROUP, CONSUMER,
            streams={SRC_STREAM: ">"},
            count=BATCH_SIZE,
            block=BLOCK_MS,
        )
        if resp:
            for _stream_name, messages in resp:
                for msg_id, fields in messages:
                    try:
                        ts_ns = int(fields["ts_ns"])
                        w = align_window(ts_ns, WINDOW_SECONDS)
                        if w not in windows:
                            windows[w] = WindowAgg()
                        add_packet(windows[w], fields, msg_id)
                        total_packets += 1
                    except Exception as e:
                        LOG.warning("bad event %s: %s", msg_id, e)

        # Flush windows older than (window + grace)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=WINDOW_SECONDS + GRACE_SECONDS)
        ready = sorted(w for w in windows if w <= cutoff)
        for w in ready:
            agg = windows.pop(w)
            try:
                n = flush_window(conn, w, agg)
                if agg.msg_ids:
                    r.xack(SRC_STREAM, GROUP, *agg.msg_ids)
                total_flushed_msgs += n
                LOG.debug(
                    "flushed window %s: %d msgs, flows=%d proto=%d talkers=%d ports=%d",
                    w.isoformat(), n,
                    len(agg.flows), len(agg.proto), len(agg.talkers), len(agg.ports),
                )
            except Exception as e:
                LOG.error("flush failed for %s: %s", w.isoformat(), e)
                conn.rollback()
                # Put it back so we retry on the next loop iteration
                windows[w] = agg
                break

        if time.monotonic() - last_log >= LOG_INTERVAL_SEC:
            LOG.info(
                "packets=%d flushed_msgs=%d open_windows=%d",
                total_packets, total_flushed_msgs, len(windows),
            )
            last_log = time.monotonic()

    # Drain remaining windows on shutdown
    LOG.info("draining %d open windows on shutdown", len(windows))
    for w in sorted(windows.keys()):
        agg = windows.pop(w)
        try:
            flush_window(conn, w, agg)
            if agg.msg_ids:
                r.xack(SRC_STREAM, GROUP, *agg.msg_ids)
        except Exception as e:
            LOG.error("final flush failed for %s: %s", w.isoformat(), e)
            conn.rollback()

    conn.close()
    LOG.info(
        "stopped. packets=%d flushed_msgs=%d",
        total_packets, total_flushed_msgs,
    )


if __name__ == "__main__":
    main()
