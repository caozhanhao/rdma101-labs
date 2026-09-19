"""Adapt the Transfer Engine C API to Python request and result types."""

from dataclasses import dataclass
from typing import Iterable, Optional

from .bindings import constants as R101
from .bindings import ffi, library
from .types import (
    CompareSwap,
    Completed,
    Config,
    ErrorCode,
    Failed,
    FetchAdd,
    MemoryAccess,
    MemoryMetadata,
    Message,
    PeerMetadata,
    Pending,
    Read,
    Recv,
    Request,
    Send,
    TransferStatus,
    Write,
    WriteImm,
    WriteNotice,
)

_ERROR_CODES = {getattr(R101, code.name): code for code in ErrorCode}
_OPCODES = {
    Write: R101.WRITE,
    Read: R101.READ,
    Send: R101.SEND,
    Recv: R101.RECV,
    WriteImm: R101.WRITE_IMM,
    CompareSwap: R101.COMPARE_SWAP,
    FetchAdd: R101.FETCH_ADD,
}


class EngineError(RuntimeError):
    def __init__(self, operation: str, code: ErrorCode):
        self.code = code
        super().__init__(f"{operation}: {code.name}")


def _error_code(value):
    try:
        return _ERROR_CODES[value]
    except KeyError:
        raise RuntimeError(f"unknown engine error code: {value}") from None


def check(operation, code):
    if code != R101.OK:
        raise EngineError(operation, _error_code(code))


@dataclass
class Memory:
    """Application-owned storage. Registration doesn't allocate memory."""

    size: int

    def __post_init__(self):
        if self.size <= 0:
            raise ValueError("positive memory size required")
        self.storage = ffi.new("char[]", self.size + 63)
        self.address = (int(ffi.cast("uintptr_t", self.storage)) + 63) & ~63

    @classmethod
    def from_buffer(cls, buffer):
        """Retain writable contiguous Python storage without copying its contents."""
        storage = ffi.from_buffer("char[]", buffer, require_writable=True)
        if not len(storage):
            raise ValueError("positive memory size required")
        memory = cls.__new__(cls)
        memory.storage = storage
        memory.size = len(storage)
        memory.address = int(ffi.cast("uintptr_t", storage))
        return memory

    def bytes(self):
        return bytes(ffi.buffer(ffi.cast("char *", self.address), self.size))

    def fill(self, data, offset=0):
        if offset < 0 or offset + len(data) > self.size:
            raise ValueError("memory write out of bounds")
        ffi.memmove(ffi.cast("char *", self.address + offset), data, len(data))


def _encode_request(request):
    if not isinstance(request, Request):
        raise TypeError("requests must contain Request values")
    operation = request.operation
    if type(operation) not in _OPCODES:
        raise TypeError(f"unknown operation: {type(operation).__name__}")
    values = {
        "opcode": _OPCODES[type(operation)],
        "flags": R101.INLINE if getattr(operation, "inline_data", False) else 0,
        "peer": request.peer,
        "local_address": ffi.cast("void *", request.local_address),
        "length": request.length,
    }
    if isinstance(operation, (Write, Read)):
        values["args"] = {"rma": {"remote_address": operation.remote_address}}
    elif isinstance(operation, WriteImm):
        values["args"] = {
            "write_imm": {
                "remote_address": operation.remote_address,
                "immediate": operation.immediate,
            }
        }
    elif isinstance(operation, (CompareSwap, FetchAdd)):
        values["args"] = {
            "atomic": {
                "remote_address": operation.remote_address,
                "compare": operation.compare if isinstance(operation, CompareSwap) else 0,
                "value": operation.value,
            }
        }
    return values


class NativeEngine:
    """Own the native engine and adapt its C API to Python values."""

    def __init__(self, path, config: Optional[Config] = None):
        config = Config() if config is None else config
        if not isinstance(config, Config):
            raise TypeError("config must be a Config instance")
        self.lib = library(path)
        self.memories, self.remote_memories, self.batches, self.peers = {}, {}, {}, set()
        device = (
            ffi.new("char[]", config.device.encode()) if config.device is not None else ffi.NULL
        )
        config_storage = ffi.new(
            "r101_config *",
            {
                "abi_version": R101.ABI_VERSION,
                "device": device,
                "port": config.port,
                "gid_index": config.gid_index,
            },
        )
        output = ffi.new("r101_engine **")
        self.call("create_engine", config_storage, output)
        self.handle = output[0]
        if self.handle == ffi.NULL:
            raise RuntimeError("create_engine returned OK with null engine")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @property
    def closed(self):
        return self.handle == ffi.NULL

    def call(self, name, *args):
        check(name, getattr(self.lib, "r101_" + name)(*args))

    def register_local_memory(
        self,
        memory: Memory,
        access: MemoryAccess,
    ) -> MemoryMetadata:
        """Register memory and return its handle and copied metadata."""
        if not isinstance(memory, Memory):
            raise TypeError("memory must be a Memory instance")
        region = ffi.new("r101_local_memory_handle *")
        metadata, size = ffi.new("const void **"), ffi.new("uint64_t *")
        self.call(
            "register_local_memory",
            self.handle,
            ffi.cast("void *", memory.address),
            memory.size,
            int(access),
            region,
            metadata,
            size,
        )
        if not region[0] or region[0] in self.memories:
            raise RuntimeError("registration IDs must be nonzero and distinct")
        self.memories[region[0]] = (memory, b"")
        if not metadata[0] or not size[0]:
            raise RuntimeError("registration returned missing memory metadata")
        data = bytes(ffi.buffer(ffi.cast("char *", metadata[0]), size[0]))
        self.memories[region[0]] = (memory, data)
        return MemoryMetadata(region[0], data)

    def memory_metadata(self, memory: Memory) -> bytes:
        """Return the copied metadata for a live local registration."""
        for registered, metadata in self.memories.values():
            if registered is memory:
                return metadata
        raise ValueError("memory is not registered with this engine")

    def deregister_local_memory(self, memory: int) -> None:
        """Deregister after the application has stopped and drained remote accesses."""
        self.call("deregister_local_memory", self.handle, memory)
        del self.memories[memory]

    def import_remote_memory(self, peer: int, metadata: bytes) -> int:
        """Copy a peer's MR descriptor into the engine; return its local import ID."""
        if not isinstance(metadata, bytes) or not metadata:
            raise ValueError("memory metadata must be nonempty bytes")
        region = ffi.new("r101_remote_memory_handle *")
        self.call(
            "import_remote_memory",
            self.handle,
            peer,
            ffi.from_buffer(metadata),
            len(metadata),
            region,
        )
        if not region[0] or self.remote_memories.get(region[0], peer) != peer:
            raise RuntimeError("invalid remote memory ID")
        self.remote_memories[region[0]] = peer
        return region[0]

    def remove_remote_memory(self, remote_memory: int) -> None:
        """Forget an imported region; this does not deregister memory on the peer."""
        self.call("remove_remote_memory", self.handle, remote_memory)
        del self.remote_memories[remote_memory]

    def create_peer(self) -> PeerMetadata:
        """Return a local peer handle and an immediate copy of its metadata."""
        peer = ffi.new("r101_peer_handle *")
        metadata, size = ffi.new("const void **"), ffi.new("uint64_t *")
        self.call("create_peer", self.handle, peer, metadata, size)
        if not peer[0] or peer[0] in self.peers:
            raise RuntimeError("live peer IDs must be nonzero and distinct")
        if not metadata[0] or not size[0]:
            raise RuntimeError("create_peer returned missing connection metadata")
        data = bytes(ffi.buffer(ffi.cast("char *", metadata[0]), size[0]))
        self.peers.add(peer[0])
        return PeerMetadata(peer[0], data)

    def connect_peer(self, peer: int, metadata: bytes) -> None:
        if not isinstance(metadata, bytes) or not metadata:
            raise ValueError("connection metadata must be nonempty bytes")
        self.call("connect_peer", self.handle, peer, ffi.from_buffer(metadata), len(metadata))

    def destroy_peer(self, peer: int) -> None:
        """Cancel a peer's pending tasks and wait for local cleanup."""
        self.call("destroy_peer", self.handle, peer)
        self.peers.remove(peer)
        for region, owner in list(self.remote_memories.items()):
            if owner == peer:
                del self.remote_memories[region]

    def submit_transfer(self, requests: Iterable[Request]) -> int:
        """Convert and submit requests; retain their operation types for result decoding."""
        requests = tuple(requests)
        entries = ffi.new("r101_request[]", [_encode_request(request) for request in requests])
        operations = tuple(type(request.operation) for request in requests)
        batch = ffi.new("r101_batch_handle *")
        self.call("submit_transfer", self.handle, entries, len(entries), batch)
        if not batch[0] or batch[0] in self.batches:
            raise RuntimeError("live batch IDs must be nonzero and distinct")
        self.batches[batch[0]] = operations
        return batch[0]

    def get_transfer_statuses(self, batch: int) -> list[TransferStatus]:
        """Return independent Python status values in submission order."""
        operations = self.batches[batch]
        statuses = ffi.new("r101_transfer_status[]", len(operations))
        self.call("get_transfer_statuses", self.handle, batch, statuses, len(operations))
        results = []
        for index, (status, operation) in enumerate(zip(statuses, operations)):
            if status.state == R101.FAILED:
                results.append(Failed(_error_code(status.error)))
                continue
            if status.error != R101.OK:
                raise RuntimeError(f"task {index}: non-failed status has an error")
            if status.state == R101.PENDING:
                results.append(Pending())
            elif status.state == R101.COMPLETED:
                receive = None
                if operation is Recv:
                    if status.receive.kind == R101.RECV_MESSAGE:
                        if status.receive.immediate != 0:
                            raise RuntimeError(f"task {index}: message has nonzero immediate")
                        receive = Message(status.receive.length)
                    elif status.receive.kind == R101.RECV_WRITE_IMM:
                        if status.receive.length != 0:
                            raise RuntimeError(f"task {index}: write notice has nonzero length")
                        receive = WriteNotice(status.receive.immediate)
                    else:
                        raise RuntimeError(
                            f"task {index}: invalid receive kind {status.receive.kind}"
                        )
                results.append(Completed(receive))
            else:
                raise RuntimeError(f"task {index}: invalid transfer state {status.state}")
        return results

    def free_batch(self, batch: int) -> None:
        self.call("free_batch", self.handle, batch)
        del self.batches[batch]

    def close(self) -> None:
        """Destroy the native engine before releasing retained buffers and handles."""
        if not self.closed:
            self.call("destroy_engine", self.handle)
            self.handle = ffi.NULL
            self.memories.clear()
            self.remote_memories.clear()
            self.batches.clear()
            self.peers.clear()
