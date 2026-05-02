# aggregator

`parsed_events` 스트림을 소비해 시간 윈도우 단위로 집계 후 TimescaleDB hypertable에 upsert.

## 동작

1. Redis 연결 + `XGROUP CREATE parsed_events aggregator-cg 0 MKSTREAM` (idempotent)
2. PG 연결
3. `XREADGROUP aggregator-cg aggregator-1 COUNT 500 BLOCK 1000 STREAMS parsed_events >`
4. 각 이벤트의 `ts_ns`로 10초 윈도우 정렬
5. 메모리에서 4종 카운터 + 3종 L7 이벤트 리스트 누적:
   - 카운터(윈도우 정렬 시각): `flows` (5-tuple) / `protocol_stats` (proto) / `top_talkers` (ip, role) / `port_traffic` (port, role, proto)
   - 이벤트(원본 ts_ns 보존): `dns_queries` / `http_requests` / `tls_sni`
6. 윈도우 종료 시각이 `wall_clock - (window + grace)` 보다 오래되면:
   - 카운터: batch upsert (`INSERT ... ON CONFLICT DO UPDATE SET = EXCLUDED`)
   - L7 이벤트: 각 이벤트의 원본 `ts_ns` 그대로 일반 `INSERT` (UNIQUE 없음)
   - 해당 윈도우에 기여한 모든 메시지 일괄 `XACK`

## 멱등성 — 왜 동작하는가

**카운터 4종**:
- 윈도우의 합계는 메모리에서 *완전히* 누적된 뒤 한 번에 flush
- 모든 hypertable에 `UNIQUE (time, dims...)` 가 걸려있음
- INSERT는 `ON CONFLICT ... DO UPDATE SET = EXCLUDED` (덧셈 X, **덮어쓰기**)
- → at-least-once 환경에서 같은 윈도우가 재처리되어도 같은 행에 같은 값이 다시 써짐 → 결과 불변

**L7 이벤트 3종**:
- UNIQUE 없이 단순 INSERT. 크래시 직후 같은 윈도우 재처리 시 드물게 중복 행 발생.
- 대시보드는 `COUNT/GROUP BY qname` 등으로 집계하므로 작은 중복은 top-N 결과에 거의 영향 없음.

## 알려진 한계

- 윈도우 grace(`5초`)를 넘긴 *지각 패킷*은 무시됨. 미니 프로젝트 스코프엔 OK.
- aggregator 다중 인스턴스 운용 시, 같은 윈도우에 두 인스턴스가 각자 부분 합계를 쓰면 마지막 쓴 쪽이 이긴다. 1인스턴스 운용 권장.

## 환경 변수

| 변수 | 기본값 |
| --- | --- |
| `REDIS_URL` | `redis://redis:6379` |
| `PG_DSN` | `postgresql://pkt:pkt@timescaledb:5432/pkt` |
| `SRC_STREAM` | `parsed_events` |
| `GROUP` | `aggregator-cg` |
| `CONSUMER` | `aggregator-1` |
| `WINDOW_SECONDS` | `10` |
| `GRACE_SECONDS` | `5` |
| `BATCH_SIZE` | `500` |
| `BLOCK_MS` | `1000` |
| `LOG_INTERVAL_SEC` | `5` |
| `LOG_LEVEL` | `INFO` |

## 검증

```powershell
docker compose exec timescaledb psql -U pkt -d pkt
```

```sql
-- 최근 1분간 10초 단위 PPS / Bps
SELECT time_bucket('10 seconds', time) AS t,
       SUM(packets) AS packets,
       SUM(bytes)   AS bytes
FROM flows
WHERE time > now() - interval '1 minute'
GROUP BY t
ORDER BY t;

-- 프로토콜 비율 (최근 5분)
SELECT proto,
       SUM(packets) AS packets,
       SUM(bytes)   AS bytes
FROM protocol_stats
WHERE time > now() - interval '5 minutes'
GROUP BY proto
ORDER BY bytes DESC;

-- Top talker (송신 기준, 최근 5분)
SELECT ip,
       SUM(bytes) AS bytes
FROM top_talkers
WHERE time > now() - interval '5 minutes' AND role = 'src'
GROUP BY ip
ORDER BY bytes DESC
LIMIT 10;

-- 포트별 트래픽 (목적지 기준)
SELECT port, proto,
       SUM(packets) AS packets,
       SUM(bytes)   AS bytes
FROM port_traffic
WHERE time > now() - interval '5 minutes' AND role = 'dst'
GROUP BY port, proto
ORDER BY bytes DESC
LIMIT 20;

-- DNS top domains (최근 5분)
SELECT qname, COUNT(*) AS queries
FROM dns_queries
WHERE time > now() - interval '5 minutes'
GROUP BY qname
ORDER BY queries DESC
LIMIT 20;

-- TLS SNI top (최근 5분)
SELECT sni, COUNT(*) AS hellos
FROM tls_sni
WHERE time > now() - interval '5 minutes'
GROUP BY sni
ORDER BY hellos DESC
LIMIT 20;

-- HTTP host top (최근 5분)
SELECT host, method, COUNT(*) AS reqs
FROM http_requests
WHERE time > now() - interval '5 minutes'
GROUP BY host, method
ORDER BY reqs DESC
LIMIT 20;
```
