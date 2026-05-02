# NpcapPractice

Npcap으로 로컬 NIC에서 패킷을 캡처하고, Docker 컨테이너로 분리한 파이프라인이 이를 **분석 → 집계 → 저장 → 시각화**하는 미니 패킷 분석 프로젝트.

## 개요

- **목표**: Windows 호스트 NIC에서 실시간 패킷을 캡처해, 컨테이너 분석 파이프라인에 흘려보내고 시계열 DB에 적재한 뒤 Grafana로 시각화한다.
- **컨셉**: 캡처 계층(호스트)과 분석 계층(컨테이너)을 분리하고, 분석 계층 안에서도 *파싱*과 *집계*를 다시 분리해 각 단계의 책임을 명확히 한다.
- **범위**: 학습용 미니 프로젝트. 운영 환경용 무손실/고성능 캡처는 목표가 아니다.

## 아키텍처

```
[ Windows Host ]                                [ Docker Compose Network ]

┌──────────────────────┐                  ┌─────────┐    ┌──────────┐    ┌──────────────┐
│ capture-agent (C++)  │ ──Redis Stream─▶ │  redis  │───▶│ analyzer │───▶│ aggregator   │
│  Npcap, 관리자 권한  │   raw_packets    │ (broker)│    │ (parse)  │    │ (window agg) │
└──────────────────────┘                  └─────────┘    └──────────┘    └──────┬───────┘
                                                                                ▼
                                                                        ┌──────────────┐
                                                                        │ TimescaleDB  │
                                                                        └──────┬───────┘
                                                                               ▼
                                                                        ┌──────────────┐
                                                                        │   Grafana    │
                                                                        └──────────────┘
```

> Npcap은 Windows 커널 드라이버라 컨테이너 안에서 호스트 NIC를 promiscuous로 잡을 수 없다. 따라서 capture-agent는 호스트에서 네이티브로 실행하고, Redis Stream을 다리로 컨테이너 네트워크에 붙인다.

## 컴포넌트

| 컴포넌트 | 위치 | 언어/런타임 | 역할 |
| --- | --- | --- | --- |
| **capture-agent** | Windows host | C++17 + Npcap SDK | NIC 캡처, 최소 메타만 붙여 `raw_packets` 스트림에 push |
| **redis** | container | Redis 7 | `raw_packets` / `parsed_events` 스트림 브로커 |
| **analyzer** | container | Python (dpkt) | L2~L7 파싱 후 `parsed_events`에 발행 |
| **aggregator** | container | Python (psycopg) | 시간 윈도우 집계 후 TimescaleDB에 batch insert |
| **timescaledb** | container | TimescaleDB (PostgreSQL) | 시계열 hypertable 영속화 |
| **grafana** | container | Grafana | 대시보드 |

## 데이터 모델

### 캡처 시 수집 필드 (per packet)

| 필드 | 설명 |
| --- | --- |
| `timestamp` | 패킷 캡처 시각 (ns) |
| `src_ip` / `dst_ip` | IPv4/IPv6 출발/도착 주소 |
| `src_port` / `dst_port` | TCP/UDP 출발/도착 포트 |
| `protocol` | L4 프로토콜 (TCP/UDP/ICMP/...) |
| `packet_size` | 프레임 길이 (bytes) |

### L7 파싱 항목 (해당 트래픽일 때만)

- **DNS**: query domain
- **HTTP**: Host, method, path
- **TLS**: SNI

### Redis Streams

| 스트림 | 생산자 → 소비자 | 페이로드 |
| --- | --- | --- |
| `raw_packets` | capture-agent → analyzer | `ts_ns`, `iface`, `snap_len`, `raw_b64` |
| `parsed_events` | analyzer → aggregator | `ts_ns`, `iface`, `src_ip`, `dst_ip`, `sport`, `dport`, `proto`, `packet_size`, `dns_qname`, `http_host`, `http_method`, `http_path`, `tls_sni` |

`MAXLEN ~` 옵션으로 스트림 길이를 제한해 메모리 폭주를 막는다.

### TimescaleDB hypertables

| 테이블 | 버킷 | 용도 |
| --- | --- | --- |
| `flows(time, src_ip, dst_ip, sport, dport, proto, packets, bytes)` | 10s | 5-tuple 단위 트래픽 |
| `protocol_stats(time, proto, packets, bytes)` | 1m | 프로토콜 비율 |
| `top_talkers(time, ip, role, packets, bytes)` | 1m | 송/수신 측 Top N |
| `port_traffic(time, port, role, packets, bytes)` | 1m | 포트별 분포 |
| `dns_queries(time, qname, client_ip)` | 이벤트 | DNS top domain |
| `http_requests(time, host, method, path, client_ip)` | 이벤트 | HTTP 가시성 |
| `tls_sni(time, sni, client_ip)` | 이벤트 | TLS SNI 가시성 |

## Grafana 대시보드

- 초당 패킷 수 (PPS)
- 초당 바이트 수 (Bps)
- 프로토콜별 비율
- Top talker (송신/수신)
- 목적지 IP Top N
- 포트별 트래픽 분포
- (보조) DNS top domains, TLS SNI top, HTTP host top

## 디렉토리 구조

```
NpcapPractice/
├─ capture-agent/                # Windows 호스트 네이티브 (C++)
│  ├─ CMakeLists.txt
│  ├─ src/
│  │  ├─ main.cpp                # CLI (--iface, --filter, --redis, --stream)
│  │  ├─ capture.cpp             # pcap_open_live / pcap_loop
│  │  └─ redis_sink.cpp          # XADD raw_packets
│  ├─ third_party/
│  │  └─ npcap-sdk/              # Npcap SDK (헤더/lib)
│  └─ README.md
├─ analyzer/
│  ├─ Dockerfile
│  ├─ requirements.txt           # redis, dpkt
│  └─ app.py
├─ aggregator/
│  ├─ Dockerfile
│  ├─ requirements.txt           # redis, psycopg
│  └─ app.py
├─ db/
│  └─ init.sql                   # hypertable + 인덱스
├─ grafana/
│  └─ provisioning/
│     ├─ datasources/timescaledb.yaml
│     └─ dashboards/
├─ docker-compose.yml
├─ .env.example
└─ README.md
```

## 요구 사항

- Windows 10/11
- [Npcap](https://npcap.com/) 설치 (WinPcap 호환 모드 권장)
- Npcap SDK
- Visual Studio Build Tools (MSVC, C++17)
- CMake 3.20+
- Docker Desktop

## 실행 (계획)

```powershell
# 1. 분석 스택 기동
docker compose up -d

# 2. capture-agent 빌드 (관리자 PowerShell)
cmake -S capture-agent -B capture-agent/build -A x64
cmake --build capture-agent/build --config Release

# 3. 캡처 시작 (관리자 권한 필요)
.\capture-agent\build\Release\capture-agent.exe `
    --iface "\Device\NPF_{GUID}" `
    --filter "ip" `
    --redis 127.0.0.1:6379 `
    --stream raw_packets

# 4. Grafana 접속
# http://localhost:3000  (admin/admin)
```

> Npcap 드라이버 호출에는 관리자 권한이 필요하다. capture-agent는 반드시 **관리자 PowerShell**에서 실행한다.

## 단계별 로드맵

| 단계 | 산출물 |
| --- | --- |
| **0. 스켈레톤** | docker-compose 기동 (redis + timescaledb + grafana), 헬스체크 |
| **1. capture-agent v0** | C++로 NIC 열거/선택, BPF 필터 적용, raw 프레임을 `raw_packets`에 push |
| **2. analyzer** | dpkt 기반 L2~L4 파싱, `parsed_events` 발행 |
| **3. aggregator + DB** | 윈도우 집계, TimescaleDB hypertable batch insert |
| **4. L7 파싱** | DNS query domain / HTTP Host·method·path / TLS SNI 추출 |
| **5. Grafana** | 대시보드 6종 프로비저닝 |
| **6. (스트레치)** | drop counter, pcap 파일 replay 모드, capture-agent 핫패스 최적화 |

## 비기능적 메모

- **캡처 무손실은 보장하지 않는다.** Redis 백프레셔 / 스냅 길이 / BPF 필터로 부하를 제어한다.
- **개인 PC / 본인이 권한을 가진 네트워크에서만** 캡처한다. 사내·공용망 무단 캡처 금지.
- L7 파싱은 평문 트래픽에 한한다 (HTTPS 본문은 파싱하지 않으며 SNI만 추출).
