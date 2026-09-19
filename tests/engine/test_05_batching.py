"""Lab 5: batched WRs, selective signaling and tail completion."""

import pytest

from tests.verbs_probe import VerbsProbe

from .helpers import setup_transfer, submit_batch, wait_and_free

pytestmark = [pytest.mark.rdma, pytest.mark.lab5]


def _batches(engine, comm, lengths, rounds):
    transfer = setup_transfer(engine, comm, lengths)
    probe = VerbsProbe(timeout=engine.timeout / 2) if comm.rank == 0 else None
    for _ in range(rounds):
        transfer.memory.fill(transfer.before)
        comm.barrier()
        if comm.rank == 0:
            with probe.observe_writes():
                wait_and_free(engine, submit_batch(engine, transfer.requests))
                probe.assert_writes_completed(len(lengths), batching=len(lengths) > 1)
        comm.barrier()
        transfer.assert_contents()
        comm.barrier()


def test_l5_1_mixed_lengths(run_engines):
    run_engines(_batches, [1, 3, 17, 63, 64, 127, 128, 257, 777], 1, observe_verbs=True)


def test_l5_2_short_tail(run_engines):
    run_engines(_batches, [3], 1, observe_verbs=True)


def test_l5_3_many_post_batches(run_engines):
    run_engines(_batches, [4096 + i % 19 for i in range(1025)], 1, observe_verbs=True)


def test_l5_4_release_and_repeat(run_engines):
    run_engines(_batches, [1, 127, 4097, 65539, 3, 777, 256, 33, 8193], 3, observe_verbs=True)
