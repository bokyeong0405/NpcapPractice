#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include "capture.hpp"
#include "redis_sink.hpp"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

namespace {

struct Args {
    bool list_ifaces = false;
    std::string iface;
    std::string filter = "ip";
    int snaplen = 65535;
    bool promisc = false;
    std::string redis_host = "127.0.0.1";
    int redis_port = 6379;
    std::string stream = "raw_packets";
    long long maxlen = 100000;
};

void print_usage() {
    std::printf(
        "Usage: capture-agent [options]\n"
        "  --list-ifaces              List available NPF interfaces and exit\n"
        "  --iface NAME               NPF device path (e.g. \\Device\\NPF_{GUID})\n"
        "  --filter EXPR              BPF filter expression (default: \"ip\")\n"
        "  --snaplen N                Snapshot length (default: 65535)\n"
        "  --promisc                  Enable promiscuous mode\n"
        "  --redis HOST:PORT          Redis address (default: 127.0.0.1:6379)\n"
        "  --stream NAME              Redis stream name (default: raw_packets)\n"
        "  --maxlen N                 XADD MAXLEN ~ value (default: 100000)\n"
        "  -h, --help                 Show this help\n"
    );
}

bool parse_redis(const std::string& s, std::string& host, int& port) {
    auto colon = s.find(':');
    if (colon == std::string::npos) return false;
    host = s.substr(0, colon);
    try {
        port = std::stoi(s.substr(colon + 1));
    } catch (...) {
        return false;
    }
    return host.size() > 0 && port > 0 && port < 65536;
}

bool parse_args(int argc, char** argv, Args& a) {
    for (int i = 1; i < argc; ++i) {
        const std::string k = argv[i];
        auto need = [&](const char* name) -> const char* {
            if (i + 1 >= argc) {
                std::fprintf(stderr, "%s requires a value\n", name);
                return nullptr;
            }
            return argv[++i];
        };

        if (k == "-h" || k == "--help") { print_usage(); std::exit(0); }
        else if (k == "--list-ifaces") { a.list_ifaces = true; }
        else if (k == "--iface")    { auto v = need("--iface");   if (!v) return false; a.iface = v; }
        else if (k == "--filter")   { auto v = need("--filter");  if (!v) return false; a.filter = v; }
        else if (k == "--snaplen")  { auto v = need("--snaplen"); if (!v) return false; a.snaplen = std::atoi(v); }
        else if (k == "--promisc")  { a.promisc = true; }
        else if (k == "--redis")    {
            auto v = need("--redis"); if (!v) return false;
            if (!parse_redis(v, a.redis_host, a.redis_port)) {
                std::fprintf(stderr, "--redis must be HOST:PORT\n");
                return false;
            }
        }
        else if (k == "--stream")   { auto v = need("--stream"); if (!v) return false; a.stream = v; }
        else if (k == "--maxlen")   { auto v = need("--maxlen"); if (!v) return false; a.maxlen = std::atoll(v); }
        else {
            std::fprintf(stderr, "Unknown option: %s\n", k.c_str());
            return false;
        }
    }
    return true;
}

Capture* g_capture = nullptr;

BOOL WINAPI on_console_ctrl(DWORD type) {
    if (type == CTRL_C_EVENT || type == CTRL_BREAK_EVENT || type == CTRL_CLOSE_EVENT) {
        if (g_capture) g_capture->stop();
        return TRUE;
    }
    return FALSE;
}

} // namespace

int main(int argc, char** argv) {
    Args a;
    if (!parse_args(argc, argv, a)) {
        print_usage();
        return 2;
    }

    if (a.list_ifaces) {
        Capture::list_interfaces();
        return 0;
    }

    if (a.iface.empty()) {
        std::fprintf(stderr,
            "Missing --iface. Use --list-ifaces to see available NPF devices.\n");
        return 2;
    }
    if (a.snaplen <= 0 || a.snaplen > 65535) {
        std::fprintf(stderr, "--snaplen must be in (0, 65535]\n");
        return 2;
    }

    Capture cap;
    std::string err;
    if (!cap.open(a.iface, a.snaplen, a.promisc, err)) {
        std::fprintf(stderr, "Failed to open %s: %s\n", a.iface.c_str(), err.c_str());
        return 1;
    }
    if (!cap.set_filter(a.filter, err)) {
        std::fprintf(stderr, "Failed to set filter \"%s\": %s\n", a.filter.c_str(), err.c_str());
        return 1;
    }

    RedisSink sink;
    if (!sink.connect(a.redis_host, a.redis_port, err)) {
        std::fprintf(stderr, "Redis connect failed (%s:%d): %s\n",
            a.redis_host.c_str(), a.redis_port, err.c_str());
        return 1;
    }

    g_capture = &cap;
    SetConsoleCtrlHandler(on_console_ctrl, TRUE);

    std::printf(
        "[capture-agent] iface=%s filter=\"%s\" snaplen=%d promisc=%d link_type=%d\n"
        "[capture-agent] redis=%s:%d stream=%s maxlen=%lld\n"
        "[capture-agent] running. Ctrl+C to stop.\n",
        a.iface.c_str(), a.filter.c_str(), a.snaplen, a.promisc ? 1 : 0, cap.link_type(),
        a.redis_host.c_str(), a.redis_port, a.stream.c_str(), a.maxlen
    );

    long long pushed = 0;
    long long dropped = 0;

    cap.run([&](const PacketView& p) {
        std::string xerr;
        if (!sink.xadd_packet(a.stream, p.ts_ns, a.iface, p.cap_len,
                              p.data, p.cap_len, a.maxlen, xerr)) {
            ++dropped;
            if (dropped == 1 || dropped % 100 == 0) {
                std::fprintf(stderr,
                    "[warn] XADD failed (dropped=%lld): %s\n", dropped, xerr.c_str());
            }
            return;
        }
        ++pushed;
        if (pushed % 1000 == 0) {
            std::printf("[capture-agent] pushed=%lld dropped=%lld\n", pushed, dropped);
        }
    });

    std::printf("[capture-agent] stopped. pushed=%lld dropped=%lld\n", pushed, dropped);
    return 0;
}
