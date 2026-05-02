#pragma once

#include <cstdint>
#include <string>

struct redisContext;

class RedisSink {
public:
    RedisSink() = default;
    ~RedisSink();

    RedisSink(const RedisSink&) = delete;
    RedisSink& operator=(const RedisSink&) = delete;

    bool connect(const std::string& host, int port, std::string& err);

    // XADD <stream> MAXLEN ~ <maxlen> * ts_ns <ts> iface <iface> snap_len <n> raw_b64 <b64>
    bool xadd_packet(const std::string& stream,
                     long long ts_ns,
                     const std::string& iface,
                     int cap_len,
                     const unsigned char* data,
                     int data_len,
                     long long maxlen,
                     std::string& err);

private:
    redisContext* ctx_ = nullptr;
};
