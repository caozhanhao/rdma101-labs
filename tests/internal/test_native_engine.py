"""Ownership and result handling at the native C API boundary."""

import gc
import weakref
from types import SimpleNamespace

import pytest

from rdma101 import (
    CompareSwap,
    Completed,
    Config,
    ErrorCode,
    Failed,
    FetchAdd,
    MemoryAccess,
    Message,
    Pending,
    Read,
    Recv,
    Request,
    Send,
    Write,
    WriteImm,
    WriteNotice,
)
from rdma101.bindings import constants as R101
from rdma101.bindings import ffi
from rdma101.native_engine import EngineError, Memory, NativeEngine


@pytest.fixture
def engine():
    result = NativeEngine.__new__(NativeEngine)
    result.handle = ffi.cast("r101_engine *", 123)
    result.peers, result.batches, result.memories = set(), {}, {}
    result.remote_memories = {}
    result.lib = SimpleNamespace()
    return result


@pytest.mark.parametrize(
    "code,peer,size", [(R101.NO_MEMORY, 0, 0), (R101.OK, 1, 0), (R101.OK, 0, 4)]
)
def test_failed_or_incomplete_peer_creation(engine, code, peer, size):
    def create(handle, output, metadata, length):
        output[0], length[0] = peer, size
        return code

    engine.lib.r101_create_peer = create
    with pytest.raises(RuntimeError):
        engine.create_peer()
    assert not engine.peers


@pytest.mark.parametrize(
    "code,name", [(R101.INVALID, "INVALID"), (R101.INVALID_STATE, "INVALID_STATE")]
)
def test_rejected_submit_and_busy_free_keep_existing_batches(engine, code, name):
    engine.batches = {9: (Write,)}
    engine.lib.r101_submit_transfer = lambda *args: code
    engine.lib.r101_free_batch = lambda *args: R101.BUSY
    with pytest.raises(EngineError, match=f"submit_transfer: {name}$") as exc:
        engine.submit_transfer([])
    assert exc.value.code == ErrorCode[name]
    with pytest.raises(EngineError, match="BUSY"):
        engine.free_batch(9)
    assert engine.batches == {9: (Write,)}


@pytest.mark.parametrize(
    "operation,opcode,flags,arguments",
    [
        (Write(0x2000), R101.WRITE, 0, (0x2000,)),
        (Write(0x2000, inline_data=True), R101.WRITE, R101.INLINE, (0x2000,)),
        (Read(0x2000), R101.READ, 0, (0x2000,)),
        (Send(), R101.SEND, 0, ()),
        (Send(inline_data=True), R101.SEND, R101.INLINE, ()),
        (Recv(), R101.RECV, 0, ()),
        (WriteImm(0x2000, 0x89ABCDEF), R101.WRITE_IMM, 0, (0x2000, 0x89ABCDEF)),
        (
            WriteImm(0x2000, 0x89ABCDEF, inline_data=True),
            R101.WRITE_IMM,
            R101.INLINE,
            (0x2000, 0x89ABCDEF),
        ),
        (
            CompareSwap(0x2000, 0x1122334455667788, 0xFFEEDDCCBBAA0099),
            R101.COMPARE_SWAP,
            0,
            (0x2000, 0x1122334455667788, 0xFFEEDDCCBBAA0099),
        ),
        (FetchAdd(0x2000, 0xFFEEDDCCBBAA0099), R101.FETCH_ADD, 0, (0x2000, 0, 0xFFEEDDCCBBAA0099)),
    ],
)
def test_descriptor_fields(engine, operation, opcode, flags, arguments):
    def submit(handle, entries, count, batch):
        assert count == 1
        entry = entries[0]
        assert (
            entry.opcode,
            entry.flags,
            entry.peer,
            int(ffi.cast("uintptr_t", entry.local_address)),
            entry.length,
        ) == (opcode, flags, 7, 0x1000, 8)
        if opcode in (R101.WRITE, R101.READ):
            actual = (entry.args.rma.remote_address,)
        elif opcode == R101.WRITE_IMM:
            actual = (entry.args.write_imm.remote_address, entry.args.write_imm.immediate)
        elif opcode in (R101.COMPARE_SWAP, R101.FETCH_ADD):
            actual = (
                entry.args.atomic.remote_address,
                entry.args.atomic.compare,
                entry.args.atomic.value,
            )
        else:
            actual = ()
        assert actual == arguments
        batch[0] = 41
        return R101.OK

    engine.lib.r101_submit_transfer = submit
    assert engine.submit_transfer([Request(7, 0x1000, 8, operation)]) == 41


def test_status_snapshot_contains_independent_python_values(engine):
    calls = []
    engine.batches = {10: (Recv, Recv, Write, Send, Read)}

    def statuses(handle, batch, output, count):
        calls.append((batch, count))
        output[0].state = R101.COMPLETED
        output[0].receive.kind, output[0].receive.length = R101.RECV_MESSAGE, 31
        output[1].state = R101.COMPLETED
        output[1].receive.kind, output[1].receive.immediate = R101.RECV_WRITE_IMM, 0x89ABCDEF
        output[2].state = R101.COMPLETED
        output[3].state = R101.PENDING
        output[4].state, output[4].error = R101.FAILED, R101.IO_ERROR
        for index in (2, 3, 4):
            output[index].receive.kind = 0x7FFFFFFF  # Only a successful RECV defines this field.
        return R101.OK

    engine.lib.r101_get_transfer_statuses = statuses
    result = engine.get_transfer_statuses(10)
    assert engine.get_transfer_statuses(10) == result
    assert calls == [(10, 5), (10, 5)]
    assert result == [
        Completed(Message(31)),
        Completed(WriteNotice(0x89ABCDEF)),
        Completed(),
        Pending(),
        Failed(ErrorCode.IO_ERROR),
    ]
    engine.lib.r101_get_transfer_statuses = lambda *args: R101.INVALID
    with pytest.raises(EngineError, match="INVALID"):
        engine.get_transfer_statuses(10)
    assert engine.batches == {10: (Recv, Recv, Write, Send, Read)}


def test_single_status_can_outlive_its_batch(engine):
    engine.batches = {10: (Recv, Send)}

    def statuses(handle, batch, output, count):
        output[0].state = R101.COMPLETED
        output[0].receive.kind, output[0].receive.length = R101.RECV_MESSAGE, 31
        output[1].state = R101.COMPLETED
        return R101.OK

    engine.lib.r101_get_transfer_statuses = statuses
    first = engine.get_transfer_statuses(10)[0]
    engine.lib.r101_free_batch = lambda *args: R101.OK
    engine.free_batch(10)
    gc.collect()
    assert first == Completed(Message(31))
    assert not engine.batches


@pytest.mark.parametrize("cleanup", ["deregister", "destroy"])
def test_registered_memory_lives_until_successful_cleanup(engine, cleanup):
    storage = ffi.new("char[]", b"\x00\xffMR")

    def register(handle, address, length, access, region, metadata, size):
        assert access == R101.LOCAL_WRITE | R101.REMOTE_READ
        region[0], metadata[0], size[0] = 31, storage, 4
        return R101.OK

    engine.lib.r101_register_local_memory = register
    memory = Memory(32)
    reference = weakref.ref(memory)
    assert memory.address % 64 == 0
    memory.fill(b"abc", 29)
    assert memory.bytes()[29:] == b"abc"
    with pytest.raises(ValueError):
        memory.fill(b"bad", 30)
    registered = engine.register_local_memory(
        memory, MemoryAccess.LOCAL_WRITE | MemoryAccess.REMOTE_READ
    )
    region, metadata = registered.memory, registered.metadata
    assert region == 31 and metadata == b"\x00\xffMR"
    assert engine.memory_metadata(memory) == metadata
    ffi.buffer(storage)[:] = b"\0" * len(storage)
    assert metadata == b"\x00\xffMR"
    del memory
    gc.collect()
    assert reference() is not None

    if cleanup == "destroy":
        engine.lib.r101_destroy_engine = lambda *args: R101.IO_ERROR
        with pytest.raises(EngineError, match="IO_ERROR"):
            engine.close()
    else:
        engine.lib.r101_deregister_local_memory = lambda *args: R101.BUSY
        with pytest.raises(EngineError, match="BUSY"):
            engine.deregister_local_memory(region)
    assert region in engine.memories and reference() is not None

    def release(*args):
        assert reference() is not None
        return R101.OK

    if cleanup == "destroy":
        engine.lib.r101_destroy_engine = release
        engine.close()
    else:
        engine.lib.r101_deregister_local_memory = release
        engine.deregister_local_memory(region)
    gc.collect()
    assert reference() is None and not engine.memories


def test_failed_registration_does_not_retain_buffer(engine):
    engine.lib.r101_register_local_memory = lambda *args: R101.NO_MEMORY
    memory = Memory(32)
    reference = weakref.ref(memory)
    with pytest.raises(EngineError, match="NO_MEMORY"):
        engine.register_local_memory(memory, MemoryAccess.LOCAL_WRITE)
    del memory
    gc.collect()
    assert not engine.memories and reference() is None


def test_failed_import_preserves_previous_imports(engine):
    engine.remote_memories = {17: 5}
    engine.lib.r101_import_remote_memory = lambda *args: R101.INVALID
    with pytest.raises(EngineError, match="INVALID"):
        engine.import_remote_memory(5, b"bad descriptor")
    assert engine.remote_memories == {17: 5}


@pytest.mark.parametrize("device", [None, "设备-rxe0"])
def test_config_is_converted_to_c_with_the_current_abi(monkeypatch, device):
    def create(config, output):
        assert config.abi_version == R101.ABI_VERSION
        assert (config.port, config.gid_index) == (3, 5)
        assert (ffi.string(config.device).decode() if config.device else None) == device
        output[0] = ffi.cast("r101_engine *", 123)
        return R101.OK

    lib = SimpleNamespace(r101_create_engine=create, r101_destroy_engine=lambda _: R101.OK)
    monkeypatch.setattr("rdma101.native_engine.library", lambda _: lib)
    with NativeEngine("test-library", Config(device=device, port=3, gid_index=5)):
        pass
