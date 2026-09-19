"""Transfer Engine with metadata exchange and native data transfers."""

import math
import threading
import time
from dataclasses import dataclass, field

from .control import ControlPlane
from .native_engine import EngineError, Memory, NativeEngine
from .types import Failed, Pending


@dataclass(frozen=True)
class LocalMemory:
    """A local registration; its backing storage is retained until Engine.close()."""

    id: int
    address: int
    length: int
    _storage: Memory = field(repr=False, compare=False)

    @property
    def size(self):
        return self.length

    def bytes(self):
        return self._storage.bytes()

    def fill(self, data, offset=0):
        self._storage.fill(data, offset)


class Engine:
    """Register memory, connect peers and submit native transfers."""

    def __init__(self, path, config=None, timeout=10, *, listen="127.0.0.1:0", advertise=None):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.timeout = timeout
        self._resource_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self.native = NativeEngine(path, config)
        try:
            self._control = ControlPlane(self.native, self._state_lock, listen, advertise, timeout)
        except Exception:
            self.native.close()
            raise

    @property
    def endpoint(self):
        return self._control.endpoint

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _check_open(self):
        if self._control.closed or self.native.closed:
            raise RuntimeError("engine is closed")

    def connect(self, endpoint):
        """Connect both ends and exchange memory metadata; reuse a live peer.

        Use the remote Engine's advertised endpoint. Its application does not
        need to call connect(); simultaneous calls reuse the same pair of QPs.
        """
        with self._resource_lock:
            return self._control.connect(endpoint)

    def disconnect(self, peer):
        """Release the local peer, then ask the remote side to release its peer."""
        with self._resource_lock:
            self._control.disconnect(peer)

    def register_local_memory(self, buffer, access):
        """Register storage and wait for connected peers to import its metadata."""
        memory = buffer if isinstance(buffer, Memory) else Memory.from_buffer(buffer)
        with self._resource_lock:
            with self._state_lock:
                self._check_open()
                registration = self.native.register_local_memory(memory, access)
            self._control.publish(registration.metadata)
            return LocalMemory(registration.memory, memory.address, memory.size, memory)

    def allocate(self, size, access):
        """Allocate aligned storage, register it and publish its metadata."""
        return self.register_local_memory(Memory(size), access)

    def submit_transfer(self, requests):
        """Submit a native batch."""
        with self._state_lock:
            self._check_open()
            return self.native.submit_transfer(requests)

    def get_transfer_statuses(self, batch):
        """Read task states."""
        with self._state_lock:
            self._check_open()
            return self.native.get_transfer_statuses(batch)

    def free_batch(self, batch):
        """Release terminal batch record."""
        with self._state_lock:
            self._check_open()
            self.native.free_batch(batch)

    def wait(self, batch, *, timeout=None):
        """Wait for terminal states, retaining the batch on completion or timeout."""
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            statuses = self.get_transfer_statuses(batch)
            if all(not isinstance(status, Pending) for status in statuses):
                return statuses
            if time.monotonic() >= deadline:
                raise TimeoutError(f"batch {batch} is still pending (not cancelled)")
            time.sleep(0.001)

    def transfer(self, requests, *, timeout=None):
        """Submit, wait and free a terminal batch; raise on a failed task."""
        batch = self.submit_transfer(requests)
        statuses = self.wait(batch, timeout=timeout)
        self.free_batch(batch)
        for index, status in enumerate(statuses):
            if isinstance(status, Failed):
                raise EngineError(f"batch {batch} task {index}", status.error)
        return statuses

    def close(self):
        """Stop RPC handling, then release native resources and retained storage."""
        with self._resource_lock:
            self._control.close()
            with self._state_lock:
                self.native.close()
