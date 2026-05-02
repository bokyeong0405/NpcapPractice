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
| **capture-agent** | Windows host | C++17 + Npcap SDK + hiredis | NIC 캡처, 최소 메타만 붙여 `raw_packets` 스트림에 push |
| **redis** | container | Redis 7 | `raw_packets` / `parsed_events` 스트림 브로커 |
| **analyzer** | container | Python 3.12 + dpkt | L2~L7 파싱 후 `parsed_events`에 발행 |
| **aggregator** | container | Python 3.12 + psycopg | 시간 윈도우 집계 후 TimescaleDB에 batch upsert |
| **timescaledb** | container | TimescaleDB (PostgreSQL 16) | 시계열 hypertable 영속화 |
| **grafana** | container | Grafana 10.4 | 대시보드 (provisioning) |

## 수집된 정보

캡처된 프레임은 4단계 파이프라인을 거치며 점점 풍부한 메타데이터를 얻는다.

### 1단계 · 캡처 (capture-agent → Redis stream `raw_packets`)

호스트 NIC에서 Npcap으로 잡은 raw 프레임을 base64 인코딩해 푸시.

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `ts_ns` | int64 | epoch ns 단위 캡처 시각 (`pcap_pkthdr.ts`) |
| `iface` | string | NPF 디바이스 경로 |
| `snap_len` | int | 캡처된 바이트 수 (caplen) |
| `raw_b64` | string | base64로 인코딩된 프레임 전체 |

### 2단계 · 파싱 (analyzer → Redis stream `parsed_events`)

프레임을 디코드해 L2~L4 헤더 + 일부 L7을 구조화.

**L2/L3/L4 — 모든 IP 패킷**

| 필드 | 출처 | 설명 |
| --- | --- | --- |
| `ts_ns` | 1단계 그대로 | |
| `iface` | 1단계 그대로 | |
| `src_ip`, `dst_ip` | IPv4/IPv6 헤더 | dotted IPv4 또는 표준 IPv6 |
| `sport`, `dport` | TCP/UDP 헤더 | TCP/UDP가 아니면 `0` |
| `proto` | IP `proto`/`nxt` | `TCP` / `UDP` / `ICMP` / `ICMPv6` / `IP_<num>` |
| `packet_size` | caplen | 캡처된 바이트 수 |

**L7 — 해당 트래픽일 때만, 안 잡히면 빈 문자열**

| 필드 | 트리거 | 추출 방법 |
| --- | --- | --- |
| `dns_qname` | UDP:53 query | `dpkt.dns` 의 첫 query name |
| `http_host`, `http_method`, `http_path` | TCP:80 request | `dpkt.http.Request` |
| `tls_sni` | TCP:443 ClientHello | TLS record → handshake → extension `0x00` 직접 디코드 |

L7 파싱의 한계:
- HTTP/TLS는 첫 TCP 세그먼트에 헤더가 모두 있어야 매칭 (분할 시 무시)
- 비-표준 포트 (8080의 HTTP 등)는 미감지
- HTTP 응답 / 중간 데이터 / TLS Application Data 는 자연스럽게 무시
- 비-IP 프레임 (ARP 등)은 analyzer가 ack만 하고 스킵

### 3단계 · 집계 + 저장 (aggregator → TimescaleDB hypertable)

10초 윈도우로 in-memory 집계한 뒤 TimescaleDB hypertable에 upsert (`INSERT ... ON CONFLICT (time, dims) DO UPDATE SET = EXCLUDED`). 결정론적 합산 + replace upsert로 at-least-once 환경에서 멱등.

**집계 테이블 — 10초 윈도우 카운터**

| 테이블 | 차원 (PK) | 측정값 |
| --- | --- | --- |
| `flows` | time, src_ip, dst_ip, sport, dport, proto | packets, bytes |
| `protocol_stats` | time, proto | packets, bytes |
| `top_talkers` | time, ip, role(`src`/`dst`) | packets, bytes |
| `port_traffic` | time, port, role, proto | packets, bytes |

**이벤트 테이블 — 원본 `ts_ns` 보존, 일반 INSERT**

| 테이블 | 컬럼 |
| --- | --- |
| `dns_queries` | time, qname, client_ip |
| `http_requests` | time, host, method, path, client_ip |
| `tls_sni` | time, sni, client_ip |

(`client_ip` 는 항상 `src_ip` — analyzer가 client→server 방향만 매칭하기 때문)

### 4단계 · 시각화 (Grafana)

TimescaleDB를 데이터 소스로 단일 대시보드 *NpcapPractice Overview* 가 9개 패널을 자동 프로비저닝.

| 패널 | 출처 테이블 |
| --- | --- |
| Packets / sec | `flows` |
| Bytes / sec | `flows` |
| Protocol ratio (donut) | `protocol_stats` |
| Top senders (bar) | `top_talkers` (role=src) |
| Top destination IPs (bar) | `top_talkers` (role=dst) |
| Top destination ports | `port_traffic` |
| DNS top domains | `dns_queries` |
| TLS SNI top | `tls_sni` |
| HTTP host top | `http_requests` |

## 디렉토리 구조

```
NpcapPractice/
├─ capture-agent/                    # Windows 호스트 네이티브 (C++)
│  ├─ CMakeLists.txt
│  ├─ CMakePresets.json
│  ├─ vcpkg.json
│  ├─ src/
│  │  ├─ main.cpp                    # CLI 파싱, 메인 루프
│  │  ├─ capture.{hpp,cpp}           # pcap_findalldevs / pcap_open_live / pcap_loop
│  │  ├─ redis_sink.{hpp,cpp}        # XADD raw_packets
│  │  └─ base64.hpp
│  └─ third_party/npcap-sdk/         # ★ 직접 다운로드, gitignore됨
├─ analyzer/
│  ├─ Dockerfile
│  ├─ requirements.txt               # redis, dpkt
│  ├─ app.py                         # XREADGROUP → 파싱 → XADD parsed_events
│  └─ l7.py                          # DNS / HTTP / TLS SNI 파서
├─ aggregator/
│  ├─ Dockerfile
│  ├─ requirements.txt               # redis, psycopg[binary]
│  └─ app.py                         # 10s 윈도우 in-memory 집계 → TimescaleDB upsert
├─ db/
│  └─ init.sql                       # 7개 hypertable + UNIQUE 제약 + 인덱스
├─ grafana/
│  ├─ provisioning/
│  │  ├─ datasources/timescaledb.yaml
│  │  └─ dashboards/dashboards.yaml
│  └─ dashboards/
│     └─ overview.json               # 9-패널 대시보드 정의
├─ docker-compose.yml                # redis + analyzer + timescaledb + aggregator + grafana
├─ .gitignore
└─ README.md
```

## 실행

처음 클론한 상태에서 대시보드까지 보는 풀 절차.

### 0. 사전 준비 (1회만)

#### 0-A. 호스트 도구

| 도구 | 용도 |
| --- | --- |
| Windows 10/11 | OS |
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | 분석 스택 컨테이너 실행 |
| Visual Studio 2022 Build Tools (Desktop development with C++) | MSVC, C++17 |
| CMake 3.20+ | capture-agent 빌드 |
| [Npcap](https://npcap.com/#download) | NIC 패킷 캡처 (WinPcap 호환 모드 체크) |

#### 0-B. Npcap SDK 배치

[npcap.com](https://npcap.com/#download) 에서 SDK zip 다운로드 후 다음 중 하나로 위치 지정:

- `capture-agent/third_party/npcap-sdk/` 로 압축 해제 (이 경로는 `.gitignore`에 포함)
- 또는 환경변수 `NPCAP_SDK_DIR=C:\path\to\npcap-sdk` 설정
- 또는 빌드 시 `cmake -DNPCAP_SDK_DIR=...`

압축 해제 후 `<sdk_dir>/Include/pcap.h` 파일이 보여야 한다.

#### 0-C. vcpkg 부트스트랩 (capture-agent의 hiredis 의존성용)

```powershell
git clone https://github.com/microsoft/vcpkg.git C:\dev\vcpkg
C:\dev\vcpkg\bootstrap-vcpkg.bat
[Environment]::SetEnvironmentVariable("VCPKG_ROOT", "C:\dev\vcpkg", "User")
# 새 PowerShell 세션을 열고 아래로 확인
$env:VCPKG_ROOT
```

### 1. capture-agent 빌드 (1회만)

```powershell
cd capture-agent
cmake --preset default
cmake --build build --config Release
```

산출물: `capture-agent\build\Release\capture-agent.exe`

빌드 후 사용할 NIC의 NPF 경로 확인:

```powershell
.\build\Release\capture-agent.exe --list-ifaces
```

출력 예:
```
[0] \Device\NPF_{2D7309D7-BB2C-4DF4-929E-C1E9503227AF}
    Intel(R) Wi-Fi 6 AX201
[1] \Device\NPF_Loopback
    Adapter for loopback traffic capture
```

사용할 디바이스 경로를 메모해둔다.

### 2. 분석 스택 기동

repo 루트에서:

```powershell
docker compose up -d --build
docker compose ps
```

5개 서비스(`redis`, `analyzer`, `timescaledb`, `aggregator`, `grafana`)가 모두 `running` 또는 `healthy` 상태여야 한다.

서비스 로그 확인:
```powershell
docker compose logs --tail 50 analyzer aggregator
```

### 3. 캡처 시작

**관리자 권한 PowerShell** 에서 (Npcap 드라이버 호출에 관리자 권한 필수):

```powershell
cd capture-agent
.\build\Release\capture-agent.exe `
    --iface "\Device\NPF_{<위에서 확인한 GUID>}" `
    --filter "ip" `
    --redis 127.0.0.1:6379 `
    --stream raw_packets
```

화면에 `[capture-agent] pushed=N dropped=M` 카운터가 흐르면 정상. `Ctrl+C` 로 종료.

### 4. 대시보드 보기

브라우저에서:

```
http://localhost:3000
```

좌측 햄버거 메뉴 → **Dashboards** → **NpcapPractice Overview**.

(익명 viewer 권한이 활성화되어 로그인 없이도 보인다. 편집/관리 권한이 필요하면 `admin` / `admin` 으로 로그인.)

기본 시간 범위는 `now-15m`, 자동 새로고침 10초. 캡처 시작 후 첫 윈도우가 닫히는 데 약 15초 (window 10s + grace 5s) 걸린다.

### 5. 종료

```powershell
# capture-agent 창에서 Ctrl+C 로 캡처 중단

# 스택 정지 (DB 데이터 + 대시보드 보존)
docker compose stop

# 또는 완전 정리 (tsdb 볼륨까지 삭제)
docker compose down -v
```

## 검증 명령

DB 직접 조회:
```powershell
docker compose exec timescaledb psql -U pkt -d pkt
```

```sql
-- 데이터 시간 범위 확인
SELECT MIN(time), MAX(time), COUNT(*) FROM flows;

-- 행 수 한 눈에
SELECT 'flows'         t, count(*) FROM flows
UNION ALL SELECT 'protocol_stats', count(*) FROM protocol_stats
UNION ALL SELECT 'top_talkers',    count(*) FROM top_talkers
UNION ALL SELECT 'port_traffic',   count(*) FROM port_traffic
UNION ALL SELECT 'dns_queries',    count(*) FROM dns_queries
UNION ALL SELECT 'http_requests',  count(*) FROM http_requests
UNION ALL SELECT 'tls_sni',        count(*) FROM tls_sni;

-- 최근 5분간 TLS SNI top
SELECT sni, COUNT(*) AS hellos FROM tls_sni
WHERE time > now() - interval '5 minutes'
GROUP BY sni ORDER BY hellos DESC LIMIT 10;
```

Redis stream 상태:
```powershell
docker compose exec redis redis-cli XLEN raw_packets
docker compose exec redis redis-cli XLEN parsed_events
docker compose exec redis redis-cli XINFO GROUPS raw_packets
docker compose exec redis redis-cli XINFO GROUPS parsed_events
```

## 단계별 로드맵

| 단계 | 산출물 | 상태 |
| --- | --- | --- |
| **1. capture-agent** | C++로 NIC 열거/선택, BPF 필터 적용, raw 프레임을 `raw_packets`에 push | 완료 |
| **2. analyzer** | dpkt 기반 L2~L4 파싱, `parsed_events` 발행 | 완료 |
| **3. aggregator + DB** | 10s 윈도우 집계, TimescaleDB hypertable upsert | 완료 |
| **4. L7 파싱** | DNS query / HTTP Host·method·path / TLS SNI 추출 | 완료 |
| **5. Grafana** | 대시보드 9개 프로비저닝 | 완료 |
| **6. (스트레치)** | drop counter, pcap 파일 replay 모드, capture-agent 핫패스 최적화 | TODO |

## 비기능적 메모

- **캡처 무손실은 보장하지 않는다.** Redis 백프레셔 / 스냅 길이 / BPF 필터로 부하를 제어한다.
- **개인 PC / 본인이 권한을 가진 네트워크에서만** 캡처한다. 사내·공용망 무단 캡처 금지.
- L7 파싱은 평문 트래픽에 한한다 (HTTPS 본문은 파싱하지 않으며 SNI만 추출).
- Grafana 익명 viewer는 로컬 dev 편의를 위한 설정이라, Grafana를 외부에 노출할 거면 `GF_AUTH_ANONYMOUS_ENABLED` 를 끄자.
