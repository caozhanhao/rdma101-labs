"""Real gRPC handshakes and metadata exchange, without an RDMA device."""

import threading
from concurrent.futures import ThreadPoolExecutor

import grpc
import pytest

from rdma101 import EngineError, ErrorCode, MemoryAccess
from rdma101.control import ControlPlane
from rdma101.generated.control_pb2_grpc import ControlStub


def test_connect_exchanges_memory_metadata_and_reuses_peer(engines):
    left, right = engines(), engines()
    left.allocate(32, MemoryAccess.LOCAL_WRITE)
    right.allocate(64, MemoryAccess.LOCAL_WRITE | MemoryAccess.REMOTE_WRITE)
    right.allocate(128, MemoryAccess.REMOTE_READ)

    peer = left.connect(right.endpoint)
    other = right.connect(left.endpoint)
    assert left.native.connected[peer] == right.native.peers[other]
    assert right.native.connected[other] == left.native.peers[peer]
    assert len(left.native.imports) == 2 and len(right.native.imports) == 1
    assert left.connect(right.endpoint) == peer
    assert len(left.native.peers) == len(right.native.peers) == 1


def test_simultaneous_connect_reuses_pending_qps(engines, monkeypatch):
    left, right = engines(), engines()
    created = threading.Barrier(2)
    for engine in (left, right):
        original = engine.native.create_peer

        def create_peer(original=original):
            peer = original()
            created.wait(timeout=5)
            return peer

        monkeypatch.setattr(engine.native, "create_peer", create_peer)

    with ThreadPoolExecutor(2) as workers:
        connecting_left = workers.submit(left.connect, right.endpoint)
        connecting_right = workers.submit(right.connect, left.endpoint)
        peer = connecting_left.result(timeout=10)
        other = connecting_right.result(timeout=10)
    assert len(left.native.peers) == len(right.native.peers) == 1
    assert left.native.connected[peer] == right.native.peers[other]
    assert right.native.connected[other] == left.native.peers[peer]


def test_registration_is_imported_before_return(engines):
    left, right = engines(), engines()
    left.connect(right.endpoint)
    with ThreadPoolExecutor(2) as workers:
        registering_left = workers.submit(left.allocate, 32, MemoryAccess.LOCAL_WRITE)
        registering_right = workers.submit(right.allocate, 64, MemoryAccess.LOCAL_WRITE)
        registering_left.result(timeout=10)
        registering_right.result(timeout=10)
    assert len(left.native.imports) == len(right.native.imports) == 1


def test_older_memory_snapshot_is_ignored(engines, monkeypatch):
    updates = []
    changed = ControlPlane.Changed

    def record_update(control, request, context):
        updates.append(request)
        return changed(control, request, context)

    monkeypatch.setattr(ControlPlane, "Changed", record_update)
    left, right = engines(), engines()
    left.connect(right.endpoint)
    right.allocate(32, MemoryAccess.LOCAL_WRITE)
    right.allocate(64, MemoryAccess.LOCAL_WRITE)
    assert len(left.native.imports) == 2

    def unexpected_import(*args):
        raise AssertionError("an older memory metadata snapshot must not be imported again")

    monkeypatch.setattr(left.native, "import_remote_memory", unexpected_import)
    with grpc.insecure_channel(left.endpoint) as channel:
        ControlStub(channel).Changed(updates[0], timeout=5)
    assert len(left.native.imports) == 2


def test_disconnect_keeps_local_memory_and_ignores_stale_close(engines, monkeypatch):
    closes = []
    close = ControlPlane.Close

    def record_close(control, request, context):
        closes.append(request)
        return close(control, request, context)

    monkeypatch.setattr(ControlPlane, "Close", record_close)
    left, right = engines(), engines()
    left.allocate(32, MemoryAccess.LOCAL_WRITE)
    peer = left.connect(right.endpoint)
    left.disconnect(peer)
    assert not left.native.peers and not right.native.peers
    assert not left.native.imports and not right.native.imports
    assert len(left.native.memories) == 1

    replacement = left.connect(right.endpoint)
    with grpc.insecure_channel(right.endpoint) as channel:
        ControlStub(channel).Close(closes[0], timeout=5)
    assert replacement != peer
    assert replacement in left.native.peers and len(right.native.peers) == 1
