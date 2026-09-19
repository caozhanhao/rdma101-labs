"""Lab 2: background progress and asynchronous batch lifetime."""

import pytest

from rdma101 import Engine, EngineError, ErrorCode, Pending
from tests.verbs_probe import VerbsProbe

from .helpers import (
    FILL,
    make_buffer_contents,
    run_write_round,
    setup_transfer,
    submit_batch,
    wait_and_free,
    wait_for_success,
)

pytestmark = [pytest.mark.rdma, pytest.mark.lab2]


def _async_write(engine, comm):
    transfer = setup_transfer(engine, comm, [4096])
    if comm.rank == 0:
        probe = VerbsProbe(timeout=engine.timeout / 2)
        with probe.hold_completions() as before:
            batch = submit_batch(engine, transfer.requests)
            assert engine.get_transfer_statuses(batch) == [Pending()], (
                "completed before CQE delivery"
            )
            probe.wait_for_post(before)
            assert engine.get_transfer_statuses(batch) == [Pending()]
            probe.assert_background_only(before)
        probe.wait_for_completion(before)
        wait_and_free(engine, batch)
        probe.assert_background_only(before)
    comm.barrier()
    transfer.assert_contents()


def test_l2_1_async_write(run_engines):
    run_engines(_async_write, observe_verbs=True)


def _queries(engine, comm):
    transfer = setup_transfer(engine, comm, [4096])
    if comm.rank == 0:
        probe = VerbsProbe(timeout=engine.timeout / 2)
        with probe.hold_completions() as before:
            batch = submit_batch(engine, transfer.requests)
            # No Engine call is needed to make the worker post the request.
            probe.wait_for_post(before)
            print("querying while CQE delivery is paused", flush=True)
            for _ in range(8):
                assert engine.get_transfer_statuses(batch) == [Pending()]
                probe.assert_background_only(before)
        # No query or free_batch call is needed to make the worker consume CQEs.
        probe.wait_for_completion(before)
        terminal = wait_for_success(engine, batch)
        assert engine.get_transfer_statuses(batch) == terminal
        assert engine.get_transfer_statuses(batch) == terminal
        probe.assert_background_only(before)
        engine.free_batch(batch)
    comm.barrier()
    transfer.assert_contents()


def test_l2_2_nonblocking_monotonic_queries(run_engines):
    run_engines(_queries, observe_verbs=True)


def _busy(engine, comm):
    transfer = setup_transfer(engine, comm, [4096])
    if comm.rank == 0:
        probe = VerbsProbe(timeout=engine.timeout / 2)
        with probe.hold_completions() as before:
            batch = submit_batch(engine, transfer.requests)
            probe.wait_for_post(before)
            assert engine.get_transfer_statuses(batch) == [Pending()]
            for _ in range(3):
                with pytest.raises(EngineError) as rejected:
                    engine.free_batch(batch)
                assert rejected.value.code is ErrorCode.BUSY
                assert engine.get_transfer_statuses(batch) == [Pending()]
                probe.assert_background_only(before)
        probe.wait_for_completion(before)
        wait_and_free(engine, batch)
        probe.assert_background_only(before)
    comm.barrier()
    transfer.assert_contents()
    transfer.memory.fill(transfer.before)
    comm.barrier()
    run_write_round(engine, comm, transfer)


def test_l2_3_busy_batch_and_reuse(run_engines):
    run_engines(_busy, observe_verbs=True)


def _rounds(engine, comm):
    transfer = setup_transfer(engine, comm, [4096])
    probe = VerbsProbe(timeout=engine.timeout / 2) if comm.rank == 0 else None
    seen = set()
    for turn in range(12):
        transfer.expected = make_buffer_contents(
            transfer.offsets, transfer.lengths, transfer.memory.size, seed=turn
        )
        transfer.memory.fill(transfer.expected if comm.rank == 0 else FILL * transfer.memory.size)
        comm.barrier()
        before = probe.call_stats() if probe is not None else None
        batch = run_write_round(engine, comm, transfer)
        if comm.rank == 0:
            assert batch not in seen, "batch IDs must not be reused after free"
            seen.add(batch)
            probe.assert_background_only(before)


def test_l2_4_repeated_batches(run_engines):
    run_engines(_rounds, observe_verbs=True)


def _destroy_pending(engine, comm, library, config):
    transfer = setup_transfer(engine, comm, [4096])
    if comm.rank == 0:
        probe = VerbsProbe(timeout=engine.timeout / 2)
        with probe.hold_completions() as before:
            batch = submit_batch(engine, transfer.requests)
            probe.wait_for_post(before)
            assert engine.get_transfer_statuses(batch) == [Pending()]
            probe.assert_background_only(before)
            print("destroying Engine with a pending task", flush=True)
            # QP shutdown releases the gate so cleanup can drain flush CQEs.
            engine.close()
        assert probe.call_stats().live_contexts == 0, "destroy left a device context open"
    comm.barrier()  # The writer has stopped before the receiver releases its MR.
    engine.close()
    with Engine(library, config, timeout=engine.timeout) as replacement:
        next_transfer = setup_transfer(replacement, comm, [4096])
        run_write_round(replacement, comm, next_transfer)
        comm.barrier()


def test_l2_5_destroy_pending(run_engines, library_path, engine_config):
    run_engines(_destroy_pending, str(library_path), engine_config, observe_verbs=True)
