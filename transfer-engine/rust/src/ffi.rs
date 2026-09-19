use crate::{bindings::*, engine::*};
use std::ffi::{c_void, CStr};
use std::mem::{align_of, size_of};
use std::{ptr, slice};

fn encode_error(error: Error) -> r101_result {
    match error {
        Error::Unimplemented => R101_UNIMPLEMENTED,
        Error::Invalid => R101_INVALID,
        Error::Busy => R101_BUSY,
        Error::Io => R101_IO_ERROR,
        Error::Timeout => R101_TIMEOUT,
        Error::Unsupported => R101_UNSUPPORTED,
        Error::NoMemory => R101_NO_MEMORY,
        Error::InvalidState => R101_INVALID_STATE,
        Error::Canceled => R101_CANCELED,
    }
}

fn call(operation: impl FnOnce() -> Result<(), Error>) -> r101_result {
    match operation() {
        Ok(()) => R101_OK,
        Err(error) => encode_error(error),
    }
}

// Return a checked element count for a C array; its byte length must fit in isize.
fn array_len<T>(data: *const T, count: u64) -> Result<usize, Error> {
    let count = usize::try_from(count).map_err(|_| Error::Invalid)?;
    if data.is_null()
        || count == 0
        || count > isize::MAX as usize / size_of::<T>()
        || (data as usize) % align_of::<T>() != 0
    {
        return Err(Error::Invalid);
    }
    Ok(count)
}

unsafe fn engine_mut<'a>(engine: *mut r101_engine) -> Result<&'a mut Engine, Error> {
    engine.cast::<Engine>().as_mut().ok_or(Error::Invalid)
}

unsafe fn decode_config(input: &r101_config) -> Result<Config, Error> {
    if input.abi_version != R101_ABI_VERSION {
        return Err(Error::Invalid);
    }
    let device = if input.device.is_null() {
        None
    } else {
        let name = CStr::from_ptr(input.device)
            .to_str()
            .map_err(|_| Error::Invalid)?;
        let mut owned = String::new();
        owned
            .try_reserve_exact(name.len())
            .map_err(|_| Error::NoMemory)?;
        owned.push_str(name);
        Some(owned)
    };
    Ok(Config {
        port: input.port,
        gid_index: input.gid_index,
        device,
    })
}

fn decode_access(flags: r101_access_flags) -> Result<MemoryAccess, Error> {
    let known = R101_LOCAL_WRITE | R101_REMOTE_WRITE | R101_REMOTE_READ | R101_REMOTE_ATOMIC;
    if flags & !known != 0 {
        return Err(Error::Invalid);
    }
    let mut access = MemoryAccess::NONE;
    for (flag, native) in [
        (R101_LOCAL_WRITE, MemoryAccess::LOCAL_WRITE),
        (R101_REMOTE_WRITE, MemoryAccess::REMOTE_WRITE),
        (R101_REMOTE_READ, MemoryAccess::REMOTE_READ),
        (R101_REMOTE_ATOMIC, MemoryAccess::REMOTE_ATOMIC),
    ] {
        if flags & flag != 0 {
            access = access | native;
        }
    }
    Ok(access)
}

// Convert a C request into a native Request after checking its opcode and flags.
unsafe fn decode_request(input: &r101_request) -> Result<Request, Error> {
    if input.flags != 0 && input.flags != R101_INLINE {
        return Err(Error::Invalid);
    }
    let inline_data = input.flags == R101_INLINE;
    let supports_inline = matches!(input.opcode, R101_WRITE | R101_SEND | R101_WRITE_IMM);
    if inline_data && !supports_inline {
        return Err(Error::Invalid);
    }
    let operation = match input.opcode {
        R101_WRITE => Operation::Write {
            remote_address: input.args.rma.remote_address,
            inline_data,
        },
        R101_READ => Operation::Read {
            remote_address: input.args.rma.remote_address,
        },
        R101_SEND => Operation::Send { inline_data },
        R101_RECV => Operation::Recv,
        R101_WRITE_IMM => Operation::WriteImm {
            remote_address: input.args.write_imm.remote_address,
            immediate: input.args.write_imm.immediate,
            inline_data,
        },
        R101_COMPARE_SWAP => Operation::CompareSwap {
            remote_address: input.args.atomic.remote_address,
            compare: input.args.atomic.compare,
            value: input.args.atomic.value,
        },
        R101_FETCH_ADD => Operation::FetchAdd {
            remote_address: input.args.atomic.remote_address,
            value: input.args.atomic.value,
        },
        _ => return Err(Error::Invalid),
    };
    Ok(Request {
        peer: input.peer,
        local_address: input.local_address,
        length: input.length,
        operation,
    })
}

fn encode_status(input: TransferStatus) -> r101_transfer_status {
    let mut output = r101_transfer_status {
        state: R101_PENDING,
        error: R101_OK,
        receive: r101_receive_result {
            kind: 0,
            immediate: 0,
            length: 0,
        },
    };
    match input {
        TransferStatus::Pending => (),
        TransferStatus::Failed(error) => {
            output.state = R101_FAILED;
            output.error = encode_error(error);
        }
        TransferStatus::Completed { receive } => {
            output.state = R101_COMPLETED;
            match receive {
                Some(ReceiveResult::Message { length }) => {
                    output.receive.kind = R101_RECV_MESSAGE;
                    output.receive.length = length;
                }
                Some(ReceiveResult::WriteNotice { immediate }) => {
                    output.receive.kind = R101_RECV_WRITE_IMM;
                    output.receive.immediate = immediate;
                }
                None => (),
            }
        }
    }
    output
}

#[no_mangle]
pub unsafe extern "C" fn r101_create_engine(
    config: *const r101_config,
    engine: *mut *mut r101_engine,
) -> r101_result {
    if engine.is_null() {
        return R101_INVALID;
    }
    engine.write(ptr::null_mut());
    if config.is_null() {
        return R101_INVALID;
    }
    call(|| {
        let local = Engine::create(decode_config(&*config)?)?;
        engine.write(Box::into_raw(local).cast());
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn r101_destroy_engine(engine: *mut r101_engine) -> r101_result {
    if engine.is_null() {
        return R101_INVALID;
    }
    drop(Box::from_raw(engine.cast::<Engine>()));
    R101_OK
}

#[no_mangle]
pub unsafe extern "C" fn r101_register_local_memory(
    engine: *mut r101_engine,
    address: *mut c_void,
    length: u64,
    access: r101_access_flags,
    memory: *mut r101_local_memory_handle,
    metadata: *mut *const c_void,
    metadata_size: *mut u64,
) -> r101_result {
    if memory.is_null() || metadata.is_null() || metadata_size.is_null() {
        return R101_INVALID;
    }
    memory.write(0);
    metadata.write(ptr::null());
    metadata_size.write(0);
    call(|| {
        let length = array_len(address.cast::<u8>(), length)?;
        let local =
            engine_mut(engine)?.register_local_memory(address, length, decode_access(access)?)?;
        memory.write(local.memory);
        metadata.write(local.metadata.as_ptr().cast());
        metadata_size.write(local.metadata.len() as u64);
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn r101_deregister_local_memory(
    engine: *mut r101_engine,
    memory: r101_local_memory_handle,
) -> r101_result {
    if memory == 0 {
        return R101_INVALID;
    }
    call(|| engine_mut(engine)?.deregister_local_memory(memory))
}

#[no_mangle]
pub unsafe extern "C" fn r101_import_remote_memory(
    engine: *mut r101_engine,
    peer: r101_peer_handle,
    metadata: *const c_void,
    metadata_size: u64,
    remote_memory: *mut r101_remote_memory_handle,
) -> r101_result {
    if remote_memory.is_null() {
        return R101_INVALID;
    }
    remote_memory.write(0);
    if peer == 0 {
        return R101_INVALID;
    }
    call(|| {
        let bytes = metadata.cast::<u8>();
        let size = array_len(bytes, metadata_size)?;
        let id =
            engine_mut(engine)?.import_remote_memory(peer, slice::from_raw_parts(bytes, size))?;
        remote_memory.write(id);
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn r101_remove_remote_memory(
    engine: *mut r101_engine,
    remote_memory: r101_remote_memory_handle,
) -> r101_result {
    if remote_memory == 0 {
        return R101_INVALID;
    }
    call(|| engine_mut(engine)?.remove_remote_memory(remote_memory))
}

#[no_mangle]
pub unsafe extern "C" fn r101_create_peer(
    engine: *mut r101_engine,
    peer: *mut r101_peer_handle,
    metadata: *mut *const c_void,
    metadata_size: *mut u64,
) -> r101_result {
    if peer.is_null() || metadata.is_null() || metadata_size.is_null() {
        return R101_INVALID;
    }
    peer.write(0);
    metadata.write(ptr::null());
    metadata_size.write(0);
    call(|| {
        let local = engine_mut(engine)?.create_peer()?;
        peer.write(local.peer);
        metadata.write(local.metadata.as_ptr().cast());
        metadata_size.write(local.metadata.len() as u64);
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn r101_connect_peer(
    engine: *mut r101_engine,
    peer: r101_peer_handle,
    metadata: *const c_void,
    metadata_size: u64,
) -> r101_result {
    if peer == 0 {
        return R101_INVALID;
    }
    call(|| {
        let bytes = metadata.cast::<u8>();
        let metadata_size = array_len(bytes, metadata_size)?;
        engine_mut(engine)?.connect_peer(peer, slice::from_raw_parts(bytes, metadata_size))
    })
}

#[no_mangle]
pub unsafe extern "C" fn r101_destroy_peer(
    engine: *mut r101_engine,
    peer: r101_peer_handle,
) -> r101_result {
    if peer == 0 {
        return R101_INVALID;
    }
    call(|| engine_mut(engine)?.destroy_peer(peer))
}

#[no_mangle]
pub unsafe extern "C" fn r101_submit_transfer(
    engine: *mut r101_engine,
    requests: *const r101_request,
    count: u64,
    batch: *mut r101_batch_handle,
) -> r101_result {
    if batch.is_null() {
        return R101_INVALID;
    }
    batch.write(0);
    call(|| {
        let count = array_len(requests, count)?;
        let engine = engine_mut(engine)?;
        let mut native = Vec::new();
        native
            .try_reserve_exact(count)
            .map_err(|_| Error::NoMemory)?;
        for request in slice::from_raw_parts(requests, count) {
            native.push(decode_request(request)?);
        }
        let id = engine.submit_transfer(native)?;
        batch.write(id);
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn r101_get_transfer_statuses(
    engine: *mut r101_engine,
    batch: r101_batch_handle,
    statuses: *mut r101_transfer_status,
    count: u64,
) -> r101_result {
    if batch == 0 {
        return R101_INVALID;
    }
    call(|| {
        let count = array_len(statuses, count)?;
        let engine = engine.cast::<Engine>().as_ref().ok_or(Error::Invalid)?;
        let mut native = Vec::new();
        native
            .try_reserve_exact(count)
            .map_err(|_| Error::NoMemory)?;
        native.resize(count, TransferStatus::Pending);
        engine.get_transfer_statuses(batch, &mut native)?;
        // Commit only after the query succeeds; caller storage may be uninitialized.
        for (index, status) in native.into_iter().enumerate() {
            statuses.add(index).write(encode_status(status));
        }
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn r101_free_batch(
    engine: *mut r101_engine,
    batch: r101_batch_handle,
) -> r101_result {
    if batch == 0 {
        return R101_INVALID;
    }
    call(|| engine_mut(engine)?.free_batch(batch))
}
