"""Exchange connection and memory metadata between trusted Engine processes."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum, auto
from uuid import uuid4

import grpc

from .generated import control_pb2 as wire
from .generated import control_pb2_grpc


class PeerState(Enum):
    CREATED = auto()
    CONNECTED = auto()  # The local QP is connected.
    READY = auto()  # Both QPs are connected and remote memory metadata is imported.


@dataclass
class Peer:
    handle: int
    endpoint: str
    metadata: bytes
    connection_id: str = field(default_factory=lambda: uuid4().hex)
    remote_connection_id: str = ""
    state: PeerState = PeerState.CREATED
    remote_version: int = -1


class ControlPlane(control_pb2_grpc.ControlServicer):
    """Manage peer handshakes and exchange memory metadata.

    Engine serializes outgoing resource operations. RPC handlers use the shared
    state/native lock; outgoing RPCs always run outside that lock.
    """

    def __init__(self, native, state_lock, listen, advertise, timeout):
        host, separator, port = listen.rpartition(":")
        if not separator or not host or not port.isdecimal() or not 0 <= int(port) <= 65535:
            raise ValueError("listen must be host:port (port 0 selects a free port)")
        if host in ("0.0.0.0", "[::]") and not advertise:
            raise ValueError("a wildcard listener needs an advertise host reachable by peers")
        self.native = native
        self._state_lock = state_lock
        self.timeout = timeout
        self.closed = False
        self._peers = {}
        self._channels = {}
        self._memory_metadata = []
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="r101-rpc")
        self._server = grpc.server(self._executor)
        control_pb2_grpc.add_ControlServicer_to_server(self, self._server)
        try:
            bound = self._server.add_insecure_port(listen)
            if not bound:
                raise OSError(f"cannot listen on {listen}")
            self.endpoint = f"{advertise or host}:{bound}"
            self._server.start()
        except Exception:
            self._server.stop(0).wait()
            self._executor.shutdown(wait=True)
            raise

    def _check_open(self):
        if self.closed:
            raise RuntimeError("engine is closed")

    def _stub(self, endpoint):
        channel = self._channels.get(endpoint)
        if channel is None:
            channel = grpc.insecure_channel(endpoint)
            self._channels[endpoint] = channel
        return control_pb2_grpc.ControlStub(channel)

    def _connection(self, peer):
        return wire.Connection(endpoint=self.endpoint, id=peer.connection_id)

    def _offer(self, peer):
        return wire.Offer(connection=self._connection(peer), metadata=peer.metadata)

    def _snapshot(self):
        return wire.Snapshot(version=len(self._memory_metadata), memories=self._memory_metadata)

    def _create_peer(self, endpoint):
        local = self.native.create_peer()
        peer = Peer(local.peer, endpoint, local.metadata)
        self._peers[endpoint] = peer
        return peer

    def _require_peer(self, peer):
        if self._peers.get(peer.endpoint) is not peer:
            raise RuntimeError("peer disconnected during handshake")

    def _find_connection(self, connection):
        peer = self._peers.get(connection.endpoint)
        if peer is None or not connection.id or peer.remote_connection_id != connection.id:
            raise ValueError("unknown connection")
        return peer

    def _connect_native(self, peer, offer):
        """Connect a local QP once, including when both ends initiate together."""
        if offer.connection.endpoint != peer.endpoint or not offer.connection.id:
            raise ValueError("unexpected connection identity")
        if peer.state is PeerState.CREATED:
            self.native.connect_peer(peer.handle, offer.metadata)
            peer.remote_connection_id = offer.connection.id
            peer.state = PeerState.CONNECTED
        elif peer.remote_connection_id != offer.connection.id:
            raise RuntimeError("endpoint already has a different connection")

    def _import_snapshot(self, peer, snapshot):
        """Import a newer memory metadata snapshot; native imports are idempotent."""
        if snapshot.version > peer.remote_version:
            for metadata in snapshot.memories:
                self.native.import_remote_memory(peer.handle, metadata)
            peer.remote_version = snapshot.version

    def connect(self, endpoint):
        """Exchange QP metadata, connect locally, then exchange memory metadata."""
        if not isinstance(endpoint, str) or not endpoint or endpoint == self.endpoint:
            raise ValueError("a different remote Engine endpoint is required")
        with self._state_lock:
            self._check_open()
            peer = self._peers.get(endpoint)
            if peer is None:
                peer = self._create_peer(endpoint)
            if peer.state is PeerState.READY:
                return peer.handle
            stub, offer = self._stub(endpoint), self._offer(peer)

        remote = stub.Connect(offer, timeout=self.timeout)
        with self._state_lock:
            self._require_peer(peer)
            self._connect_native(peer, remote)
            update = wire.Update(connection=self._connection(peer), snapshot=self._snapshot())

        snapshot = stub.Ready(update, timeout=self.timeout)
        with self._state_lock:
            self._require_peer(peer)
            self._import_snapshot(peer, snapshot)
            peer.state = PeerState.READY
            return peer.handle

    def publish(self, metadata):
        """Append memory metadata and wait for ready peers to import the snapshot."""
        with self._state_lock:
            self._check_open()
            self._memory_metadata.append(metadata)
            snapshot = self._snapshot()
            peers = [peer for peer in self._peers.values() if peer.state is PeerState.READY]
        for peer in peers:
            update = wire.Update(connection=self._connection(peer), snapshot=snapshot)
            self._stub(peer.endpoint).Changed(update, timeout=self.timeout)

    def disconnect(self, handle):
        """Destroy the local peer before requesting remote cleanup."""
        with self._state_lock:
            self._check_open()
            peer = next((peer for peer in self._peers.values() if peer.handle == handle), None)
            if peer is None:
                raise ValueError("unknown peer")
            stub, connection = self._stub(peer.endpoint), self._connection(peer)
            self.native.destroy_peer(peer.handle)
            del self._peers[peer.endpoint]
        stub.Close(connection, timeout=self.timeout)

    def Connect(self, request, context):
        """Accept a connection offer and return the local QP metadata."""
        endpoint = request.connection.endpoint
        if not endpoint or endpoint == self.endpoint:
            raise ValueError("a different remote Engine endpoint is required")
        with self._state_lock:
            self._check_open()
            peer = self._peers.get(endpoint)
            if peer is None:
                peer = self._create_peer(endpoint)
            self._connect_native(peer, request)
            return self._offer(peer)

    def Ready(self, request, context):
        """Import the remote memory metadata and return the local snapshot."""
        with self._state_lock:
            self._check_open()
            peer = self._find_connection(request.connection)
            self._import_snapshot(peer, request.snapshot)
            peer.state = PeerState.READY
            return self._snapshot()

    def Changed(self, request, context):
        """Acknowledge updated memory metadata after importing the snapshot."""
        with self._state_lock:
            self._check_open()
            peer = self._find_connection(request.connection)
            self._import_snapshot(peer, request.snapshot)
            return wire.Empty()

    def Close(self, request, context):
        """Release the matching connection; ignore closes for an old connection."""
        with self._state_lock:
            self._check_open()
            peer = self._peers.get(request.endpoint)
            if peer is not None and peer.remote_connection_id == request.id:
                self.native.destroy_peer(peer.handle)
                del self._peers[peer.endpoint]
            return wire.Empty()

    def close(self):
        """Stop incoming RPCs and close outgoing channels."""
        with self._state_lock:
            if self.closed:
                return
            self.closed = True
        self._server.stop(0).wait()
        self._executor.shutdown(wait=True)
        for channel in self._channels.values():
            channel.close()
        self._channels.clear()
        self._peers.clear()
        self._memory_metadata.clear()
