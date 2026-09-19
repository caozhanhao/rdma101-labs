#![allow(dead_code)]

#[allow(unused_imports)]
use rdma_mummy_sys as verbs;
use std::ffi::c_void;
use std::ops::BitOr;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Error {
    Unimplemented,
    Invalid,
    Busy,
    Io,
    Timeout,
    Unsupported,
    NoMemory,
    InvalidState,
    Canceled,
}

pub struct Config {
    pub port: u32,
    pub gid_index: u32,
    pub device: Option<String>, // None selects an available device.
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MemoryAccess(u32);

impl MemoryAccess {
    pub const NONE: Self = Self(0);
    pub const LOCAL_WRITE: Self = Self(1 << 0);
    pub const REMOTE_WRITE: Self = Self(1 << 1);
    pub const REMOTE_READ: Self = Self(1 << 2);
    pub const REMOTE_ATOMIC: Self = Self(1 << 3);

    pub fn contains(self, access: Self) -> bool {
        self.0 & access.0 == access.0
    }
}

impl BitOr for MemoryAccess {
    type Output = Self;

    fn bitor(self, other: Self) -> Self {
        Self(self.0 | other.0)
    }
}

pub type PeerId = u64;
pub type BatchId = u64;
pub type MemoryId = u64;
pub type RemoteMemoryId = u64;

#[derive(Debug, Clone, Copy)]
pub enum Operation {
    Write {
        remote_address: u64,
        inline_data: bool,
    },
    Read {
        remote_address: u64,
    },
    Send {
        inline_data: bool,
    },
    Recv,
    WriteImm {
        remote_address: u64,
        immediate: u32,
        inline_data: bool,
    },
    CompareSwap {
        remote_address: u64,
        compare: u64,
        value: u64,
    },
    FetchAdd {
        remote_address: u64,
        value: u64,
    },
}

/// The descriptor is owned; its buffer remains borrowed until terminal status.
#[derive(Debug, Clone, Copy)]
pub struct Request {
    pub peer: PeerId,
    pub local_address: *mut c_void,
    pub length: u64,
    pub operation: Operation,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReceiveResult {
    Message { length: u64 },
    WriteNotice { immediate: u32 },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TransferStatus {
    Pending,
    Completed { receive: Option<ReceiveResult> },
    Failed(Error),
}

/// Engine owns the metadata; it stays valid until this peer or Engine is destroyed.
pub struct PeerMetadata<'a> {
    pub peer: PeerId,
    pub metadata: &'a [u8],
}

/// Bytes remain stable until this registration is deregistered or Engine is dropped.
pub struct MemoryMetadata<'a> {
    pub memory: MemoryId,
    pub metadata: &'a [u8],
}

pub struct Engine {}

impl Engine {
    /// Create an engine with device resources and a background worker.
    /// Return an initialized object; release partial resources on error.
    pub fn create(config: Config) -> Result<Box<Self>, Error> {
        Err(Error::Unimplemented)
    }

    /// Register application memory and return a fresh handle and its metadata.
    ///
    /// # Safety
    /// Memory stays allocated until deregistration succeeds or Engine is dropped.
    /// Applications must not access a buffer in conflict with a pending transfer's
    /// reads or writes.
    pub unsafe fn register_local_memory(
        &mut self,
        address: *mut c_void,
        length: usize,
        access: MemoryAccess,
    ) -> Result<MemoryMetadata<'_>, Error> {
        Err(Error::Unimplemented)
    }

    /// Deregister local memory without freeing the application's buffer.
    /// Return Busy while accepted local tasks still use it, including queued work.
    /// Success invalidates the registration handle and its metadata.
    ///
    /// # Safety
    /// The application has stopped and drained all remote accesses to this MR.
    pub unsafe fn deregister_local_memory(&mut self, memory: MemoryId) -> Result<(), Error> {
        Err(Error::Unimplemented)
    }

    /// Import a connected peer's memory registration and return a local handle.
    /// Copy the metadata without modifying the QP; reuse identical live imports
    /// and reject conflicting overlapping regions.
    pub fn import_remote_memory(
        &mut self,
        peer: PeerId,
        metadata: &[u8],
    ) -> Result<RemoteMemoryId, Error> {
        Err(Error::Unimplemented)
    }

    /// Remove a local imported descriptor; return Busy while an accepted task uses it.
    /// The peer's MR and its other connections remain unchanged.
    pub fn remove_remote_memory(&mut self, memory: RemoteMemoryId) -> Result<(), Error> {
        Err(Error::Unimplemented)
    }

    /// Create a local peer and export its connection metadata.
    /// Return a nonzero handle and nonempty metadata; the RC QP starts in INIT.
    /// Bytes stay immutable and stable until this peer or Engine is destroyed;
    /// roll back the creation on failure.
    pub fn create_peer(&mut self) -> Result<PeerMetadata<'_>, Error> {
        Err(Error::Unimplemented)
    }

    /// Complete the local side of a connection using the remote peer's metadata.
    /// Copy retained fields and move the QP to RTR/RTS; input bytes are borrowed for this call.
    pub fn connect_peer(&mut self, peer: PeerId, metadata: &[u8]) -> Result<(), Error> {
        Err(Error::Unimplemented)
    }

    /// Destroy a local peer and its imported memory descriptors.
    /// Stop its work, including unmatched receives, and finish local cleanup.
    /// After accesses end, publish Failed(Canceled) for tasks pending at shutdown;
    /// preserve existing terminal results, all batches, local MRs and other peers.
    /// On success destroy its QP/imports and invalidate its metadata and handle.
    /// On cleanup failure keep the handle for retry, but reject further use.
    /// Also handle INIT and failed setup.
    pub fn destroy_peer(&mut self, peer: PeerId) -> Result<(), Error> {
        Err(Error::Unimplemented)
    }

    /// Submit requests as a new batch for background execution and return its nonzero ID.
    /// Either the whole batch is accepted or no tasks are accepted.
    ///
    /// # Safety
    /// Referenced buffers stay valid until terminal status. Registration is required
    /// except for Inline sources; these remain borrowed until terminal status too.
    /// Sources cannot be modified; destinations cannot be accessed while pending.
    pub unsafe fn submit_transfer(&mut self, requests: Vec<Request>) -> Result<BatchId, Error> {
        Err(Error::Unimplemented)
    }

    /// Read the current status of every task in a batch, in submission order.
    /// The output slice must match the task count. This call does not advance transfers.
    /// Completed/Failed mean device accesses have ended and buffers are no longer borrowed.
    pub fn get_transfer_statuses(
        &self,
        batch: BatchId,
        statuses: &mut [TransferStatus],
    ) -> Result<(), Error> {
        Err(Error::Unimplemented)
    }

    /// Release the batch's task records. Return Busy while any task is Pending.
    pub fn free_batch(&mut self, batch: BatchId) -> Result<(), Error> {
        Err(Error::Unimplemented)
    }
}

impl Drop for Engine {
    /// Release the engine's resources after stopping worker and device accesses.
    /// Cleanup must also handle partial initialization and unmatched receives.
    fn drop(&mut self) {}
}
