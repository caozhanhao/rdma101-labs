"""Lab 3: ranges, sliced completion and all-or-nothing acceptance."""

from dataclasses import replace

import pytest

from rdma101 import Completed, EngineError, ErrorCode, Pending, Write
from tests.verbs_probe import VerbsProbe

from .helpers import (
    assert_bytes_equal,
    assert_submission_rejected,
    run_write_round,
    setup_transfer,
    submit_batch,
    wait_and_free,
    wait_for_success,
)

pytestmark = [pytest.mark.rdma, pytest.mark.lab3]


def _scattered_ranges(engine, comm):
    lengths = [
        1,
        3,
        7,
        31,
        64,
        127,
        128,
        129,
        255,
        256,
        257,
        511,
        777,
        1024,
        2049,
        4095,
        4096,
        4097,
        8193,
    ]
    transfer = setup_transfer(engine, comm, lengths)
    run_write_round(engine, comm, transfer)


def test_l3_1_scattered_ranges(run_engines):
    run_engines(_scattered_ranges)


def _boundaries(engine, comm):
    lengths = [1, 4096, 4097, 65536, 65537, 1024 * 1024, 1024 * 1024 + 17]
    transfer = setup_transfer(engine, comm, lengths)
    if comm.rank == 0:
        probe = VerbsProbe(timeout=engine.timeout / 2)
        for request in transfer.requests:
            before = probe.call_stats()
            statuses = wait_and_free(engine, submit_batch(engine, [request]))
            assert statuses == [Completed()]
            posted = probe.call_stats().posted_wrs - before.posted_wrs
            print(f"WRITE length={request.length} posted_wrs={posted}", flush=True)
            if request.length == lengths[-1]:
                assert posted >= 2, "the 1 MiB + 17 B request must be split into multiple WRs"
    comm.barrier()
    transfer.assert_contents()


def test_l3_2_slice_boundaries(run_engines):
    run_engines(_boundaries, observe_verbs=True)


def _invalid_tail(engine, comm):
    transfer = setup_transfer(engine, comm, [4097])
    if comm.rank == 0:
        good = transfer.requests[0]
        bad = replace(good, operation=Write(transfer.remote_address + transfer.remote_size - 1))
        assert_submission_rejected(engine.native, [good, bad])
    comm.barrier()
    assert_bytes_equal(transfer.memory.bytes(), transfer.before, "rejected batch")
    comm.barrier()
    run_write_round(engine, comm, transfer)


def test_l3_3_invalid_tail_rejects_whole_batch(run_engines):
    run_engines(_invalid_tail)


def _batch_statuses(engine, comm):
    lengths = [1, 65537, 3, 4097, 17]
    transfer = setup_transfer(engine, comm, lengths)
    if comm.rank == 0:
        probe = VerbsProbe(timeout=engine.timeout / 2)
        with probe.hold_completions() as before:
            batch = submit_batch(engine, transfer.requests)
            assert engine.get_transfer_statuses(batch) == [Pending()] * len(lengths)
            probe.wait_for_post(before)
            for _ in range(3):
                assert engine.get_transfer_statuses(batch) == [Pending()] * len(lengths)
                probe.assert_background_only(before)
        statuses = wait_for_success(engine, batch)
        assert statuses == [Completed()] * len(lengths)
        assert engine.get_transfer_statuses(batch) == statuses
        engine.free_batch(batch)
    comm.barrier()
    transfer.assert_contents()


def test_l3_4_batch_statuses(run_engines):
    run_engines(_batch_statuses, observe_verbs=True)


def _pending_batch(engine, comm):
    transfer = setup_transfer(engine, comm, [4097, 17, 65537])
    if comm.rank == 0:
        probe = VerbsProbe(timeout=engine.timeout / 2)
        with probe.hold_completions() as before:
            batch = submit_batch(engine, transfer.requests)
            probe.wait_for_post(before)
            with pytest.raises(EngineError) as rejected:
                engine.free_batch(batch)
            assert rejected.value.code is ErrorCode.BUSY
            assert engine.get_transfer_statuses(batch) == [Pending()] * len(transfer.requests)
            probe.assert_background_only(before)
        wait_and_free(engine, batch)
    comm.barrier()
    transfer.assert_contents()


def test_l3_5_pending_batch_cannot_be_freed(run_engines):
    run_engines(_pending_batch, observe_verbs=True)


def _reuse(engine, comm):
    transfer = setup_transfer(engine, comm, [17, 4097, 65537])
    for _ in range(2):
        transfer.memory.fill(transfer.before)
        comm.barrier()
        run_write_round(engine, comm, transfer)
    added = setup_transfer(engine, comm, [31, 8193], seed=71)
    assert added.peer == transfer.peer, "adding an MR should reuse the connection"
    run_write_round(engine, comm, added)
    transfer.assert_contents()


def test_l3_6_reuse_and_dynamic_registration(run_engines):
    run_engines(_reuse)
