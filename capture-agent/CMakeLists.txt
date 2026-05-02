cmake_minimum_required(VERSION 3.20)
project(capture-agent CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT WIN32)
  message(FATAL_ERROR "capture-agent supports Windows only (Npcap).")
endif()

# ---------------------------------------------------------------------------
# Npcap SDK location
#   1) -DNPCAP_SDK_DIR=...   (CMake cache)
#   2) %NPCAP_SDK_DIR%       (env)
#   3) ./third_party/npcap-sdk
# ---------------------------------------------------------------------------
if(NOT NPCAP_SDK_DIR AND DEFINED ENV{NPCAP_SDK_DIR})
  set(NPCAP_SDK_DIR "$ENV{NPCAP_SDK_DIR}")
endif()
if(NOT NPCAP_SDK_DIR)
  set(NPCAP_SDK_DIR "${CMAKE_CURRENT_SOURCE_DIR}/third_party/npcap-sdk")
endif()

if(NOT EXISTS "${NPCAP_SDK_DIR}/Include/pcap.h")
  message(FATAL_ERROR
    "Npcap SDK not found at ${NPCAP_SDK_DIR}.\n"
    "Download from https://npcap.com/#download and either extract under "
    "capture-agent/third_party/npcap-sdk or set NPCAP_SDK_DIR.")
endif()

if(CMAKE_SIZEOF_VOID_P EQUAL 8)
  set(NPCAP_LIB_DIR "${NPCAP_SDK_DIR}/Lib/x64")
else()
  set(NPCAP_LIB_DIR "${NPCAP_SDK_DIR}/Lib")
endif()

# ---------------------------------------------------------------------------
# hiredis via vcpkg
# ---------------------------------------------------------------------------
find_package(hiredis CONFIG REQUIRED)

# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------
add_executable(capture-agent
  src/main.cpp
  src/capture.cpp
  src/redis_sink.cpp
)

target_include_directories(capture-agent PRIVATE
  "${NPCAP_SDK_DIR}/Include"
  src
)

target_link_directories(capture-agent PRIVATE "${NPCAP_LIB_DIR}")

target_link_libraries(capture-agent PRIVATE
  wpcap
  Packet
  ws2_32
  hiredis::hiredis
)

target_compile_definitions(capture-agent PRIVATE
  WIN32_LEAN_AND_MEAN
  NOMINMAX
  WPCAP
  _CRT_SECURE_NO_WARNINGS
)

if(MSVC)
  target_compile_options(capture-agent PRIVATE /W4 /permissive-)
endif()
