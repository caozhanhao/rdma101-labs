"""Lab 7: RDMA READ direction, permissions and responder credits."""

import pytest

from rdma101 import EngineError, ErrorCode, Failed, Memory, Read, Request, Write
from rdma101 import MemoryAccess as Access

from .helpers import (
    FILL,
    WRITE_ACCESS,
    assert_bytes_equal,
    connect_native_pair,
    connect_pair,
    pattern_bytes,
    setup_transfer,
    submit_batch,
    wait_and_free,
)

pytestmark = [pytest.mark.rdma, pytest.mark.lab7]


def _read(engine, comm, lengths, separate=False):
    transfer = setup_transfer(engine, comm, lengths, read=True)
    if comm.rank == 0:
        groups = [[request] for request in transfer.requests] if separate else [transfer.requests]
        for requests in groups:
            wait_and_free(engine, submit_batch(engine, requests))
    comm.barrier()
    transfer.assert_contents()


def test_l7_1_scattered_read(run_engines):
    run_engines(_read, [1, 17, 64, 127, 777, 4097, 65537])


def _zero_and_boundaries(engine, comm):
    memory = Memory(256)
    memory.fill(FILL * memory.size)
    peer, _ = connect_native_pair(engine.native, comm)
    if comm.rank == 0:
        wait_and_free(engine, submit_batch(engine, [Request(peer, 0, 0, Read(0))]))
    comm.barrier()
    assert_bytes_equal(memory.bytes(), FILL * memory.size)
    engine.native.destroy_peer(peer)
    _read(engine, comm, [1, 4096, 4097, 65536, 65537, 1024 * 1024 + 17], True)


def test_l7_2_zero_and_slice_boundaries(run_engines):
    run_engines(_zero_and_boundaries)


def _permissions(engine, comm, missing_remote):
    access = (
        Access.LOCAL_WRITE
        if missing_remote
        else (Access.NONE if comm.rank == 0 else Access.REMOTE_READ)
    )
    transfer = setup_transfer(engine, comm, [4096], read=True, access=access)
    if comm.rank == 0:
        try:
            batch = submit_batch(engine, transfer.requests)
        except EngineError as error:
            assert error.code in (ErrorCode.INVALID, ErrorCode.IO_ERROR), error
        else:
            statuses = engine.wait(batch)
            assert len(statuses) == 1 and isinstance(statuses[0], Failed), statuses
            engine.free_batch(batch)
    comm.barrier()
    assert_bytes_equal(transfer.memory.bytes(), transfer.before, "forbidden READ")


def test_l7_3_remote_read_permission(run_engines):
    run_engines(_permissions, True)


def test_l7_4_local_write_permission(run_engines):
    run_engines(_permissions, False)


def test_l7_5_more_reads_than_responder_credits(run_engines):
    run_engines(_read, [257 + i % 31 for i in range(1024)])


def _mixed(engine, comm):
    memory = engine.allocate(2048, WRITE_ACCESS | Access.REMOTE_READ)
    before = bytearray(FILL * memory.size)
    before[64:321] = pattern_bytes(257, comm.rank)
    memory.fill(before)
    peer, remote, _ = connect_pair(engine, comm, memory)
    if comm.rank == 0:
        wait_and_free(
            engine,
            submit_batch(
                engine,
                [
                    Request(peer, memory.address + 512, 257, Read(remote + 64)),
                    Request(peer, memory.address + 64, 257, Write(remote + 512)),
                ],
            ),
        )
    comm.barrier()
    expected = bytearray(before)
    expected[512:769] = pattern_bytes(257, 1 - comm.rank)
    assert_bytes_equal(memory.bytes(), expected)


def test_l7_6_read_write_batch(run_engines):
    run_engines(_mixed)
