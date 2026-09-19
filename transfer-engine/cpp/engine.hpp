#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <variant>
#include <vector>

namespace rdma101 {

enum class ResultCode {
    Ok,
    Unimplemented,
    Invalid,
    Busy,
    IoError,
    Timeout,
    Unsupported,
    NoMemory,
    InvalidState,
    Canceled,
};

struct Config {
    uint32_t port;
    uint32_t gid_index;
    std::optional<std::string> device; // nullopt selects an available device.
};

enum class MemoryAccess : uint32_t {
    None = 0,
    LocalWrite = 1u << 0,
    RemoteWrite = 1u << 1,
    RemoteRead = 1u << 2,
    RemoteAtomic = 1u << 3,
};

constexpr MemoryAccess operator|(MemoryAccess a, MemoryAccess b) {
    return static_cast<MemoryAccess>(static_cast<uint32_t>(a) | static_cast<uint32_t>(b));
}

constexpr MemoryAccess operator&(MemoryAccess a, MemoryAccess b) {
    return static_cast<MemoryAccess>(static_cast<uint32_t>(a) & static_cast<uint32_t>(b));
}

using PeerId = uint64_t;
using BatchId = uint64_t;
using MemoryId = uint64_t;
using RemoteMemoryId = uint64_t;

struct Write {
    uint64_t remote_address;
    bool inline_data = false;
};
struct Read {
    uint64_t remote_address;
};
struct Send {
    bool inline_data = false;
};
struct Recv {};
struct WriteImm {
    uint64_t remote_address;
    uint32_t immediate;
    bool inline_data = false;
};
struct CompareSwap {
    uint64_t remote_address;
    uint64_t compare;
    uint64_t value;
};
struct FetchAdd {
    uint64_t remote_address;
    uint64_t value;
};
using Operation = std::variant<Write, Read, Send, Recv, WriteImm, CompareSwap, FetchAdd>;

// The descriptor is owned; its buffer remains borrowed until terminal status.
struct Request {
    PeerId peer;
    void *local_address;
    uint64_t length;
    Operation operation;
};

struct Message {
    uint64_t length;
};
struct WriteNotice {
    uint32_t immediate;
};
using ReceiveResult = std::variant<Message, WriteNotice>;

struct Pending {};
struct Completed {
    std::optional<ReceiveResult> receive;
};
struct Failed {
    ResultCode error;
};
using TransferStatus = std::variant<Pending, Completed, Failed>;

// Engine owns the metadata; it stays valid until this peer or the engine is destroyed.
struct PeerMetadata {
    PeerId peer = 0;
    std::span<const uint8_t> metadata;
};

// Metadata stays valid until this registration is deregistered or the engine is destroyed.
struct MemoryMetadata {
    MemoryId memory = 0;
    std::span<const uint8_t> metadata;
};

class Engine {
public:
    Engine() = default;
    Engine(const Engine &) = delete;
    Engine &operator=(const Engine &) = delete;

    // Release the engine's resources after stopping worker and device accesses.
    // Cleanup should also handle partial initialization and unmatched receives.
    ~Engine();

    // Create an engine with device resources and a background worker.
    static ResultCode create(Config config, std::unique_ptr<Engine> &engine);

    // Register application memory and return a fresh handle and its metadata.
    ResultCode register_local_memory(void *address,
                                     std::size_t length,
                                     MemoryAccess access,
                                     MemoryMetadata &result);

    // Deregister local memory without freeing the application's buffer.
    // Return Busy while accepted local tasks still use it, including queued work.
    // The application must already have stopped and drained all remote accesses.
    ResultCode deregister_local_memory(MemoryId memory);

    // Import a connected peer's memory registration and return a local handle.
    // Copy the metadata without modifying the QP; reuse identical live imports
    // and reject conflicting overlapping regions.
    ResultCode
    import_remote_memory(PeerId peer, std::span<const uint8_t> metadata, RemoteMemoryId &result);

    // Remove an imported descriptor; return Busy while an accepted task uses it.
    // This only changes local state, not the peer's MR or its other connections.
    ResultCode remove_remote_memory(RemoteMemoryId memory);

    // Create a peer and export its connection metadata.
    // Return a nonzero handle and nonempty, stable metadata; the RC QP starts in INIT.
    ResultCode create_peer(PeerMetadata &result);

    // Complete the local side of a connection using the remote peer's metadata.
    // Copy retained fields and move the QP to RTR/RTS; input bytes are borrowed for this call.
    ResultCode connect_peer(PeerId peer, std::span<const uint8_t> metadata);

    // Destroy a local peer and its imported memory descriptors.
    // Stop its work, including unmatched receives, and finish local cleanup.
    // After accesses end, publish Failed{Canceled} for tasks pending at shutdown.
    // On success destroy its QP/imports and invalidate its metadata and handle.
    // On cleanup failure keep the handle for retry. Also handle INIT and failed setup.
    ResultCode destroy_peer(PeerId peer);

    // Submit requests as a new batch for background execution and return its nonzero ID.
    // Either the whole batch is accepted or no tasks are accepted.
    ResultCode submit_transfer(std::vector<Request> requests, BatchId &batch);

    // Read the current status of every task in a batch, in submission order.
    // The output span must match the task count. This call does not advance transfers.
    // Completed/Failed mean device accesses have ended and buffers are no longer borrowed.
    ResultCode get_transfer_statuses(BatchId batch, std::span<TransferStatus> statuses) const;

    // Release the batch's task records. Return Busy while any task is Pending.
    ResultCode free_batch(BatchId batch);
};

} // namespace rdma101
