# analyzer

`raw_packets` 스트림에서 base64 인코딩된 프레임을 꺼내 L2~L4 파싱 후 `parsed_events` 스트림에 발행한다.

## 동작

1. Redis 연결 후 `XGROUP CREATE raw_packets analyzer-cg 0 MKSTREAM` (idempotent)
2. `XREADGROUP analyzer-cg analyzer-1 COUNT 100 BLOCK 5000 STREAMS raw_packets >`
3. 각 메시지에 대해:
   - `raw_b64` 디코드 → `dpkt.ethernet.Ethernet` 파싱
   - IPv4 / IPv6 → src/dst IP, proto
   - TCP / UDP → sport/dport (그 외엔 0)
   - 비-IP (ARP 등) 프레임은 스킵
4. 파이프라인으로 `XADD parsed_events MAXLEN ~`
5. 일괄 `XACK`

at-least-once: `XADD` 성공 후 `XACK`. 크래시 시 중복 가능, 누락 없음.

## 출력 스키마 (`parsed_events`)

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `ts_ns` | int (str) | 캡처 시각 (ns), 입력 그대로 전달 |
| `iface` | str | NPF 디바이스 경로 |
| `src_ip` / `dst_ip` | str | dotted IPv4 또는 IPv6 |
| `sport` / `dport` | int | TCP/UDP 포트, 그 외 0 |
| `proto` | str | TCP / UDP / ICMP / ICMPv6 / IP_<num> |
| `packet_size` | int | 캡처된 바이트 수 (caplen) |
| `l7_kind` | str | 4단계에서 채움 (DNS/HTTP/TLS) |
| `l7_value` | str | 4단계에서 채움 |

## 환경 변수

| 변수 | 기본값 |
| --- | --- |
| `REDIS_URL` | `redis://redis:6379` |
| `SRC_STREAM` | `raw_packets` |
| `DST_STREAM` | `parsed_events` |
| `GROUP` | `analyzer-cg` |
| `CONSUMER` | `analyzer-1` |
| `DST_MAXLEN` | `100000` |
| `BATCH_SIZE` | `100` |
| `BLOCK_MS` | `5000` |
| `LOG_INTERVAL_SEC` | `5` |
| `LOG_LEVEL` | `INFO` |

## 실행

루트 `docker-compose.yml`로 함께 기동:

```powershell
docker compose up -d --build
docker compose logs -f analyzer
```

## 검증

```powershell
# parsed_events 길이
docker compose exec redis redis-cli XLEN parsed_events

# 최신 1건
docker compose exec redis redis-cli XREVRANGE parsed_events + - COUNT 1

# consumer group / pending 상태
docker compose exec redis redis-cli XINFO GROUPS raw_packets
docker compose exec redis redis-cli XPENDING raw_packets analyzer-cg
```
