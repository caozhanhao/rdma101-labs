// Test double for the C++ adapter. No device operations or data transfers.
#include "engine.hpp"
#include <algorithm>
#include <utility>

using namespace rdma101;
namespace {
std::string metadata;
void *buffer = nullptr;
std::vector<Request> saved;
uint64_t submissions = 0;
constexpr uint64_t remote = 0xfedcba9876543210;
constexpr uint64_t compare = 0x1122334455667788;
constexpr uint64_t value = 0xffeeddccbbaa0099;
constexpr uint8_t memory_metadata[] = {0, 255, 'M', 'R', 128};
} // namespace

ResultCode Engine::create(Config config, std::unique_ptr<Engine> &engine) {
    metadata = config.device.value_or("<auto>");
    for (auto n : {config.port, config.gid_index}) {
        metadata += "|" + std::to_string(n);
    }
    buffer = nullptr;
    saved.clear();
    submissions = 0;
    engine = std::make_unique<Engine>();
    return ResultCode::Ok;
}
Engine::~Engine() = default;

ResultCode Engine::register_local_memory(void *address,
                                         std::size_t length,
                                         MemoryAccess access,
                                         MemoryMetadata &output) {
    unsigned mask = 0;
    if ((access & MemoryAccess::LocalWrite) != MemoryAccess::None)
        mask |= 1;
    if ((access & MemoryAccess::RemoteWrite) != MemoryAccess::None)
        mask |= 2;
    if ((access & MemoryAccess::RemoteRead) != MemoryAccess::None)
        mask |= 4;
    if ((access & MemoryAccess::RemoteAtomic) != MemoryAccess::None)
        mask |= 8;
    buffer = address;
    output = {77, memory_metadata};
    return length == mask + 1 ? ResultCode::Ok : ResultCode::Invalid;
}
ResultCode Engine::deregister_local_memory(MemoryId memory) {
    return memory == 77 ? ResultCode::Ok : memory == 78 ? ResultCode::Busy : ResultCode::Invalid;
}
ResultCode
Engine::import_remote_memory(PeerId peer, std::span<const uint8_t> bytes, RemoteMemoryId &output) {
    output = 91;
    return peer == 41 && bytes.size() == sizeof(memory_metadata) &&
                   std::equal(bytes.begin(), bytes.end(), memory_metadata)
               ? ResultCode::Ok
               : ResultCode::Invalid;
}
ResultCode Engine::remove_remote_memory(RemoteMemoryId memory) {
    return memory == 91 ? ResultCode::Ok : memory == 92 ? ResultCode::Busy : ResultCode::Invalid;
}
ResultCode Engine::create_peer(PeerMetadata &output) {
    output = {41, {reinterpret_cast<const uint8_t *>(metadata.data()), metadata.size()}};
    return ResultCode::Ok;
}
ResultCode Engine::connect_peer(PeerId peer, std::span<const uint8_t> bytes) {
    return peer == 41 && bytes.size() == metadata.size() &&
                   std::equal(bytes.begin(),
                              bytes.end(),
                              reinterpret_cast<const uint8_t *>(metadata.data()))
               ? ResultCode::Ok
               : ResultCode::Invalid;
}

ResultCode Engine::destroy_peer(PeerId peer) {
    return peer == 41 ? ResultCode::Ok : peer == 42 ? ResultCode::IoError : ResultCode::Invalid;
}

ResultCode Engine::submit_transfer(std::vector<Request> requests, BatchId &batch) {
    ++submissions;
    if (requests.size() != 8)
        return ResultCode::Invalid;
    for (const auto &request : requests) {
        if (request.peer != 41 || request.local_address != buffer || request.length != 8)
            return ResultCode::Invalid;
    }
    const auto *write = std::get_if<Write>(&requests[0].operation);
    const auto *read = std::get_if<Read>(&requests[1].operation);
    const auto *send = std::get_if<Send>(&requests[2].operation);
    const auto *imm = std::get_if<WriteImm>(&requests[4].operation);
    const auto *cas = std::get_if<CompareSwap>(&requests[5].operation);
    const auto *faa = std::get_if<FetchAdd>(&requests[6].operation);
    const auto *plain_send = std::get_if<Send>(&requests[7].operation);
    if (!write || write->remote_address != remote || !write->inline_data || !read ||
        read->remote_address != remote || !send || !send->inline_data ||
        !std::holds_alternative<Recv>(requests[3].operation) || !imm ||
        imm->remote_address != remote || imm->immediate != 0x89abcdef || !imm->inline_data ||
        !cas || cas->remote_address != remote || cas->compare != compare || cas->value != value ||
        !faa || faa->remote_address != remote || faa->value != value || !plain_send ||
        plain_send->inline_data)
        return ResultCode::Invalid;
    saved = std::move(requests);
    batch = submissions;
    return ResultCode::Ok;
}

ResultCode Engine::get_transfer_statuses(BatchId batch, std::span<TransferStatus> output) const {
    if (!output.empty())
        output[0] = Completed{};
    if (batch != submissions || output.size() != saved.size() || saved.empty())
        return ResultCode::Invalid;
    const TransferStatus states[] = {
        Pending{},
        Failed{ResultCode::IoError},
        Completed{Message{saved[2].length}},
        Completed{WriteNotice{std::get<WriteImm>(saved[4].operation).immediate}},
        Failed{ResultCode::Canceled},
        Failed{ResultCode::Unsupported},
        Failed{ResultCode::NoMemory},
        Completed{},
    };
    std::copy(std::begin(states), std::end(states), output.begin());
    return ResultCode::Ok;
}
ResultCode Engine::free_batch(BatchId batch) {
    const ResultCode errors[] = {ResultCode::Unimplemented,
                                 ResultCode::Invalid,
                                 ResultCode::Busy,
                                 ResultCode::IoError,
                                 ResultCode::Timeout,
                                 ResultCode::Unsupported,
                                 ResultCode::NoMemory,
                                 ResultCode::InvalidState,
                                 ResultCode::Canceled};
    return batch <= std::size(errors) ? errors[batch - 1] : ResultCode::Ok;
}
