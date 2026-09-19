"""Lab 4: shared send credits and multiple live batches."""

import time

import pytest

from rdma101 import Completed, Pending
from tests.verbs_probe import VerbsProbe

from .helpers import setup_transfer, submit_batch

pytestmark = [pytest.mark.rdma, pytest.mark.lab4]


def _windowed_batches(engine, comm, lengths, batch_size, rounds):
    transfer = setup_transfer(engine, comm, lengths)
    probe = VerbsProbe(timeout=engine.timeout / 2) if comm.rank == 0 else None
    for _ in range(rounds):
        transfer.memory.fill(transfer.before)
        comm.barrier()
        if comm.rank == 0:
            with probe.observe_writes():
                with probe.hold_completions() as before:
                    batches = {
                        submit_batch(engine, transfer.requests[start : start + batch_size]): min(
                            batch_size, len(lengths) - start
                        )
                        for start in range(0, len(lengths), batch_size)
                    }
                    probe.wait_for_post(before, minimum=2)
                previous = {batch: [Pending()] * count for batch, count in batches.items()}
                live = set(batches)
                deadline = time.monotonic() + engine.timeout
                while live:
                    for batch in list(live):
                        statuses = engine.get_transfer_statuses(batch)
                        assert len(statuses) == batches[batch]
                        for old, new in zip(previous[batch], statuses):
                            assert isinstance(old, Pending) or old == new, (batch, old, new)
                        assert all(isinstance(s, (Pending, Completed)) for s in statuses), statuses
                        previous[batch] = statuses
                        if all(isinstance(s, Completed) for s in statuses):
                            live.remove(batch)
                    assert time.monotonic() < deadline, f"batches still pending: {live}"
                    if live:
                        time.sleep(0.001)
                probe.assert_background_only(before)
                probe.assert_writes_completed(len(lengths), concurrent=True)
                for batch in batches:
                    engine.free_batch(batch)
        comm.barrier()
        transfer.assert_contents()
        comm.barrier()


def test_l4_1_thousand_tasks(run_engines):
    run_engines(
        _windowed_batches, [1 + i * 37 % 2048 for i in range(1024)], 1024, 1, observe_verbs=True
    )


def test_l4_2_two_live_batches(run_engines):
    run_engines(_windowed_batches, [8192 + i % 31 for i in range(1024)], 512, 1, observe_verbs=True)


def test_l4_3_more_slices_than_queue_capacity(run_engines):
    run_engines(
        _windowed_batches, [65536 + i % 31 for i in range(1024)], 1024, 1, observe_verbs=True
    )


def test_l4_4_free_and_resubmit(run_engines):
    run_engines(_windowed_batches, [257 + i % 97 for i in range(1024)], 512, 2, observe_verbs=True)


def test_l4_5_competing_batches_make_progress(run_engines):
    run_engines(
        _windowed_batches, [16384 + i % 127 for i in range(1024)], 128, 3, observe_verbs=True
    )
