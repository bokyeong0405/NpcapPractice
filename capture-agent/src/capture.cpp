#include "capture.hpp"

#include <cstdio>

void Capture::list_interfaces() {
    pcap_if_t* alldevs = nullptr;
    char errbuf[PCAP_ERRBUF_SIZE] = {};
    if (pcap_findalldevs(&alldevs, errbuf) == -1) {
        std::fprintf(stderr, "pcap_findalldevs failed: %s\n", errbuf);
        return;
    }
    int idx = 0;
    for (pcap_if_t* d = alldevs; d != nullptr; d = d->next, ++idx) {
        std::printf("[%d] %s\n", idx, d->name ? d->name : "(unnamed)");
        if (d->description) {
            std::printf("    %s\n", d->description);
        }
    }
    if (idx == 0) {
        std::fprintf(stderr,
            "No interfaces found. Is Npcap installed and is this an elevated "
            "(Administrator) shell?\n");
    }
    pcap_freealldevs(alldevs);
}

Capture::~Capture() {
    if (handle_) {
        pcap_close(handle_);
        handle_ = nullptr;
    }
}

bool Capture::open(const std::string& iface, int snaplen, bool promisc, std::string& err) {
    char errbuf[PCAP_ERRBUF_SIZE] = {};
    handle_ = pcap_open_live(iface.c_str(), snaplen, promisc ? 1 : 0, /*to_ms*/ 100, errbuf);
    if (!handle_) {
        err = errbuf;
        return false;
    }
    return true;
}

bool Capture::set_filter(const std::string& expr, std::string& err) {
    if (!handle_) {
        err = "capture not open";
        return false;
    }
    bpf_program bpf{};
    if (pcap_compile(handle_, &bpf, expr.c_str(), /*optimize*/ 1, PCAP_NETMASK_UNKNOWN) < 0) {
        err = pcap_geterr(handle_);
        return false;
    }
    if (pcap_setfilter(handle_, &bpf) < 0) {
        err = pcap_geterr(handle_);
        pcap_freecode(&bpf);
        return false;
    }
    pcap_freecode(&bpf);
    return true;
}

int Capture::link_type() const {
    return handle_ ? pcap_datalink(handle_) : -1;
}

namespace {

struct LoopCtx {
    Capture::Handler* handler;
};

void on_packet(u_char* user, const pcap_pkthdr* h, const u_char* data) {
    auto* ctx = reinterpret_cast<LoopCtx*>(user);
    const long long ts_ns =
        static_cast<long long>(h->ts.tv_sec) * 1'000'000'000LL +
        static_cast<long long>(h->ts.tv_usec) * 1'000LL;
    const PacketView v{
        ts_ns,
        static_cast<int>(h->caplen),
        static_cast<int>(h->len),
        data
    };
    (*ctx->handler)(v);
}

} // namespace

void Capture::run(Handler handler) {
    if (!handle_) return;
    LoopCtx ctx{&handler};
    pcap_loop(handle_, /*cnt*/ -1, on_packet, reinterpret_cast<u_char*>(&ctx));
}

void Capture::stop() {
    if (handle_) {
        pcap_breakloop(handle_);
    }
}
