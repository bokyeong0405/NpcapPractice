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

-- ===========================================================================
-- L7 event tables (per-event timestamps; no UNIQUE — rare crash duplicates
-- are tolerated since dashboards roll up via COUNT/GROUP BY).
-- ===========================================================================

CREATE TABLE IF NOT EXISTS dns_queries (
    time      TIMESTAMPTZ NOT NULL,
    qname     TEXT        NOT NULL,
    client_ip INET        NOT NULL
);
SELECT create_hypertable('dns_queries', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS dns_queries_qname_idx ON dns_queries (qname, time DESC);

CREATE TABLE IF NOT EXISTS http_requests (
    time      TIMESTAMPTZ NOT NULL,
    host      TEXT        NOT NULL,
    method    TEXT        NOT NULL,
    path      TEXT        NOT NULL,
    client_ip INET        NOT NULL
);
SELECT create_hypertable('http_requests', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS http_requests_host_idx ON http_requests (host, time DESC);

CREATE TABLE IF NOT EXISTS tls_sni (
    time      TIMESTAMPTZ NOT NULL,
    sni       TEXT        NOT NULL,
    client_ip INET        NOT NULL
);
SELECT create_hypertable('tls_sni', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS tls_sni_sni_idx ON tls_sni (sni, time DESC);
