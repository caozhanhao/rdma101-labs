"""Lab 1: synchronous WRITE and the native resource lifecycle."""

from dataclasses import replace

import pytest

from rdma101 import Memory, Request, Write
from rdma101.bindings import constants as C
from rdma101.bindings import ffi
from rdma101.native_engine import NativeEngine, _encode_request

from .helpers import (
    WRITE_ACCESS,
    assert_bytes_equal,
    assert_query_rejected,
    assert_submission_rejected,
    connect_native_pair,
    run_write_round,
    setup_transfer,
    submit_batch,
    wait_and_free,
    wait_for_success,
)

pytestmark = [pytest.mark.rdma, pytest.mark.lab1]


def _metadata(engine, comm):
    native = engine.native
    memory = Memory(4096 + 128)
    memory.fill(b"\xa5" * memory.size)
    handle = ffi.new("r101_local_memory_handle *")
    blob, size = ffi.new("const void **"), ffi.new("uint64_t *")
    native.call(
        "register_local_memory",
        native.handle,
        ffi.cast("void *", memory.address),
        memory.size,
        int(WRITE_ACCESS),
        handle,
        blob,
        size,
    )
    assert handle[0] and blob[0] and size[0]
    memory_pointer, memory_size = blob[0], size[0]
    memory_bytes = bytes(ffi.buffer(ffi.cast("char *", memory_pointer), memory_size))
    peer = ffi.new("r101_peer_handle *")
    native.call("create_peer", native.handle, peer, blob, size)
    assert peer[0] and blob[0] and size[0]
    peer_pointer, peer_size = blob[0], size[0]
    peer_bytes = bytes(ffi.buffer(ffi.cast("char *", peer_pointer), peer_size))
    remote_peer, remote_memory = comm.sendrecv(
        (peer_bytes, memory_bytes), dest=1 - comm.rank, source=1 - comm.rank
    )
    native.connect_peer(peer[0], remote_peer)
    imported = native.import_remote_memory(peer[0], remote_memory)
    assert imported
    comm.barrier()
    extra_memory = Memory(256)
    extra = native.register_local_memory(extra_memory, WRITE_ACCESS)
    extra_peer = native.create_peer()
    assert extra.memory != handle[0] and extra_peer.peer != peer[0]
    assert bytes(ffi.buffer(ffi.cast("char *", memory_pointer), memory_size)) == memory_bytes
    assert bytes(ffi.buffer(ffi.cast("char *", peer_pointer), peer_size)) == peer_bytes
    comm.barrier()
    native.remove_remote_memory(imported)
    native.call("destroy_peer", native.handle, peer[0])
    native.destroy_peer(extra_peer.peer)
    native.call("deregister_local_memory", native.handle, handle[0])
    native.deregister_local_memory(extra.memory)
    assert_bytes_equal(memory.bytes(), b"\xa5" * memory.size)


def test_l1_1_metadata_lifetime(run_engines):
    run_engines(_metadata)


def _block(engine, comm):
    transfer = setup_transfer(engine, comm, [4096])
    run_write_round(engine, comm, transfer)


def test_l1_2_write_block(run_engines):
    run_engines(_block)


def _reuse(engine, comm):
    transfer = setup_transfer(engine, comm, [4096])
    for _ in range(3):
        transfer.memory.fill(transfer.before)
        comm.barrier()
        if comm.rank == 0:
            batch = submit_batch(engine, transfer.requests)
            statuses = wait_for_success(engine, batch)
            assert engine.get_transfer_statuses(batch) == statuses
            assert_query_rejected(engine.native, batch, 0)
            assert_query_rejected(engine.native, batch, 2)
            assert_query_rejected(engine.native, 0, 1)
            engine.free_batch(batch)
            assert_query_rejected(engine.native, batch, 1)
        comm.barrier()
        transfer.assert_contents()
        comm.barrier()


def test_l1_3_query_and_reuse(run_engines):
    run_engines(_reuse)


def _zero(engine, comm):
    memory = Memory(4096 + 128)  # Never registered or imported.
    memory.fill(b"\xa5" * memory.size)
    peer, _ = connect_native_pair(engine.native, comm)
    if comm.rank == 0:
        wait_and_free(engine, submit_batch(engine, [Request(peer, 0, 0, Write(0))]))
    comm.barrier()
    assert_bytes_equal(memory.bytes(), b"\xa5" * memory.size)


def test_l1_4_zero_length(run_engines):
    run_engines(_zero)


def _invalid(engine, comm):
    transfer = setup_transfer(engine, comm, [64, 64])
    if comm.rank == 0:
        native = engine.native
        good, followup = transfer.requests
        unconnected = native.create_peer()
        outside = transfer.remote_address + transfer.remote_size
        invalid = [
            (
                replace(good, local_address=transfer.memory.address + transfer.memory.size),
                C.INVALID,
            ),
            (replace(good, operation=Write(outside + 4096)), C.INVALID),
            (replace(good, operation=Write(outside - 1)), C.INVALID),
            (replace(good, local_address=(1 << 64) - 8), C.INVALID),
            (replace(good, operation=Write((1 << 64) - 8)), C.INVALID),
            (replace(good, peer=unconnected.peer), C.INVALID_STATE),
        ]
        assert_submission_rejected(native, [good], count=0)
        assert_submission_rejected(native, [good], count=1, null=True)
        for bad, error in invalid:
            assert_submission_rejected(native, [good, bad], error)
        entries = ffi.new("r101_request[]", [_encode_request(good), _encode_request(good)])
        entries[1].opcode = 0x7FFFFFFF
        batch = ffi.new("r101_batch_handle *", 123)
        assert native.lib.r101_submit_transfer(native.handle, entries, 2, batch) == C.INVALID
        assert batch[0] == 0
        native.destroy_peer(unconnected.peer)
        wait_and_free(engine, submit_batch(engine, [followup]))
    comm.barrier()
    if comm.rank == 1:
        expected = bytearray(transfer.before)
        offset, length = transfer.offsets[1], transfer.lengths[1]
        expected[offset : offset + length] = transfer.expected[offset : offset + length]
        assert_bytes_equal(transfer.memory.bytes(), expected, "rejected batch must not write")
    else:
        assert_bytes_equal(transfer.memory.bytes(), transfer.before)
    comm.barrier()
    run_write_round(engine, comm, transfer)


def test_l1_5_rejected_batch_has_no_effect(run_engines):
    run_engines(_invalid)


def _create_destroy(engine, comm, library, config):
    engine.native.close()
    for _ in range(12):
        with NativeEngine(library, config):
            pass


def test_l1_6_create_destroy(run_engines, library_path, engine_config):
    run_engines(_create_destroy, str(library_path), engine_config, ranks=1)
