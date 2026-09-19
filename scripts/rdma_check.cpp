// Two local processes, independent verbs resources, one checked RDMA WRITE.
// Built and supervised by doctor.py; no dependency on the Engine or its protocol.

#include <infiniband/verbs.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>

constexpr size_t payload_size = 4096;
constexpr size_t guard_size = 64;
constexpr size_t buffer_size = payload_size + 2 * guard_size;
constexpr uint8_t guard_value = 0xa5;

struct Resources {
    ibv_context *context = nullptr;
    ibv_pd *pd = nullptr;
    ibv_mr *mr = nullptr;
    ibv_cq *cq = nullptr;
    ibv_qp *qp = nullptr;
    ibv_port_attr port{};
    ibv_gid gid{};
    alignas(4096) uint8_t buffer[buffer_size]{};
};

// Exchanged only between instances of this executable on the same Linux host.
struct Metadata {
    uint64_t address;
    uint32_t rkey;
    uint32_t qpn;
    uint32_t psn;
    uint16_t lid;
    ibv_gid gid;
};

static void check(int error, const char *operation) {
    if (error != 0) {
        if (error < 0)
            error = errno ? errno : EIO;
        throw std::runtime_error(std::string(operation) + ": " + std::strerror(error) + " (" +
                                 std::to_string(error) + ")");
    }
}

static void check_pointer(const void *pointer, const char *operation) {
    if (!pointer) {
        check(errno ? errno : EIO, operation);
    }
}

static int number(const char *text, int minimum, int maximum) {
    char *end = nullptr;
    errno = 0;
    long value = std::strtol(text, &end, 10);
    if (errno || end == text || *end || value < minimum || value > maximum) {
        throw std::runtime_error(std::string("invalid numeric argument: ") + text);
    }
    return static_cast<int>(value);
}

static uint8_t pattern(size_t index) { return static_cast<uint8_t>(17 + index * 37 + index / 251); }

// This socket carries metadata and readiness/completion tokens, never the payload.
static void send_bytes(int fd, const void *data, size_t size) {
    auto *cursor = static_cast<const uint8_t *>(data);
    while (size) {
        ssize_t count = send(fd, cursor, size, MSG_NOSIGNAL);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            check(count < 0 ? errno : EPIPE, "control send");
        }
        cursor += count;
        size -= static_cast<size_t>(count);
    }
}

static void receive_bytes(int fd, void *data, size_t size) {
    auto *cursor = static_cast<uint8_t *>(data);
    while (size) {
        ssize_t count = recv(fd, cursor, size, 0);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            check(count < 0 ? errno : ECONNRESET, "control receive (peer exited or timed out)");
        }
        cursor += count;
        size -= static_cast<size_t>(count);
    }
}

static void receive_token(int fd, char expected) {
    char token = 0;
    receive_bytes(fd, &token, 1);
    if (token != expected) {
        throw std::runtime_error("unexpected control token");
    }
}

static void create_resources(Resources &r, const char *device_name, uint8_t port, int gid_index) {
    int count = 0;
    ibv_device **devices = ibv_get_device_list(&count);
    check_pointer(devices, "ibv_get_device_list");
    bool found = false;
    int open_error = 0;
    for (int i = 0; i < count; ++i) {
        if (std::strcmp(ibv_get_device_name(devices[i]), device_name) == 0) {
            found = true;
            r.context = ibv_open_device(devices[i]);
            open_error = errno;
            break;
        }
    }
    ibv_free_device_list(devices);
    if (!found) {
        throw std::runtime_error(std::string("RDMA device not found: ") + device_name);
    }
    if (!r.context) {
        check(open_error ? open_error : EIO, "ibv_open_device");
    }
    check(ibv_query_port(r.context, port, &r.port), "ibv_query_port");
    if (r.port.state != IBV_PORT_ACTIVE) {
        throw std::runtime_error("selected port is not ACTIVE");
    }
    if (gid_index >= r.port.gid_tbl_len) {
        throw std::runtime_error("GID index is outside the selected port's GID table");
    }
    check(ibv_query_gid(r.context, port, gid_index, &r.gid), "ibv_query_gid");
    const ibv_gid zero{};
    if (std::memcmp(&r.gid, &zero, sizeof(zero)) == 0) {
        throw std::runtime_error(
            "selected GID is zero; select an address assigned to the interface");
    }

    r.pd = ibv_alloc_pd(r.context);
    check_pointer(r.pd, "ibv_alloc_pd");
    r.mr = ibv_reg_mr(
        r.pd, r.buffer, sizeof(r.buffer), IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE);
    check_pointer(r.mr, "ibv_reg_mr (check memlock and device permissions)");
    r.cq = ibv_create_cq(r.context, 4, nullptr, nullptr, 0);
    check_pointer(r.cq, "ibv_create_cq");
    ibv_qp_init_attr init{};
    init.qp_type = IBV_QPT_RC;
    init.send_cq = r.cq;
    init.recv_cq = r.cq;
    init.cap.max_send_wr = 1;
    init.cap.max_recv_wr = 1;
    init.cap.max_send_sge = 1;
    init.cap.max_recv_sge = 1;
    // No inline capability or READ/Atomic resources are required by this check.
    r.qp = ibv_create_qp(r.pd, &init);
    check_pointer(r.qp, "ibv_create_qp");
}

static void
connect_qp(Resources &r, uint8_t port, int gid_index, const Metadata &peer, uint32_t psn) {
    ibv_qp_attr attr{};
    attr.qp_state = IBV_QPS_INIT;
    attr.port_num = port;
    attr.pkey_index = 0;
    attr.qp_access_flags = IBV_ACCESS_REMOTE_WRITE;
    check(ibv_modify_qp(
              r.qp, &attr, IBV_QP_STATE | IBV_QP_PORT | IBV_QP_PKEY_INDEX | IBV_QP_ACCESS_FLAGS),
          "ibv_modify_qp INIT");

    attr = {};
    attr.qp_state = IBV_QPS_RTR;
    attr.path_mtu = std::min(r.port.active_mtu, IBV_MTU_1024);
    attr.dest_qp_num = peer.qpn;
    attr.rq_psn = peer.psn;
    attr.max_dest_rd_atomic = 0;
    attr.min_rnr_timer = 12;
    attr.ah_attr.dlid = peer.lid;
    attr.ah_attr.port_num = port;
    attr.ah_attr.is_global = 1;
    attr.ah_attr.grh.dgid = peer.gid;
    attr.ah_attr.grh.sgid_index = static_cast<uint8_t>(gid_index);
    attr.ah_attr.grh.hop_limit = 1;
    check(ibv_modify_qp(r.qp,
                        &attr,
                        IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU | IBV_QP_DEST_QPN |
                            IBV_QP_RQ_PSN | IBV_QP_MAX_DEST_RD_ATOMIC | IBV_QP_MIN_RNR_TIMER),
          "ibv_modify_qp RTR");

    attr = {};
    attr.qp_state = IBV_QPS_RTS;
    attr.sq_psn = psn;
    attr.max_rd_atomic = 0;
    attr.timeout = 14;
    attr.retry_cnt = 3;
    attr.rnr_retry = 3;
    check(ibv_modify_qp(r.qp,
                        &attr,
                        IBV_QP_STATE | IBV_QP_SQ_PSN | IBV_QP_MAX_QP_RD_ATOMIC | IBV_QP_TIMEOUT |
                            IBV_QP_RETRY_CNT | IBV_QP_RNR_RETRY),
          "ibv_modify_qp RTS");
}

static void write_and_wait(Resources &r, const Metadata &peer, int timeout) {
    ibv_sge sge{};
    sge.addr = reinterpret_cast<uintptr_t>(r.buffer + guard_size);
    sge.length = payload_size;
    sge.lkey = r.mr->lkey;
    ibv_send_wr wr{};
    wr.wr_id = 1;
    wr.opcode = IBV_WR_RDMA_WRITE;
    wr.send_flags = IBV_SEND_SIGNALED;
    wr.sg_list = &sge;
    wr.num_sge = 1;
    wr.wr.rdma.remote_addr = peer.address;
    wr.wr.rdma.rkey = peer.rkey;
    ibv_send_wr *bad = nullptr;
    check(ibv_post_send(r.qp, &wr, &bad), "ibv_post_send WRITE");
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(timeout);
    for (;;) {
        ibv_wc wc{};
        int count = ibv_poll_cq(r.cq, 1, &wc);
        if (count < 0) {
            throw std::runtime_error("ibv_poll_cq returned " + std::to_string(count));
        }
        if (count) {
            if (wc.status != IBV_WC_SUCCESS) {
                throw std::runtime_error(std::string("CQE: ") + ibv_wc_status_str(wc.status) +
                                         " status=" + std::to_string(wc.status) +
                                         " wr_id=" + std::to_string(wc.wr_id) +
                                         " qp_num=" + std::to_string(wc.qp_num) +
                                         " vendor_err=" + std::to_string(wc.vendor_err));
            }
            if (wc.wr_id != wr.wr_id || wc.opcode != IBV_WC_RDMA_WRITE) {
                throw std::runtime_error("unexpected successful CQE");
            }
            return;
        }
        if (std::chrono::steady_clock::now() >= deadline) {
            throw std::runtime_error("timed out waiting for WRITE CQE");
        }
        usleep(1000);
    }
}

static void verify(const Resources &r) {
    std::atomic_thread_fence(std::memory_order_acquire);
    const volatile uint8_t *buffer = r.buffer;
    for (size_t i = 0; i < buffer_size; ++i) {
        const bool payload = i >= guard_size && i < guard_size + payload_size;
        const uint8_t expected = payload ? pattern(i - guard_size) : guard_value;
        const uint8_t actual = buffer[i];
        if (actual != expected) {
            throw std::runtime_error(std::string(payload ? "payload" : "guard") +
                                     " mismatch at buffer offset " + std::to_string(i) +
                                     ": expected=" + std::to_string(expected) +
                                     " actual=" + std::to_string(actual));
        }
    }
}

static int cleanup(Resources &r, const char *role) {
    int result = 0;
    auto release = [&](int error, const char *operation) {
        if (error) {
            std::fprintf(stderr,
                         "[%s] cleanup: %s: %s (%d)\n",
                         role,
                         operation,
                         std::strerror(error),
                         error);
            result = 1;
        }
    };
    if (r.qp)
        release(ibv_destroy_qp(r.qp), "ibv_destroy_qp");
    if (r.mr)
        release(ibv_dereg_mr(r.mr), "ibv_dereg_mr");
    if (r.cq)
        release(ibv_destroy_cq(r.cq), "ibv_destroy_cq");
    if (r.pd)
        release(ibv_dealloc_pd(r.pd), "ibv_dealloc_pd");
    if (r.context)
        release(ibv_close_device(r.context), "ibv_close_device");
    return result;
}

static int
run_endpoint(int fd, bool sender, const char *device, uint8_t port, int gid, int timeout) {
    const char *role = sender ? "sender" : "receiver";
    const char *stage = "resources";
    Resources r;
    int result = 0;
    try {
        const timeval limit{timeout, 0};
        if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &limit, sizeof(limit)) != 0 ||
            setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &limit, sizeof(limit)) != 0) {
            check(errno, "control socket timeout");
        }
        std::printf("[%s] resources: opening %s port=%u gid_index=%d pid=%ld\n",
                    role,
                    device,
                    static_cast<unsigned>(port),
                    gid,
                    static_cast<long>(getpid()));
        std::memset(r.buffer, guard_value, sizeof(r.buffer));
        for (size_t i = 0; i < payload_size; ++i) {
            r.buffer[guard_size + i] = sender ? pattern(i) : 0xcc;
        }
        create_resources(r, device, port, gid);
        std::printf("[%s] resources: PASS (device, PD, MR, CQ, RC QP)\n", role);

        stage = "connection";
        std::printf("[%s] connection: exchanging metadata and moving QP to RTS\n", role);
        const uint32_t psn = static_cast<uint32_t>(getpid()) & 0xffffff;
        Metadata local{};
        local.address = reinterpret_cast<uintptr_t>(r.buffer + guard_size);
        local.rkey = r.mr->rkey;
        local.qpn = r.qp->qp_num;
        local.psn = psn;
        local.lid = r.port.lid;
        local.gid = r.gid;
        Metadata peer{};
        send_bytes(fd, &local, sizeof(local));
        receive_bytes(fd, &peer, sizeof(peer));
        connect_qp(r, port, gid, peer, psn);
        send_bytes(fd, "R", 1);
        receive_token(fd, 'R');
        std::printf(
            "[%s] connection: PASS (local QPN=%u, peer QPN=%u)\n", role, local.qpn, peer.qpn);

        stage = "WRITE";
        if (sender) {
            std::printf("[%s] WRITE: posting 4096 bytes and waiting for CQE\n", role);
            write_and_wait(r, peer, timeout);
            std::printf("[%s] WRITE: PASS (successful CQE)\n", role);
            send_bytes(fd, "D", 1);
            stage = "peer verification";
            receive_token(fd, 'V');
        } else {
            std::printf("[%s] WRITE: waiting for sender completion\n", role);
            receive_token(fd, 'D');
            stage = "verification";
            verify(r);
            std::printf("[%s] verification: PASS (4096 bytes, both 64-byte guards intact)\n", role);
            send_bytes(fd, "V", 1);
        }
    } catch (const std::exception &error) {
        std::fprintf(stderr, "[%s] %s: FAIL: %s\n", role, stage, error.what());
        result = 1;
    }
    result |= cleanup(r, role);
    close(fd);
    return result;
}

int main(int argc, char **argv) {
    if (argc != 5) {
        std::fprintf(stderr, "Usage: %s DEVICE PORT GID_INDEX TIMEOUT_SECONDS\n", argv[0]);
        return 2;
    }
    try {
        const auto port = static_cast<uint8_t>(number(argv[2], 1, 255));
        const int gid = number(argv[3], 0, 255);
        const int timeout = number(argv[4], 1, 3600);
        std::setvbuf(stdout, nullptr, _IOLBF, 0);
        int channels[2];
        if (socketpair(AF_UNIX, SOCK_STREAM, 0, channels) != 0) {
            check(errno, "socketpair");
        }
        // Fork before any verbs call; neither process inherits registered memory or QPs.
        pid_t child = fork();
        if (child < 0) {
            check(errno, "fork");
        }
        const bool sender = child != 0;
        close(channels[sender ? 1 : 0]);
        int result = run_endpoint(channels[sender ? 0 : 1], sender, argv[1], port, gid, timeout);
        if (sender) {
            int status = 0;
            pid_t waited;
            do {
                waited = waitpid(child, &status, 0);
            } while (waited < 0 && errno == EINTR);
            if (waited < 0) {
                check(errno, "waitpid");
            }
            if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
                std::fprintf(stderr, "[receiver] process failed: wait status=%d\n", status);
                result = 1;
            }
        }
        return result;
    } catch (const std::exception &error) {
        std::fprintf(stderr, "[startup] FAIL: %s\n", error.what());
        return 1;
    }
}
