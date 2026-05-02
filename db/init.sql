-- ===========================================================================
-- NpcapPractice schema
--
-- All tables are hypertables partitioned on `time` with a UNIQUE constraint
-- spanning (time, dims) so the aggregator can use INSERT ... ON CONFLICT
-- DO UPDATE SET = EXCLUDED to make per-window flushes idempotent.
-- ===========================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------------------
-- flows: 5-tuple counters per window
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS flows (
    time     TIMESTAMPTZ NOT NULL,
    src_ip   INET        NOT NULL,
    dst_ip   INET        NOT NULL,
    sport    INT         NOT NULL,
    dport    INT         NOT NULL,
    proto    TEXT        NOT NULL,
    packets  BIGINT      NOT NULL,
    bytes    BIGINT      NOT NULL,
    UNIQUE (time, src_ip, dst_ip, sport, dport, proto)
);
SELECT create_hypertable('flows', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS flows_src_idx   ON flows (src_ip, time DESC);
CREATE INDEX IF NOT EXISTS flows_dst_idx   ON flows (dst_ip, time DESC);
CREATE INDEX IF NOT EXISTS flows_proto_idx ON flows (proto,  time DESC);

-- ---------------------------------------------------------------------------
-- protocol_stats: per-proto counters per window
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS protocol_stats (
    time     TIMESTAMPTZ NOT NULL,
    proto    TEXT        NOT NULL,
    packets  BIGINT      NOT NULL,
    bytes    BIGINT      NOT NULL,
    UNIQUE (time, proto)
);
SELECT create_hypertable('protocol_stats', 'time', if_not_exists => TRUE);

-- ---------------------------------------------------------------------------
-- top_talkers: per-IP counters split by role (src|dst)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS top_talkers (
    time     TIMESTAMPTZ NOT NULL,
    ip       INET        NOT NULL,
    role     TEXT        NOT NULL,   -- 'src' | 'dst'
    packets  BIGINT      NOT NULL,
    bytes    BIGINT      NOT NULL,
    UNIQUE (time, ip, role)
);
SELECT create_hypertable('top_talkers', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS top_talkers_ip_idx ON top_talkers (ip, time DESC);

-- ---------------------------------------------------------------------------
-- port_traffic: per-port counters split by role + proto
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS port_traffic (
    time     TIMESTAMPTZ NOT NULL,
    port     INT         NOT NULL,
    role     TEXT        NOT NULL,   -- 'src' | 'dst'
    proto    TEXT        NOT NULL,
    packets  BIGINT      NOT NULL,
    bytes    BIGINT      NOT NULL,
    UNIQUE (time, port, role, proto)
);
SELECT create_hypertable('port_traffic', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS port_traffic_port_idx ON port_traffic (port, time DESC);
