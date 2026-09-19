// Test double for the Rust adapter. No device operations or data transfers.
pub use crate::native::*;
use std::ffi::c_void;

pub struct Engine {
    metadata: Vec<u8>,
    buffer: *mut c_void,
    saved: Vec<Request>,
    submissions: u64,
}

impl Engine {
    pub fn create(config: Config) -> Result<Box<Self>, Error> {
        let mut metadata = config.device.unwrap_or_else(|| "<auto>".into());
        for n in [config.port, config.gid_index] {
            metadata.push_str(&format!("|{n}"));
        }
        Ok(Box::new(Self {
            metadata: metadata.into_bytes(),
            buffer: std::ptr::null_mut(),
            saved: Vec::new(),
            submissions: 0,
        }))
    }
    pub unsafe fn register_local_memory(
        &mut self,
        address: *mut c_void,
        length: usize,
        access: MemoryAccess,
    ) -> Result<MemoryMetadata<'_>, Error> {
        let mut mask = 0;
        for (flag, bit) in [
            (MemoryAccess::LOCAL_WRITE, 1),
            (MemoryAccess::REMOTE_WRITE, 2),
            (MemoryAccess::REMOTE_READ, 4),
            (MemoryAccess::REMOTE_ATOMIC, 8),
        ] {
            if access.contains(flag) {
                mask |= bit;
            }
        }
        self.buffer = address;
        if length == mask + 1 {
            Ok(MemoryMetadata {
                memory: 77,
                metadata: &[0, 255, b'M', b'R', 128],
            })
        } else {
            Err(Error::Invalid)
        }
    }
    pub unsafe fn deregister_local_memory(&mut self, memory: MemoryId) -> Result<(), Error> {
        match memory {
            77 => Ok(()),
            78 => Err(Error::Busy),
            _ => Err(Error::Invalid),
        }
    }
    pub fn import_remote_memory(
        &mut self,
        peer: PeerId,
        metadata: &[u8],
    ) -> Result<RemoteMemoryId, Error> {
        if peer == 41 && metadata == [0, 255, b'M', b'R', 128] {
            Ok(91)
        } else {
            Err(Error::Invalid)
        }
    }
    pub fn remove_remote_memory(&mut self, memory: RemoteMemoryId) -> Result<(), Error> {
        match memory {
            91 => Ok(()),
            92 => Err(Error::Busy),
            _ => Err(Error::Invalid),
        }
    }
    pub fn create_peer(&mut self) -> Result<PeerMetadata<'_>, Error> {
        Ok(PeerMetadata {
            peer: 41,
            metadata: &self.metadata,
        })
    }
    pub fn connect_peer(&mut self, peer: PeerId, metadata: &[u8]) -> Result<(), Error> {
        if peer == 41 && metadata == self.metadata {
            Ok(())
        } else {
            Err(Error::Invalid)
        }
    }
    pub fn destroy_peer(&mut self, peer: PeerId) -> Result<(), Error> {
        match peer {
            41 => Ok(()),
            42 => Err(Error::Io),
            _ => Err(Error::Invalid),
        }
    }
    pub unsafe fn submit_transfer(&mut self, requests: Vec<Request>) -> Result<BatchId, Error> {
        self.submissions += 1;
        if requests.len() != 8
            || requests
                .iter()
                .any(|r| r.peer != 41 || r.local_address != self.buffer || r.length != 8)
        {
            return Err(Error::Invalid);
        }
        let remote = 0xfedcba9876543210;
        let compare_value = 0x1122334455667788;
        let operand = 0xffeeddccbbaa0099;
        let valid = matches!(requests[0].operation, Operation::Write { remote_address, inline_data: true } if remote_address == remote)
            && matches!(requests[1].operation, Operation::Read { remote_address } if remote_address == remote)
            && matches!(requests[2].operation, Operation::Send { inline_data: true })
            && matches!(requests[3].operation, Operation::Recv)
            && matches!(requests[4].operation, Operation::WriteImm { remote_address, immediate, inline_data: true }
                if remote_address == remote && immediate == 0x89abcdef)
            && matches!(requests[5].operation, Operation::CompareSwap { remote_address, compare, value }
                if remote_address == remote && compare == compare_value && value == operand)
            && matches!(requests[6].operation, Operation::FetchAdd { remote_address, value }
                if remote_address == remote && value == operand)
            && matches!(
                requests[7].operation,
                Operation::Send { inline_data: false }
            );
        if !valid {
            return Err(Error::Invalid);
        }
        self.saved = requests;
        Ok(self.submissions)
    }
    pub fn get_transfer_statuses(
        &self,
        batch: BatchId,
        output: &mut [TransferStatus],
    ) -> Result<(), Error> {
        if let Some(first) = output.first_mut() {
            *first = TransferStatus::Completed { receive: None };
        }
        if batch != self.submissions || output.len() != self.saved.len() || self.saved.is_empty() {
            return Err(Error::Invalid);
        }
        let Operation::WriteImm { immediate, .. } = self.saved[4].operation else {
            unreachable!()
        };
        output.copy_from_slice(&[
            TransferStatus::Pending,
            TransferStatus::Failed(Error::Io),
            TransferStatus::Completed {
                receive: Some(ReceiveResult::Message {
                    length: self.saved[2].length,
                }),
            },
            TransferStatus::Completed {
                receive: Some(ReceiveResult::WriteNotice { immediate }),
            },
            TransferStatus::Failed(Error::Canceled),
            TransferStatus::Failed(Error::Unsupported),
            TransferStatus::Failed(Error::NoMemory),
            TransferStatus::Completed { receive: None },
        ]);
        Ok(())
    }
    pub fn free_batch(&mut self, batch: BatchId) -> Result<(), Error> {
        let errors = [
            Error::Unimplemented,
            Error::Invalid,
            Error::Busy,
            Error::Io,
            Error::Timeout,
            Error::Unsupported,
            Error::NoMemory,
            Error::InvalidState,
            Error::Canceled,
        ];
        if batch <= errors.len() as u64 {
            Err(errors[batch as usize - 1])
        } else {
            Ok(())
        }
    }
}
