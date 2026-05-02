#pragma once

#include <pcap.h>

#include <cstdint>
#include <functional>
#include <string>

struct PacketView {
    long long ts_ns;             // capture timestamp (ns since epoch)
    int cap_len;                 // bytes actually captured (== data length)
    int orig_len;                // original on-wire length
    const unsigned char* data;
};

class Capture {
public:
    using Handler = std::function<void(const PacketView&)>;

    static void list_interfaces();

    Capture() = default;
    ~Capture();

    Capture(const Capture&) = delete;
    Capture& operator=(const Capture&) = delete;

    bool open(const std::string& iface, int snaplen, bool promisc, std::string& err);
    bool set_filter(const std::string& expr, std::string& err);
    int  link_type() const;
    void run(Handler handler);   // blocks until stop() or pcap error
    void stop();                 // safe to call from another thread / signal

private:
    pcap_t* handle_ = nullptr;
};
