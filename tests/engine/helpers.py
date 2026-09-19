"""Data layouts, completion checks and C API assertions shared by the labs."""

import random
import time
from dataclasses import dataclass

import pytest

from rdma101 import Completed, Pending, Read, Request, Write
from rdma101 import MemoryAccess as Access
from rdma101.bindings import constants as C
from rdma101.bindings import ffi
from rdma101.native_engine import _encode_request

GUARD = 64
FILL = b"\xa5"
WRITE_ACCESS = Access.LOCAL_WRITE | Access.REMOTE_WRITE


def pattern_bytes(size, seed=0):
    return random.Random(seed).randbytes(size)


def make_layout(lengths, reverse=False):
    offsets, cursor = [0] * len(lengths), GUARD
    indices = reversed(range(len(lengths))) if reverse else range(len(lengths))
    for index in indices:
        offsets[index] = cursor
        cursor += lengths[index] + 2 * GUARD + index % 17
    return offsets, cursor + GUARD


def make_buffer_contents(offsets, lengths, size, seed=None):
    data = bytearray(FILL * size)
    if seed is not None:
        for index, (offset, length) in enumerate(zip(offsets, lengths)):
            data[offset : offset + length] = pattern_bytes(length, seed + index)
    return bytes(data)


def assert_bytes_equal(actual, expected, label="buffer"):
    if actual != expected:
        offset = next((i for i, (a, b) in enumerate(zip(actual, expected)) if a != b), None)
        pytest.fail(f"{label}: first mismatch at {offset}; sizes {len(actual)} / {len(expected)}")


def connect_pair(engine, comm, memory):
    """Exchange addresses as application data; Engine exchanges its opaque metadata."""
    descriptions = comm.allgather((engine.endpoint, memory.address, memory.size))
    endpoint, address, size = descriptions[1 - comm.rank]
    peer = engine.connect(endpoint)
    comm.barrier()
    return peer, address, size


def connect_native_pair(native, comm, memories=(), local=None):
    """Explicit two-party handshake for tests that need native resource lifetimes."""
    local = native.create_peer() if local is None else local
    other = 1 - comm.rank
    remote = comm.sendrecv(local.metadata, dest=other, source=other)
    native.connect_peer(local.peer, remote)
    descriptors = [(m.address, m.size, native.memory_metadata(m)) for m in memories]
    remote = comm.sendrecv(descriptors, dest=other, source=other)
    for _, _, metadata in remote:
        native.import_remote_memory(local.peer, metadata)
    comm.barrier()
    return local.peer, remote


@dataclass
class Transfer:
    memory: object
    requests: list
    before: bytes
    expected: bytes
    offsets: list
    lengths: list
    peer: int
    remote_address: int
    remote_size: int

    def assert_contents(self):
        assert_bytes_equal(self.memory.bytes(), self.expected)


def setup_transfer(engine, comm, lengths, *, read=False, seed=17, access=None):
    """One-way transfers, with independently laid out source/destination and guards."""
    local_offsets, size = make_layout(lengths, reverse=bool(comm.rank))
    remote_offsets, _ = make_layout(lengths, reverse=not comm.rank)
    source = comm.rank == (1 if read else 0)
    before = make_buffer_contents(local_offsets, lengths, size, seed if source else None)
    expected = make_buffer_contents(local_offsets, lengths, size, seed)
    if access is None:
        access = Access.LOCAL_WRITE | Access.REMOTE_READ if read else WRITE_ACCESS
    memory = engine.allocate(size, access)
    memory.fill(before)
    peer, remote, remote_size = connect_pair(engine, comm, memory)
    operation = Read if read else Write
    requests = [
        Request(peer, memory.address + offset, length, operation(remote + target))
        for offset, target, length in zip(local_offsets, remote_offsets, lengths)
    ]
    return Transfer(
        memory, requests, before, expected, local_offsets, lengths, peer, remote, remote_size
    )


def submit_batch(engine, requests):
    requests = list(requests)
    batch = engine.submit_transfer(requests)
    assert batch != 0
    return batch


def wait_for_terminal(engine, batch):
    """Check task-by-task monotonicity, allowing completion before the first query."""
    deadline = time.monotonic() + engine.timeout
    previous = None
    while True:
        statuses = engine.get_transfer_statuses(batch)
        if previous is not None:
            assert len(statuses) == len(previous)
            for index, (old, new) in enumerate(zip(previous, statuses)):
                assert isinstance(old, Pending) or old == new, f"task {index}: {old} -> {new}"
        if all(not isinstance(s, Pending) for s in statuses):
            assert statuses == engine.get_transfer_statuses(batch), "terminal results changed"
            return statuses
        assert time.monotonic() < deadline, f"batch {batch}: timed out with {statuses}"
        previous = statuses
        time.sleep(0.001)


def wait_for_success(engine, batch):
    statuses = wait_for_terminal(engine, batch)
    assert all(isinstance(s, Completed) for s in statuses), statuses
    return statuses


def wait_and_free(engine, batch):
    statuses = wait_for_success(engine, batch)
    engine.free_batch(batch)
    return statuses


def run_write_round(engine, comm, transfer, *, requests=None):
    batch = None
    if comm.rank == 0:
        batch = submit_batch(engine, transfer.requests if requests is None else requests)
        wait_and_free(engine, batch)
    comm.barrier()
    transfer.assert_contents()
    comm.barrier()
    return batch


def assert_submission_rejected(native, requests, code=C.INVALID, *, count=None, null=False):
    """Bypass Python argument checks; rejected submissions must clear the output."""
    entries = ffi.new("r101_request[]", [_encode_request(r) for r in requests])
    batch = ffi.new("r101_batch_handle *", 0xDEADBEEF)
    result = native.lib.r101_submit_transfer(
        native.handle,
        ffi.NULL if null else entries,
        len(requests) if count is None else count,
        batch,
    )
    assert result == code, f"submit returned {result}, expected {code}"
    assert batch[0] == 0, "a rejected submission returned a batch"


def assert_query_rejected(native, batch, count):
    statuses = ffi.new("r101_transfer_status[]", max(count, 1))
    storage = ffi.buffer(statuses)
    storage[:] = b"\x5a" * len(storage)
    before = bytes(storage)
    result = native.lib.r101_get_transfer_statuses(native.handle, batch, statuses, count)
    assert result == C.INVALID
    assert bytes(storage) == before, "failed query modified the output array"
