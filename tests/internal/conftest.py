"""Shared native resource double for Python Engine and control-plane tests."""

from itertools import count

import pytest

from rdma101 import Completed, Engine, MemoryMetadata, PeerMetadata, Pending


class FakeNativeEngine:
    """Record resources and requests without accessing a device or moving data."""

    def __init__(self, label, config=None):
        self.label = label
        self.closed = False
        self.ids = count(1)
        self.memories = {}
        self.peers = {}
        self.connected = {}
        self.imports = {}
        self.batches = {}
        self.requests = []

    def register_local_memory(self, memory, access):
        identity = next(self.ids)
        metadata = f"{self.label}:mr:{identity}".encode() + b"\x00\xff"
        self.memories[identity] = memory
        return MemoryMetadata(identity, metadata)

    def create_peer(self):
        identity = next(self.ids)
        self.peers[identity] = f"{self.label}:qp:{identity}".encode()
        return PeerMetadata(identity, self.peers[identity])

    def connect_peer(self, peer, metadata):
        assert peer in self.peers and peer not in self.connected
        self.connected[peer] = metadata

    def import_remote_memory(self, peer, metadata):
        assert peer in self.connected
        key = (peer, metadata)
        if key not in self.imports:
            self.imports[key] = next(self.ids)
        return self.imports[key]

    def destroy_peer(self, peer):
        del self.peers[peer]
        self.connected.pop(peer, None)
        self.imports = {key: value for key, value in self.imports.items() if key[0] != peer}

    def submit_transfer(self, requests):
        requests = list(requests)
        self.requests.extend(requests)
        batch = next(self.ids)
        self.batches[batch] = [Completed() for _ in requests]
        return batch

    def get_transfer_statuses(self, batch):
        return list(self.batches[batch])

    def free_batch(self, batch):
        assert not any(isinstance(status, Pending) for status in self.batches[batch])
        del self.batches[batch]

    def close(self):
        self.closed = True
        self.memories.clear()
        self.peers.clear()
        self.connected.clear()
        self.imports.clear()
        self.batches.clear()


@pytest.fixture
def engines(monkeypatch):
    """Create real RPC endpoints backed by the native double; close them after use."""
    monkeypatch.setattr("rdma101.engine.NativeEngine", FakeNativeEngine)
    created = []

    def create():
        engine = Engine(f"engine-{len(created)}", timeout=5)
        created.append(engine)
        return engine

    yield create
    for engine in reversed(created):
        engine.close()
