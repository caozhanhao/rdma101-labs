"""Lab 6: message matching, notifications, cancellation and inline sources."""

import time

import pytest

from rdma101 import (
    Completed,
    EngineError,
    ErrorCode,
    Failed,
    Memory,
    Message,
    Pending,
    Recv,
    Request,
    Send,
    Write,
    WriteImm,
    WriteNotice,
)
from rdma101.bindings import constants as C

from .helpers import (
    FILL,
    WRITE_ACCESS,
    assert_bytes_equal,
    assert_submission_rejected,
    connect_native_pair,
    connect_pair,
    pattern_bytes,
    setup_transfer,
    submit_batch,
    wait_and_free,
    wait_for_success,
)

pytestmark = [pytest.mark.rdma, pytest.mark.lab6]


def _messages(engine, comm, rounds):
    memory = engine.allocate(2048, WRITE_ACCESS)
    peer, _, _ = connect_pair(engine, comm, memory)
    for turn in range(rounds):
        length = [1, 31, 127, 255][turn % 4]
        before = bytearray(FILL * memory.size)
        before[64 : 64 + length] = pattern_bytes(length, comm.rank + turn * 17)
        memory.fill(before)
        comm.barrier()
        requests = [
            Request(peer, memory.address + 512, 256, Recv()),
            Request(peer, memory.address + 64, length, Send()),
        ]
        batch = submit_batch(engine, requests)
        statuses = wait_and_free(engine, batch)
        assert statuses == [Completed(Message(length)), Completed()]
        comm.barrier()
        expected = bytearray(before)
        expected[512 : 512 + length] = pattern_bytes(length, 1 - comm.rank + turn * 17)
        assert_bytes_equal(memory.bytes(), expected)
        comm.barrier()


def test_l6_1_send_recv_batch(run_engines):
    run_engines(_messages, 1)


def test_l6_2_repost_receive(run_engines):
    run_engines(_messages, 8)


def _busy_receive(engine, comm):
    memory = engine.allocate(256, WRITE_ACCESS)
    peer, _, _ = connect_pair(engine, comm, memory)
    memory.fill(FILL * memory.size)
    if comm.rank == 1:
        batch = submit_batch(engine, [Request(peer, memory.address + 64, 64, Recv())])
        assert engine.get_transfer_statuses(batch) == [Pending()]
        with pytest.raises(EngineError) as caught:
            engine.free_batch(batch)
        assert caught.value.code is ErrorCode.BUSY
        assert engine.get_transfer_statuses(batch) == [Pending()]
        comm.send("receive accepted", dest=0)
        assert wait_and_free(engine, batch) == [Completed(Message(17))]
    else:
        assert comm.recv(source=1) == "receive accepted"
        memory.fill(pattern_bytes(17), 64)
        wait_and_free(
            engine, submit_batch(engine, [Request(peer, memory.address + 64, 17, Send())])
        )
    comm.barrier()
    expected = bytearray(FILL * memory.size)
    expected[64:81] = pattern_bytes(17)
    assert_bytes_equal(memory.bytes(), expected)


def test_l6_3_unmatched_receive_is_busy(run_engines):
    run_engines(_busy_receive)


def _cancel_peer(engine, comm):
    memory = engine.allocate(1024, WRITE_ACCESS)
    memory.fill(FILL * memory.size)
    endpoints = comm.allgather((engine.endpoint, memory.address))
    # Rank 0 has two independent peers; the other ranks each need only one.
    peers = {
        i: engine.connect(endpoints[i][0])
        for i in range(comm.size)
        if i != comm.rank and (i == 0 or comm.rank == 0)
    }
    comm.barrier()
    if comm.rank == 0:
        old = submit_batch(engine, [Request(peers[1], 0, 0, Write(0))])
        terminal = wait_for_success(engine, old)
        batch = submit_batch(
            engine,
            [
                Request(peers[1], memory.address + 64, 64, Recv()),
                Request(peers[2], memory.address + 256, 64, Recv()),
            ],
        )
        assert engine.get_transfer_statuses(batch) == [Pending(), Pending()]
        engine.native.destroy_peer(peers[1])
        assert engine.get_transfer_statuses(old) == terminal
        assert engine.get_transfer_statuses(batch) == [Failed(ErrorCode.CANCELED), Pending()]
        engine.free_batch(old)
        comm.send("send", dest=2)
        statuses = engine.wait(batch)
        assert statuses == [Failed(ErrorCode.CANCELED), Completed(Message(17))]
        engine.free_batch(batch)
        expected = bytearray(FILL * memory.size)
        expected[256:273] = pattern_bytes(17)
        assert_bytes_equal(memory.bytes(), expected)
    elif comm.rank == 2:
        assert comm.recv(source=0) == "send"
        memory.fill(pattern_bytes(17), 64)
        wait_and_free(
            engine, submit_batch(engine, [Request(peers[0], memory.address + 64, 17, Send())])
        )
    comm.barrier()


def test_l6_4_destroy_peer_cancels_only_its_tasks(run_engines):
    run_engines(_cancel_peer, ranks=3)


def _before_connect(engine, comm):
    native = engine.native
    peer = native.create_peer()
    memory = Memory(256)
    memory.fill(FILL * memory.size)
    native.register_local_memory(memory, WRITE_ACCESS)
    receives = [
        Request(peer.peer, memory.address + 64, 64, Recv()),
        Request(peer.peer, 0, 0, Recv()),
    ]
    batch = submit_batch(engine, receives)
    assert engine.get_transfer_statuses(batch) == [Pending(), Pending()]
    assert_submission_rejected(native, [Request(peer.peer, 0, 0, Write(0))], C.INVALID_STATE)
    assert_submission_rejected(native, [Request(peer.peer, 0, 0, Send())], C.INVALID_STATE)
    connect_native_pair(native, comm, local=peer)
    # Sequential sends avoid prescribing ordering between tasks within a batch.
    wait_and_free(engine, submit_batch(engine, [Request(peer.peer, 0, 0, Send())]))
    comm.barrier()
    immediate = 0x89ABCDEF if comm.rank == 0 else 0xF1234567
    wait_and_free(engine, submit_batch(engine, [Request(peer.peer, 0, 0, WriteImm(0, immediate))]))
    statuses = wait_and_free(engine, batch)
    other = 0xF1234567 if comm.rank == 0 else 0x89ABCDEF
    # Two RECV tasks have no ordering guarantee; each incoming result occurs once.
    assert statuses.count(Completed(Message(0))) == 1
    assert statuses.count(Completed(WriteNotice(other))) == 1
    assert_bytes_equal(memory.bytes(), FILL * memory.size)


def test_l6_5_preconnection_receives_and_empty_messages(run_engines):
    run_engines(_before_connect)


def _immediate(engine, comm):
    transfer = setup_transfer(engine, comm, [2 * 1024 * 1024 + 37])
    notice = engine.allocate(256, WRITE_ACCESS)
    for immediate in [0, 0x12345678, 0xFFFFFFFF]:
        transfer.memory.fill(transfer.before)
        notice.fill(FILL * notice.size)
        if comm.rank == 1:
            # The second receive is a trap for accidental per-slice notifications.
            batch = submit_batch(
                engine,
                [
                    Request(transfer.peer, notice.address + 64, 64, Recv()),
                    Request(transfer.peer, notice.address + 128, 64, Recv()),
                ],
            )
            comm.send("ready", dest=0)
            while True:
                statuses = engine.get_transfer_statuses(batch)
                assert all(isinstance(s, (Pending, Completed)) for s in statuses), statuses
                if any(isinstance(s, Completed) for s in statuses):
                    assert statuses.count(Completed(WriteNotice(immediate))) == 1, statuses
                    transfer.assert_contents()  # Notification itself certifies visibility of every slice.
                    assert_bytes_equal(notice.bytes(), FILL * notice.size)
                    break
                time.sleep(0.001)
            comm.send("notice checked", dest=0)
            assert comm.recv(source=0) == "write completed"
            statuses = engine.get_transfer_statuses(batch)
            assert statuses.count(Completed(WriteNotice(immediate))) == 1
            assert statuses.count(Pending()) == 1
            # Supply one empty SEND to consume the spare receive and reuse the peer.
            comm.send("send empty", dest=0)
            final = wait_and_free(engine, batch)
            assert final.count(Completed(WriteNotice(immediate))) == 1
            assert final.count(Completed(Message(0))) == 1
        else:
            assert comm.recv(source=1) == "ready"
            request = transfer.requests[0]
            batch = submit_batch(
                engine,
                [
                    Request(
                        request.peer,
                        request.local_address,
                        request.length,
                        WriteImm(request.operation.remote_address, immediate),
                    )
                ],
            )
            wait_and_free(engine, batch)
            assert comm.recv(source=1) == "notice checked"
            comm.send("write completed", dest=1)
            assert comm.recv(source=1) == "send empty"
            wait_and_free(engine, submit_batch(engine, [Request(transfer.peer, 0, 0, Send())]))
        comm.barrier()
        transfer.assert_contents()
        comm.barrier()


def test_l6_6_one_notification_per_logical_write(run_engines):
    run_engines(_immediate)


def _inline(engine, comm):
    receive = engine.allocate(512, WRITE_ACCESS)
    peer, remote, _ = connect_pair(engine, comm, receive)
    source = Memory(256)  # Never register the inline source.
    for length in [1, 127, 128, 129, 256]:
        receive.fill(FILL * receive.size)
        if comm.rank == 0:
            receive.fill(b"X", 384)
        source.fill(pattern_bytes(length, length))
        if comm.rank == 1:
            batch = submit_batch(engine, [Request(peer, receive.address + 64, 256, Recv())])
            comm.send("ready", dest=0)
            supported = comm.recv(source=0)
            if not supported:
                assert engine.get_transfer_statuses(batch) == [Pending()]
                assert_bytes_equal(receive.bytes(), FILL * receive.size)
                comm.send("not consumed", dest=0)
            expected_length = length if supported else 0
            assert wait_and_free(engine, batch) == [Completed(Message(expected_length))]
            assert comm.recv(source=0) == "batch complete"
            expected = bytearray(FILL * receive.size)
            if supported:
                expected[64 : 64 + length] = pattern_bytes(length, length)
                expected[384] = ord("X")
            assert_bytes_equal(receive.bytes(), expected)
        else:
            assert comm.recv(source=1) == "ready"
            try:
                batch = submit_batch(
                    engine,
                    [
                        Request(peer, receive.address + 384, 1, Write(remote + 384)),
                        Request(peer, source.address, length, Send(inline_data=True)),
                    ],
                )
            except EngineError as error:
                assert error.code is ErrorCode.UNSUPPORTED
                print(f"inline length={length}: UNSUPPORTED", flush=True)
                comm.send(False, dest=1)
                assert comm.recv(source=1) == "not consumed"
                wait_and_free(engine, submit_batch(engine, [Request(peer, 0, 0, Send())]))
            else:
                print(f"inline length={length}: supported", flush=True)
                comm.send(True, dest=1)
                wait_and_free(engine, batch)
                assert_bytes_equal(source.bytes()[:length], pattern_bytes(length, length))
            comm.send("batch complete", dest=1)
        comm.barrier()


def test_l6_7_inline_unregistered_source(run_engines):
    run_engines(_inline)


def _mixed_queues(engine, comm):
    count, stride = 128, 192
    memory = engine.allocate(count * stride + 512, WRITE_ACCESS)
    memory.fill(FILL * memory.size)
    source_offset, target_offset = count * stride + 64, count * stride + 256
    payload = pattern_bytes(17, comm.rank)
    memory.fill(payload, source_offset)
    peer, remote, _ = connect_pair(engine, comm, memory)
    requests = [Request(peer, memory.address + i * stride + 64, 64, Recv()) for i in range(count)]
    requests += [
        Request(peer, memory.address + source_offset, 17, Write(remote + target_offset)),
        Request(peer, memory.address + source_offset, 17, Send()),
    ]
    batch = submit_batch(engine, requests)
    deadline = time.monotonic() + engine.timeout
    while True:
        statuses = engine.get_transfer_statuses(batch)
        assert all(isinstance(s, (Pending, Completed)) for s in statuses), statuses
        if statuses[-2:] == [Completed(), Completed()] and Completed(Message(17)) in statuses:
            break
        assert time.monotonic() < deadline, "unmatched receives exhausted the send credits"
        time.sleep(0.001)
    comm.barrier()  # Both senders have stopped accessing the remote MR.
    engine.native.destroy_peer(peer)
    statuses = engine.get_transfer_statuses(batch)
    assert statuses[-2:] == [Completed(), Completed()]
    assert statuses[:count].count(Completed(Message(17))) == 1
    assert statuses[:count].count(Failed(ErrorCode.CANCELED)) == count - 1
    engine.free_batch(batch)
    expected = bytearray(FILL * memory.size)
    expected[source_offset : source_offset + 17] = payload
    expected[target_offset : target_offset + 17] = pattern_bytes(17, 1 - comm.rank)
    received = statuses.index(Completed(Message(17))) * stride + 64
    expected[received : received + 17] = pattern_bytes(17, 1 - comm.rank)
    assert_bytes_equal(memory.bytes(), expected)


def test_l6_8_mixed_rma_and_messages(run_engines):
    run_engines(_mixed_queues)
