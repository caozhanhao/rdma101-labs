"""Python submission and waiting helpers, independent of RDMA execution."""

import pytest

from rdma101 import Completed, EngineError, ErrorCode, Failed, MemoryAccess, Pending, Request, Write
from rdma101.control import ControlPlane


def test_submission_uses_native_engine_without_metadata_rpc(engines, monkeypatch):
    left, right = engines(), engines()
    peer = left.connect(right.endpoint)
    local = left.allocate(32, MemoryAccess.NONE)
    remote = right.allocate(32, MemoryAccess.LOCAL_WRITE | MemoryAccess.REMOTE_WRITE)

    def unexpected_rpc(*args):
        pytest.fail("submission must not perform metadata RPCs")

    monkeypatch.setattr(ControlPlane, "_stub", unexpected_rpc)
    requests = [Request(peer, local.address, 32, Write(remote.address))]
    assert left.transfer(requests) == [Completed()]
    assert left.native.requests == requests
    assert not left.native.batches


def test_wait_retains_batch_and_timeout_does_not_cancel(engines):
    engine = engines()
    batch = engine.submit_transfer([Request(1, 0x1000, 8, Write(0x2000))] * 2)
    engine.native.batches[batch] = [Failed(ErrorCode.IO_ERROR), Pending()]
    with pytest.raises(TimeoutError, match="not cancelled"):
        engine.wait(batch, timeout=0)
    assert engine.native.batches[batch] == [Failed(ErrorCode.IO_ERROR), Pending()]

    engine.native.batches[batch][1] = Completed()
    assert engine.wait(batch) == [Failed(ErrorCode.IO_ERROR), Completed()]
    assert batch in engine.native.batches
    engine.free_batch(batch)
    assert batch not in engine.native.batches


def test_transfer_frees_failed_batch_before_raising(engines, monkeypatch):
    engine = engines()
    submit = engine.native.submit_transfer

    def failed_transfer(requests):
        batch = submit(requests)
        engine.native.batches[batch] = [Failed(ErrorCode.IO_ERROR)]
        return batch

    monkeypatch.setattr(engine.native, "submit_transfer", failed_transfer)
    with pytest.raises(EngineError) as error:
        engine.transfer([Request(1, 0x1000, 8, Write(0x2000))])
    assert error.value.code is ErrorCode.IO_ERROR
    assert not engine.native.batches
