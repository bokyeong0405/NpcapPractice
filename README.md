# NpcapPractice

Npcap으로 로컬 NIC에서 패킷을 캡처하고, Docker 컨테이너로 분리한 분석기 / 집계기 / DB가 이를 처리·저장하는 미니 패킷 캡처 & 분석 프로젝트.

## 개요

- **목표**: 호스트(Windows)의 NIC에서 실시간 패킷을 캡처해, 컨테이너로 분리된 분석 파이프라인에 흘려보내고 결과를 DB에 적재한다.
- **컨셉**: 캡처 계층(호스트)과 분석 계층(컨테이너)을 분리해, 캡처 성능과 분석 로직의 책임을 나눈다.
- **범위**: 학습용 미니 프로젝트. 운영 환경용 무손실/고성능 캡처는 목표가 아님.

## 아키텍처

```
[ 호스트 NIC ]
      │  (Npcap raw capture)
      ▼
[ Capturer (호스트 프로세스) ]
      │  TCP / UDP / Unix socket 등으로 전송
      ▼
┌─────────────── Docker Network ───────────────┐
│                                              │
│  [ Analyzer ]  →  [ Aggregator ]  →  [ DB ] │
│   패킷 파싱        통계/집계          영속화  │
│                                              │
└──────────────────────────────────────────────┘
```

### 컴포넌트

| 컴포넌트 | 위치 | 역할 |
| --- | --- | --- |
| Capturer | 호스트 | Npcap으로 NIC에서 raw packet 캡처 후 Analyzer로 전송 |
| Analyzer | 컨테이너 | 패킷 파싱(이더넷/IP/TCP/UDP 등) 후 구조화된 이벤트로 변환 |
| Aggregator | 컨테이너 | 일정 시간 윈도우 단위로 통계/집계 (트래픽량, 프로토콜 분포 등) |
| DB | 컨테이너 | 집계 결과 및 원본 메타데이터 영속화 |

## 요구 사항

- Windows 10/11 (Npcap 사용)
- [Npcap](https://npcap.com/) 설치 (WinPcap 호환 모드 권장)
- Docker Desktop
- (개발용) 사용 언어/런타임은 추후 결정

## 디렉토리 구조 (예정)

```
NpcapPractice/
├─ capturer/         # 호스트에서 도는 Npcap 캡처 프로세스
├─ analyzer/         # 패킷 파싱 컨테이너
├─ aggregator/       # 집계 컨테이너
├─ db/               # DB 초기화 스크립트 / 컴포즈 설정
├─ docker-compose.yml
└─ README.md
```

## 실행 (예정)

```bash
# 1. 분석 스택 기동
docker compose up -d

# 2. 호스트에서 캡처러 실행
./capturer --iface "이더넷" --target localhost:5555
```

## 로드맵

- [ ] Npcap 기반 캡처러 PoC
- [ ] Capturer ↔ Analyzer 전송 프로토콜 정의
- [ ] Analyzer 패킷 파서 구현
- [ ] Aggregator 집계 로직 및 윈도우 정의
- [ ] DB 스키마 설계 및 적재
- [ ] docker-compose 통합
- [ ] 간단한 대시보드/조회 인터페이스
