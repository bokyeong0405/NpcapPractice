# capture-agent

Windows 호스트에서 네이티브로 도는 Npcap 캡처 프로세스. 선택한 NIC에서 raw 프레임을 잡아 base64로 인코딩한 뒤 Redis Stream `raw_packets`에 push 한다.

## 책임 범위

- NIC 열거 / 선택 (`pcap_findalldevs`)
- BPF 필터 적용
- 프레임 캡처 (`pcap_loop`)
- Redis Stream `raw_packets` 에 `XADD MAXLEN ~` 로 발행

L3/L4/L7 파싱은 하지 않는다. 그건 컨테이너의 analyzer 책임.

## 의존성

| 항목 | 용도 |
| --- | --- |
| Npcap (런타임) | 커널 드라이버 — WinPcap 호환 모드 권장 |
| Npcap SDK | `pcap.h`, `wpcap.lib`, `Packet.lib` |
| Visual Studio 2022 Build Tools | MSVC, C++17 |
| CMake 3.20+ | 빌드 |
| vcpkg | hiredis 의존성 관리 |

## 사전 준비

### 1. Npcap 설치

[npcap.com](https://npcap.com/#download) 에서 설치. WinPcap 호환 모드 체크 권장.

### 2. Npcap SDK 배치

[npcap.com](https://npcap.com/#download) 에서 SDK zip 다운로드 후, 다음 중 하나로 배치:

- **A. 프로젝트 안에 두기** — `capture-agent/third_party/npcap-sdk/` 로 압축 해제 (Include/, Lib/ 가 그 안에 있어야 함)
- **B. 환경 변수** — `NPCAP_SDK_DIR` 에 SDK 루트 경로 설정
- **C. CMake 인자** — `-DNPCAP_SDK_DIR=C:\path\to\npcap-sdk`

### 3. vcpkg 부트스트랩

```powershell
git clone https://github.com/microsoft/vcpkg.git C:\dev\vcpkg
C:\dev\vcpkg\bootstrap-vcpkg.bat
$env:VCPKG_ROOT = "C:\dev\vcpkg"
[Environment]::SetEnvironmentVariable("VCPKG_ROOT", "C:\dev\vcpkg", "User")
```

`hiredis` 는 `vcpkg.json` 매니페스트로 선언되어 있어 CMake configure 시 자동 설치된다.

## 빌드

```powershell
cd capture-agent
cmake --preset default
cmake --build build --config Release
```

산출물: `capture-agent\build\Release\capture-agent.exe`

## Redis 띄우기 (테스트용)

전체 docker-compose 는 다음 단계에서 추가될 예정. 1단계 단독 검증은 redis 컨테이너 하나면 충분:

```powershell
docker run --rm -d --name redis-pkt -p 6379:6379 redis:7-alpine
```

## 실행

> Npcap 드라이버 호출에 관리자 권한이 필요하다. **관리자 PowerShell** 에서 실행할 것.

### 인터페이스 열거

```powershell
.\build\Release\capture-agent.exe --list-ifaces
```

출력 예:
```
[0] \Device\NPF_{3F1A...-...}
    Intel(R) Wi-Fi 6 AX201
[1] \Device\NPF_Loopback
    Adapter for loopback traffic capture
```

### 캡처 시작

```powershell
.\build\Release\capture-agent.exe `
    --iface "\Device\NPF_{3F1A...-...}" `
    --filter "ip" `
    --redis 127.0.0.1:6379 `
    --stream raw_packets
```

`Ctrl+C` 로 종료.

### 옵션

| 플래그 | 기본값 | 설명 |
| --- | --- | --- |
| `--list-ifaces` | - | NPF 디바이스 목록 출력 후 종료 |
| `--iface NAME` | (필수) | NPF 디바이스 경로 |
| `--filter EXPR` | `ip` | BPF 필터 |
| `--snaplen N` | `65535` | 캡처 길이 상한 |
| `--promisc` | off | 프로미스큐어스 모드 |
| `--redis HOST:PORT` | `127.0.0.1:6379` | Redis 주소 |
| `--stream NAME` | `raw_packets` | Redis stream 이름 |
| `--maxlen N` | `100000` | XADD MAXLEN ~ (대략적 상한) |

## Redis Stream 페이로드

```
XADD raw_packets MAXLEN ~ <maxlen> *
    ts_ns <epoch_ns>
    iface <\Device\NPF_{...}>
    snap_len <captured_bytes>
    raw_b64 <base64_of_frame>
```

## 검증

### Stream 길이 확인
```powershell
docker exec -it redis-pkt redis-cli XLEN raw_packets
```

### 최신 1건 들여다보기
```powershell
docker exec -it redis-pkt redis-cli XREVRANGE raw_packets + - COUNT 1
```

`raw_b64` 값을 디코드해서 16진 덤프로 보고 싶다면 (PowerShell 7+):
```powershell
$b64 = docker exec redis-pkt redis-cli XREVRANGE raw_packets + - COUNT 1 |
       Select-String -Pattern '^[A-Za-z0-9+/=]+$' | Select-Object -Last 1
[Convert]::FromBase64String($b64.ToString()) |
    ForEach-Object { '{0:X2}' -f $_ } |
    Select-Object -First 64
```

## 알려진 한계 (v0)

- 단일 스레드 — XADD가 캡처 콜백 안에서 동기 호출. 고트래픽 환경에선 드롭 발생 가능.
- Redis 재연결 로직 없음. 끊기면 드롭 카운터만 증가.
- DLT 는 stream 에 싣지 않음. analyzer 는 일단 Ethernet (`DLT_EN10MB`) 가정.
- IPv6 도 BPF `ip` 필터로는 잡히지 않음 — IPv6 까지 보려면 `--filter "ip or ip6"`.

## 디렉토리

```
capture-agent/
├─ CMakeLists.txt
├─ CMakePresets.json
├─ vcpkg.json
├─ README.md
└─ src/
   ├─ main.cpp
   ├─ capture.hpp / .cpp
   ├─ redis_sink.hpp / .cpp
   └─ base64.hpp
```
