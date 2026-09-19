"""Lab 8: remote 64-bit atomics and shared READ/Atomic credits."""

import struct
from dataclasses import replace

import pytest

from rdma101 import CompareSwap, FetchAdd, Memory, Read, Request
from rdma101 import MemoryAccess as Access
from rdma101.bindings import constants as C
from rdma101.bindings import ffi

from .helpers import (
    FILL,
    assert_bytes_equal,
    assert_submission_rejected,
    connect_pair,
    submit_batch,
    wait_and_free,
)

pytestmark = [pytest.mark.rdma, pytest.mark.lab8]
ATOMIC_ACCESS = Access.LOCAL_WRITE | Access.REMOTE_ATOMIC | Access.REMOTE_READ


def put(memory, offset, value):
    memory.fill(struct.pack("=Q", value), offset)


def get(memory, offset):
    return struct.unpack_from("=Q", memory.bytes(), offset)[0]


def _cas(engine, comm):
    memory = engine.allocate(512, ATOMIC_ACCESS)
    memory.fill(FILL * memory.size)
    value = 0x1122334455667788
    put(memory, 64, value)
    peer, remote, _ = connect_pair(engine, comm, memory)
    replacement = 0xDEADBEEF01234567
    if comm.rank == 0:
        wait_and_free(
            engine,
            submit_batch(
                engine,
                [
                    Request(
                        peer, memory.address + 256, 8, CompareSwap(remote + 64, value, replacement)
                    ),
                ],
            ),
        )
    comm.barrier()
    assert get(memory, 64) == (replacement if comm.rank == 1 else value)
    if comm.rank == 0:
        wait_and_free(
            engine,
            submit_batch(
                engine,
                [
                    Request(peer, memory.address + 320, 8, CompareSwap(remote + 64, value + 1, 99)),
                ],
            ),
        )
    comm.barrier()
    expected = bytearray(FILL * memory.size)
    struct.pack_into("=Q", expected, 64, replacement if comm.rank == 1 else value)
    if comm.rank == 0:
        struct.pack_into("=Q", expected, 256, value)
        struct.pack_into("=Q", expected, 320, replacement)
    assert_bytes_equal(memory.bytes(), expected)


def test_l8_1_cas_hit_and_miss(run_engines, atomic_device):
    run_engines(_cas)


def _contend(engine, comm):
    memory = engine.allocate(8192 + 256, ATOMIC_ACCESS)
    memory.fill(FILL * memory.size)
    if comm.rank == 2:
        put(memory, 64, 0)
        put(memory, 128, 0)
    endpoints = comm.allgather((engine.endpoint, memory.address))
    if comm.rank != 2:
        peer = engine.connect(endpoints[2][0])
    comm.barrier()
    old_values, winner = [], None
    if comm.rank != 2:
        remote = endpoints[2][1]
        wait_and_free(
            engine,
            submit_batch(
                engine,
                [
                    Request(peer, memory.address + 64 + i * 8, 8, FetchAdd(remote + 64, 1))
                    for i in range(1000)
                ],
            ),
        )
        old_values = list(struct.unpack_from("=1000Q", memory.bytes(), 64))
    comm.barrier()
    if comm.rank != 2:
        wait_and_free(
            engine,
            submit_batch(
                engine,
                [
                    Request(
                        peer,
                        memory.address + 8128,
                        8,
                        CompareSwap(endpoints[2][1] + 128, 0, comm.rank + 1),
                    ),
                ],
            ),
        )
        winner = get(memory, 8128)
    values = comm.allgather((old_values, winner))
    assert sorted(values[0][0] + values[1][0]) == list(range(2000))
    assert [values[0][1], values[1][1]].count(0) == 1
    expected = bytearray(FILL * memory.size)
    if comm.rank == 2:
        struct.pack_into("=Q", expected, 64, 2000)
        elected = 1 if values[0][1] == 0 else 2
        struct.pack_into("=Q", expected, 128, elected)
        assert (values[1][1] if elected == 1 else values[0][1]) == elected
    else:
        struct.pack_into("=1000Q", expected, 64, *old_values)
        struct.pack_into("=Q", expected, 8128, winner)
    assert_bytes_equal(memory.bytes(), expected)


def test_l8_2_atomic_contention(run_engines, atomic_device):
    run_engines(_contend, ranks=3)


def _invalid(engine, comm):
    memory = engine.allocate(512, ATOMIC_ACCESS)
    memory.fill(FILL * memory.size)
    put(memory, 64, 7)
    peer, remote, _ = connect_pair(engine, comm, memory)
    forbidden = engine.allocate(128, Access.LOCAL_WRITE)
    forbidden.fill(FILL * forbidden.size)
    addresses = comm.allgather(forbidden.address)
    comm.barrier()
    if comm.rank == 0:
        good = Request(peer, memory.address + 256, 8, FetchAdd(remote + 64, 1))
        for bad in [
            replace(good, local_address=good.local_address + 1),
            replace(good, operation=FetchAdd(remote + 65, 1)),
            replace(good, length=0),
            replace(good, length=7),
            replace(good, length=16),
            replace(good, operation=FetchAdd(addresses[1], 1)),
        ]:
            assert_submission_rejected(engine.native, [good, bad])
        # REMOTE_ATOMIC without LOCAL_WRITE is invalid at registration.
        handle = ffi.new("r101_local_memory_handle *", 99)
        metadata, size = ffi.new("const void **"), ffi.new("uint64_t *", 99)

        storage = Memory(64)
        result = engine.native.lib.r101_register_local_memory(
            engine.native.handle,
            ffi.cast("void *", storage.address),
            storage.size,
            int(Access.REMOTE_ATOMIC),
            handle,
            metadata,
            size,
        )
        assert result == C.INVALID and handle[0] == 0 and metadata[0] == ffi.NULL and size[0] == 0
        wait_and_free(engine, submit_batch(engine, [good]))
        assert get(memory, 256) == 7, "rejected batches performed an atomic update"
    comm.barrier()
    assert get(memory, 64) == (8 if comm.rank == 1 else 7)
    assert_bytes_equal(forbidden.bytes(), FILL * forbidden.size)


def test_l8_3_reject_invalid_atomic(run_engines, atomic_device):
    run_engines(_invalid)


def _mixed(engine, comm):
    count = 1024
    memory = engine.allocate(64 + count * 128, ATOMIC_ACCESS)
    memory.fill(FILL * memory.size)
    for i in range(count):
        put(memory, 64 + i * 128, i)
    peer, remote, _ = connect_pair(engine, comm, memory)
    if comm.rank == 0:
        requests = []
        for i in range(count):
            offset = 64 + i * 128
            operation = FetchAdd(remote + offset, 3) if i % 2 == 0 else Read(remote + offset)
            requests.append(Request(peer, memory.address + offset + 64, 8, operation))
        wait_and_free(engine, submit_batch(engine, requests))
    comm.barrier()
    expected = bytearray(FILL * memory.size)
    for i in range(count):
        offset = 64 + i * 128
        struct.pack_into("=Q", expected, offset, i + (3 if comm.rank == 1 and i % 2 == 0 else 0))
        if comm.rank == 0:
            struct.pack_into("=Q", expected, offset + 64, i)
    assert_bytes_equal(memory.bytes(), expected)


def test_l8_4_atomic_and_read_share_credits(run_engines, atomic_device):
    run_engines(_mixed)
