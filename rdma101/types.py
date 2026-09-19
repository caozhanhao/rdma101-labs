"""Python values shared by NativeEngine and Engine."""

from dataclasses import dataclass
from enum import Enum, IntFlag, auto
from typing import Optional, Union


class ErrorCode(Enum):
    UNIMPLEMENTED = auto()
    INVALID = auto()
    BUSY = auto()
    IO_ERROR = auto()
    TIMEOUT = auto()
    UNSUPPORTED = auto()
    NO_MEMORY = auto()
    INVALID_STATE = auto()
    CANCELED = auto()


class MemoryAccess(IntFlag):
    NONE = 0
    LOCAL_WRITE = 1 << 0
    REMOTE_WRITE = 1 << 1
    REMOTE_READ = 1 << 2
    REMOTE_ATOMIC = 1 << 3


@dataclass(frozen=True)
class Config:
    device: Optional[str] = None
    port: int = 1
    gid_index: int = 0


@dataclass(frozen=True)
class Write:
    remote_address: int
    inline_data: bool = False


@dataclass(frozen=True)
class Read:
    remote_address: int


@dataclass(frozen=True)
class Send:
    inline_data: bool = False


@dataclass(frozen=True)
class Recv:
    pass


@dataclass(frozen=True)
class WriteImm:
    remote_address: int
    immediate: int
    inline_data: bool = False


@dataclass(frozen=True)
class CompareSwap:
    remote_address: int
    compare: int
    value: int


@dataclass(frozen=True)
class FetchAdd:
    remote_address: int
    value: int


Operation = Union[Write, Read, Send, Recv, WriteImm, CompareSwap, FetchAdd]


@dataclass(frozen=True)
class Request:
    """A transfer descriptor; the application retains the referenced buffers."""

    peer: int
    local_address: int
    length: int
    operation: Operation


@dataclass(frozen=True)
class Message:
    length: int


@dataclass(frozen=True)
class WriteNotice:
    immediate: int


ReceiveResult = Union[Message, WriteNotice]


@dataclass(frozen=True)
class Pending:
    pass


@dataclass(frozen=True)
class Completed:
    receive: Optional[ReceiveResult] = None


@dataclass(frozen=True)
class Failed:
    error: ErrorCode


TransferStatus = Union[Pending, Completed, Failed]


@dataclass(frozen=True)
class PeerMetadata:
    peer: int
    metadata: bytes


@dataclass(frozen=True)
class MemoryMetadata:
    memory: int
    metadata: bytes


__all__ = [
    "ErrorCode",
    "MemoryAccess",
    "Config",
    "Write",
    "Read",
    "Send",
    "Recv",
    "WriteImm",
    "CompareSwap",
    "FetchAdd",
    "Operation",
    "Request",
    "Message",
    "WriteNotice",
    "ReceiveResult",
    "Pending",
    "Completed",
    "Failed",
    "TransferStatus",
    "PeerMetadata",
    "MemoryMetadata",
]
