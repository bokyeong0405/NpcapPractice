#include "redis_sink.hpp"

#include <hiredis/hiredis.h>

#include "base64.hpp"

RedisSink::~RedisSink() {
    if (ctx_) {
        redisFree(ctx_);
        ctx_ = nullptr;
    }
}

bool RedisSink::connect(const std::string& host, int port, std::string& err) {
    ctx_ = redisConnect(host.c_str(), port);
    if (!ctx_) {
        err = "redisConnect returned null";
        return false;
    }
    if (ctx_->err) {
        err = ctx_->errstr;
        redisFree(ctx_);
        ctx_ = nullptr;
        return false;
    }
    return true;
}

bool RedisSink::xadd_packet(const std::string& stream,
                            long long ts_ns,
                            const std::string& iface,
                            int cap_len,
                            const unsigned char* data,
                            int data_len,
                            long long maxlen,
                            std::string& err) {
    if (!ctx_) {
        err = "redis not connected";
        return false;
    }

    const std::string b64 = base64_encode(data, static_cast<std::size_t>(data_len));

    auto* reply = static_cast<redisReply*>(redisCommand(
        ctx_,
        "XADD %s MAXLEN ~ %lld * ts_ns %lld iface %s snap_len %d raw_b64 %b",
        stream.c_str(),
        maxlen,
        ts_ns,
        iface.c_str(),
        cap_len,
        b64.data(), static_cast<size_t>(b64.size())
    ));

    if (!reply) {
        err = ctx_->errstr;
        return false;
    }
    bool ok = true;
    if (reply->type == REDIS_REPLY_ERROR) {
        err = reply->str ? reply->str : "XADD error";
        ok = false;
    }
    freeReplyObject(reply);
    return ok;
}
