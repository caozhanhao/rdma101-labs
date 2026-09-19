#include "engine.hpp"
#include <infiniband/verbs.h>

namespace rdma101 {

ResultCode Engine::create(Config config, std::unique_ptr<Engine> &engine) {
    return ResultCode::Unimplemented;
}

Engine::~Engine() {}

ResultCode Engine::register_local_memory(void *address,
                                         std::size_t length,
                                         MemoryAccess access,
                                         MemoryMetadata &result) {
    return ResultCode::Unimplemented;
}

ResultCode Engine::deregister_local_memory(MemoryId memory) { return ResultCode::Unimplemented; }

ResultCode Engine::import_remote_memory(PeerId peer,
                                        std::span<const uint8_t> metadata,
                                        RemoteMemoryId &result) {
    return ResultCode::Unimplemented;
}

ResultCode Engine::remove_remote_memory(RemoteMemoryId memory) { return ResultCode::Unimplemented; }

ResultCode Engine::create_peer(PeerMetadata &result) { return ResultCode::Unimplemented; }

ResultCode Engine::connect_peer(PeerId peer, std::span<const uint8_t> metadata) {
    return ResultCode::Unimplemented;
}

ResultCode Engine::destroy_peer(PeerId peer) { return ResultCode::Unimplemented; }

ResultCode Engine::submit_transfer(std::vector<Request> requests, BatchId &batch) {
    return ResultCode::Unimplemented;
}

ResultCode Engine::get_transfer_statuses(BatchId batch, std::span<TransferStatus> statuses) const {
    return ResultCode::Unimplemented;
}

ResultCode Engine::free_batch(BatchId batch) { return ResultCode::Unimplemented; }

} // namespace rdma101
