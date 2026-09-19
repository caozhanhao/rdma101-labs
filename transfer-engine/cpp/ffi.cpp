#include "engine.hpp"
#include "transfer_engine.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <limits>
#include <new>
#include <type_traits>
#include <utility>

using namespace rdma101;

namespace {

// Run an API operation without letting exceptions cross the C boundary.
// Unexpected exceptions abort: a partially executed operation may not have been rolled back.
template <typename F> r101_result call(F &&operation) noexcept {
    try {
        return operation();
    } catch (const std::exception &error) {
        std::fprintf(stderr, "rdma101: unexpected exception: %s\n", error.what());
        std::abort();
    } catch (...) {
        std::fputs("rdma101: unexpected exception\n", stderr);
        std::abort();
    }
}

r101_result encode_result(ResultCode result) {
    switch (result) {
    case ResultCode::Ok:
        return R101_OK;
    case ResultCode::Unimplemented:
        return R101_UNIMPLEMENTED;
    case ResultCode::Invalid:
        return R101_INVALID;
    case ResultCode::Busy:
        return R101_BUSY;
    case ResultCode::IoError:
        return R101_IO_ERROR;
    case ResultCode::Timeout:
        return R101_TIMEOUT;
    case ResultCode::Unsupported:
        return R101_UNSUPPORTED;
    case ResultCode::NoMemory:
        return R101_NO_MEMORY;
    case ResultCode::InvalidState:
        return R101_INVALID_STATE;
    case ResultCode::Canceled:
        return R101_CANCELED;
    }
    std::abort();
}

template <typename T> bool valid_array(const T *data, uint64_t count) {
    return data && count > 0 &&
           count <= static_cast<uint64_t>(std::numeric_limits<std::ptrdiff_t>::max()) / sizeof(T) &&
           reinterpret_cast<std::uintptr_t>(data) % alignof(T) == 0;
}

bool decode_access(r101_access_flags flags, MemoryAccess &access) {
    constexpr auto known =
        R101_LOCAL_WRITE | R101_REMOTE_WRITE | R101_REMOTE_READ | R101_REMOTE_ATOMIC;
    if (flags & ~known)
        return false;
    access = MemoryAccess::None;
    if (flags & R101_LOCAL_WRITE)
        access = access | MemoryAccess::LocalWrite;
    if (flags & R101_REMOTE_WRITE)
        access = access | MemoryAccess::RemoteWrite;
    if (flags & R101_REMOTE_READ)
        access = access | MemoryAccess::RemoteRead;
    if (flags & R101_REMOTE_ATOMIC)
        access = access | MemoryAccess::RemoteAtomic;
    return true;
}

// Convert a C request into a native Request after checking its opcode and flags.
bool decode_request(const r101_request &input, Request &output) {
    // Read the representation so unknown C enum values can be rejected safely.
    std::underlying_type_t<r101_opcode> opcode;
    std::memcpy(&opcode, &input.opcode, sizeof(opcode));
    if (input.flags != 0 && input.flags != R101_INLINE)
        return false;
    const bool inline_data = input.flags == R101_INLINE;
    const bool supports_inline =
        opcode == R101_WRITE || opcode == R101_SEND || opcode == R101_WRITE_IMM;
    if (inline_data && !supports_inline)
        return false;
    switch (opcode) {
    case R101_WRITE:
        output.operation = Write{input.args.rma.remote_address, inline_data};
        break;
    case R101_READ:
        output.operation = Read{input.args.rma.remote_address};
        break;
    case R101_SEND:
        output.operation = Send{inline_data};
        break;
    case R101_RECV:
        output.operation = Recv{};
        break;
    case R101_WRITE_IMM:
        output.operation = WriteImm{
            input.args.write_imm.remote_address, input.args.write_imm.immediate, inline_data};
        break;
    case R101_COMPARE_SWAP:
        output.operation = CompareSwap{
            input.args.atomic.remote_address, input.args.atomic.compare, input.args.atomic.value};
        break;
    case R101_FETCH_ADD:
        output.operation = FetchAdd{input.args.atomic.remote_address, input.args.atomic.value};
        break;
    default:
        return false;
    }
    output.peer = input.peer;
    output.local_address = input.local_address;
    output.length = input.length;
    return true;
}

r101_transfer_status encode_status(const TransferStatus &input) {
    r101_transfer_status output{};
    if (const auto *failed = std::get_if<Failed>(&input)) {
        if (failed->error == ResultCode::Ok)
            std::abort();
        output.state = R101_FAILED;
        output.error = encode_result(failed->error);
    } else if (const auto *completed = std::get_if<Completed>(&input)) {
        output.state = R101_COMPLETED;
        if (completed->receive) {
            if (const auto *message = std::get_if<Message>(&*completed->receive)) {
                output.receive.kind = R101_RECV_MESSAGE;
                output.receive.length = message->length;
            } else {
                output.receive.kind = R101_RECV_WRITE_IMM;
                output.receive.immediate = std::get<WriteNotice>(*completed->receive).immediate;
            }
        }
    } else {
        output.state = R101_PENDING;
    }
    return output;
}

} // namespace

r101_result r101_create_engine(const r101_config *config, r101_engine **engine) {
    if (!engine)
        return R101_INVALID;
    *engine = nullptr;
    if (!config || config->abi_version != R101_ABI_VERSION)
        return R101_INVALID;
    return call([&] {
        Config native{config->port, config->gid_index, std::nullopt};
        try {
            if (config->device)
                native.device = config->device;
        } catch (const std::bad_alloc &) {
            return R101_NO_MEMORY;
        }
        std::unique_ptr<Engine> local;
        const auto result = Engine::create(std::move(native), local);
        if (result == ResultCode::Ok)
            *engine = reinterpret_cast<r101_engine *>(local.release());
        return encode_result(result);
    });
}

r101_result r101_destroy_engine(r101_engine *engine) {
    if (!engine)
        return R101_INVALID;
    return call([&] {
        delete reinterpret_cast<Engine *>(engine);
        return R101_OK;
    });
}

r101_result r101_register_local_memory(r101_engine *engine,
                                       void *address,
                                       uint64_t length,
                                       r101_access_flags access,
                                       r101_local_memory_handle *memory,
                                       const void **metadata,
                                       uint64_t *metadata_size) {
    if (!memory || !metadata || !metadata_size)
        return R101_INVALID;
    *memory = 0;
    *metadata = nullptr;
    *metadata_size = 0;
    if (!engine || !valid_array(static_cast<uint8_t *>(address), length))
        return R101_INVALID;
    MemoryAccess native;
    if (!decode_access(access, native))
        return R101_INVALID;
    return call([&] {
        MemoryMetadata local;
        const auto result = reinterpret_cast<Engine *>(engine)->register_local_memory(
            address, length, native, local);
        if (result == ResultCode::Ok) {
            *memory = local.memory;
            *metadata = local.metadata.data();
            *metadata_size = local.metadata.size();
        }
        return encode_result(result);
    });
}

r101_result r101_deregister_local_memory(r101_engine *engine, r101_local_memory_handle memory) {
    if (!engine || !memory)
        return R101_INVALID;
    return call([&] {
        return encode_result(reinterpret_cast<Engine *>(engine)->deregister_local_memory(memory));
    });
}

r101_result r101_import_remote_memory(r101_engine *engine,
                                      r101_peer_handle peer,
                                      const void *metadata,
                                      uint64_t metadata_size,
                                      r101_remote_memory_handle *remote_memory) {
    if (!remote_memory)
        return R101_INVALID;
    *remote_memory = 0;
    const auto *bytes = static_cast<const uint8_t *>(metadata);
    if (!engine || !peer || !valid_array(bytes, metadata_size))
        return R101_INVALID;
    return call([&] {
        RemoteMemoryId local = 0;
        const auto result = reinterpret_cast<Engine *>(engine)->import_remote_memory(
            peer, {bytes, metadata_size}, local);
        if (result == ResultCode::Ok)
            *remote_memory = local;
        return encode_result(result);
    });
}

r101_result r101_remove_remote_memory(r101_engine *engine,
                                      r101_remote_memory_handle remote_memory) {
    if (!engine || !remote_memory)
        return R101_INVALID;
    return call([&] {
        return encode_result(
            reinterpret_cast<Engine *>(engine)->remove_remote_memory(remote_memory));
    });
}

r101_result r101_create_peer(r101_engine *engine,
                             r101_peer_handle *peer,
                             const void **metadata,
                             uint64_t *metadata_size) {
    if (!peer || !metadata || !metadata_size)
        return R101_INVALID;
    *peer = 0;
    *metadata = nullptr;
    *metadata_size = 0;
    if (!engine)
        return R101_INVALID;
    return call([&] {
        PeerMetadata local;
        const auto result = reinterpret_cast<Engine *>(engine)->create_peer(local);
        if (result == ResultCode::Ok) {
            *peer = local.peer;
            *metadata = local.metadata.data();
            *metadata_size = local.metadata.size();
        }
        return encode_result(result);
    });
}

r101_result r101_connect_peer(r101_engine *engine,
                              r101_peer_handle peer,
                              const void *metadata,
                              uint64_t metadata_size) {
    const auto *bytes = static_cast<const uint8_t *>(metadata);
    if (!engine || !peer || !valid_array(bytes, metadata_size))
        return R101_INVALID;
    return call([&] {
        return encode_result(
            reinterpret_cast<Engine *>(engine)->connect_peer(peer, {bytes, metadata_size}));
    });
}

r101_result r101_destroy_peer(r101_engine *engine, r101_peer_handle peer) {
    if (!engine || !peer)
        return R101_INVALID;
    return call(
        [&] { return encode_result(reinterpret_cast<Engine *>(engine)->destroy_peer(peer)); });
}

r101_result r101_submit_transfer(r101_engine *engine,
                                 const r101_request *requests,
                                 uint64_t count,
                                 r101_batch_handle *batch) {
    if (!batch)
        return R101_INVALID;
    *batch = 0;
    if (!engine || !valid_array(requests, count))
        return R101_INVALID;
    return call([&] {
        std::vector<Request> native;
        if (count > native.max_size())
            return R101_NO_MEMORY;
        try {
            native.resize(count);
        } catch (const std::bad_alloc &) {
            return R101_NO_MEMORY;
        }
        for (std::size_t i = 0; i < count; ++i) {
            if (!decode_request(requests[i], native[i]))
                return R101_INVALID;
        }
        BatchId local = 0;
        const auto result =
            reinterpret_cast<Engine *>(engine)->submit_transfer(std::move(native), local);
        if (result == ResultCode::Ok)
            *batch = local;
        return encode_result(result);
    });
}

r101_result r101_get_transfer_statuses(r101_engine *engine,
                                       r101_batch_handle batch,
                                       r101_transfer_status *statuses,
                                       uint64_t count) {
    if (!engine || !batch || !valid_array(statuses, count))
        return R101_INVALID;
    return call([&] {
        std::vector<TransferStatus> native;
        if (count > native.max_size())
            return R101_NO_MEMORY;
        try {
            native.resize(count);
        } catch (const std::bad_alloc &) {
            return R101_NO_MEMORY;
        }
        const auto result =
            reinterpret_cast<const Engine *>(engine)->get_transfer_statuses(batch, native);
        if (result != ResultCode::Ok)
            return encode_result(result);
        // Commit only after the query succeeds; caller storage may be uninitialized.
        for (std::size_t i = 0; i < count; ++i)
            statuses[i] = encode_status(native[i]);
        return R101_OK;
    });
}

r101_result r101_free_batch(r101_engine *engine, r101_batch_handle batch) {
    if (!engine || !batch)
        return R101_INVALID;
    return call(
        [&] { return encode_result(reinterpret_cast<Engine *>(engine)->free_batch(batch)); });
}
