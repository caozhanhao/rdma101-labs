// Generated from transfer-engine/transfer_engine.h by scripts/generate_rust_bindings.py.
// Do not edit; regenerate with bindgen 0.72.1.
#![allow(non_camel_case_types, dead_code)]
pub const R101_ABI_VERSION: u32 = 1;
#[repr(C)]
#[derive(Copy, Clone)]
pub struct r101_engine {
    _unused: [u8; 0],
}
pub type r101_peer_handle = u64;
pub type r101_local_memory_handle = u64;
pub type r101_remote_memory_handle = u64;
pub type r101_batch_handle = u64;
pub const R101_OK: r101_result = 0;
pub const R101_UNIMPLEMENTED: r101_result = -1;
pub const R101_INVALID: r101_result = -2;
pub const R101_BUSY: r101_result = -3;
pub const R101_IO_ERROR: r101_result = -4;
pub const R101_TIMEOUT: r101_result = -5;
pub const R101_UNSUPPORTED: r101_result = -6;
pub const R101_NO_MEMORY: r101_result = -7;
pub const R101_INVALID_STATE: r101_result = -8;
pub const R101_CANCELED: r101_result = -9;
pub type r101_result = ::std::os::raw::c_int;
pub type r101_access_flags = u32;
pub const R101_LOCAL_WRITE: r101_access_flag = 1;
pub const R101_REMOTE_WRITE: r101_access_flag = 2;
pub const R101_REMOTE_READ: r101_access_flag = 4;
pub const R101_REMOTE_ATOMIC: r101_access_flag = 8;
pub type r101_access_flag = ::std::os::raw::c_uint;
pub const R101_PENDING: r101_transfer_state = 0;
pub const R101_COMPLETED: r101_transfer_state = 1;
pub const R101_FAILED: r101_transfer_state = 2;
pub type r101_transfer_state = ::std::os::raw::c_uint;
pub const R101_RECV_MESSAGE: r101_receive_kind = 1;
pub const R101_RECV_WRITE_IMM: r101_receive_kind = 2;
pub type r101_receive_kind = ::std::os::raw::c_uint;
pub const R101_WRITE: r101_opcode = 1;
pub const R101_READ: r101_opcode = 2;
pub const R101_SEND: r101_opcode = 3;
pub const R101_RECV: r101_opcode = 4;
pub const R101_WRITE_IMM: r101_opcode = 5;
pub const R101_COMPARE_SWAP: r101_opcode = 6;
pub const R101_FETCH_ADD: r101_opcode = 7;
pub type r101_opcode = ::std::os::raw::c_uint;
pub type r101_request_flags = u32;
pub const R101_INLINE: r101_request_flag = 1;
pub type r101_request_flag = ::std::os::raw::c_uint;
#[repr(C)]
#[derive(Copy, Clone)]
pub struct r101_config {
    pub abi_version: u32,
    pub port: u32,
    pub gid_index: u32,
    pub device: *const ::std::os::raw::c_char,
}
#[repr(C)]
#[derive(Copy, Clone)]
pub struct r101_rma {
    pub remote_address: u64,
}
#[repr(C)]
#[derive(Copy, Clone)]
pub struct r101_write_imm {
    pub remote_address: u64,
    pub immediate: u32,
}
#[repr(C)]
#[derive(Copy, Clone)]
pub struct r101_atomic {
    pub remote_address: u64,
    pub compare: u64,
    pub value: u64,
}
#[repr(C)]
#[derive(Copy, Clone)]
pub union r101_request_args {
    pub rma: r101_rma,
    pub write_imm: r101_write_imm,
    pub atomic: r101_atomic,
}
#[repr(C)]
#[derive(Copy, Clone)]
pub struct r101_request {
    pub peer: r101_peer_handle,
    pub opcode: r101_opcode,
    pub flags: r101_request_flags,
    pub local_address: *mut ::std::os::raw::c_void,
    pub length: u64,
    pub args: r101_request_args,
}
#[repr(C)]
#[derive(Copy, Clone)]
pub struct r101_receive_result {
    pub kind: r101_receive_kind,
    pub immediate: u32,
    pub length: u64,
}
#[repr(C)]
#[derive(Copy, Clone)]
pub struct r101_transfer_status {
    pub state: r101_transfer_state,
    pub error: r101_result,
    pub receive: r101_receive_result,
}
pub type r101_create_engine_signature = ::std::option::Option<
    unsafe extern "C" fn(arg1: *const r101_config, arg2: *mut *mut r101_engine) -> r101_result,
>;
pub type r101_destroy_engine_signature =
    ::std::option::Option<unsafe extern "C" fn(arg1: *mut r101_engine) -> r101_result>;
pub type r101_register_local_memory_signature = ::std::option::Option<
    unsafe extern "C" fn(
        arg1: *mut r101_engine,
        arg2: *mut ::std::os::raw::c_void,
        arg3: u64,
        arg4: r101_access_flags,
        arg5: *mut r101_local_memory_handle,
        arg6: *mut *const ::std::os::raw::c_void,
        arg7: *mut u64,
    ) -> r101_result,
>;
pub type r101_deregister_local_memory_signature = ::std::option::Option<
    unsafe extern "C" fn(arg1: *mut r101_engine, arg2: r101_local_memory_handle) -> r101_result,
>;
pub type r101_import_remote_memory_signature = ::std::option::Option<
    unsafe extern "C" fn(
        arg1: *mut r101_engine,
        arg2: r101_peer_handle,
        arg3: *const ::std::os::raw::c_void,
        arg4: u64,
        arg5: *mut r101_remote_memory_handle,
    ) -> r101_result,
>;
pub type r101_remove_remote_memory_signature = ::std::option::Option<
    unsafe extern "C" fn(arg1: *mut r101_engine, arg2: r101_remote_memory_handle) -> r101_result,
>;
pub type r101_create_peer_signature = ::std::option::Option<
    unsafe extern "C" fn(
        arg1: *mut r101_engine,
        arg2: *mut r101_peer_handle,
        arg3: *mut *const ::std::os::raw::c_void,
        arg4: *mut u64,
    ) -> r101_result,
>;
pub type r101_connect_peer_signature = ::std::option::Option<
    unsafe extern "C" fn(
        arg1: *mut r101_engine,
        arg2: r101_peer_handle,
        arg3: *const ::std::os::raw::c_void,
        arg4: u64,
    ) -> r101_result,
>;
pub type r101_destroy_peer_signature = ::std::option::Option<
    unsafe extern "C" fn(arg1: *mut r101_engine, arg2: r101_peer_handle) -> r101_result,
>;
pub type r101_submit_transfer_signature = ::std::option::Option<
    unsafe extern "C" fn(
        arg1: *mut r101_engine,
        arg2: *const r101_request,
        arg3: u64,
        arg4: *mut r101_batch_handle,
    ) -> r101_result,
>;
pub type r101_get_transfer_statuses_signature = ::std::option::Option<
    unsafe extern "C" fn(
        arg1: *mut r101_engine,
        arg2: r101_batch_handle,
        arg3: *mut r101_transfer_status,
        arg4: u64,
    ) -> r101_result,
>;
pub type r101_free_batch_signature = ::std::option::Option<
    unsafe extern "C" fn(arg1: *mut r101_engine, arg2: r101_batch_handle) -> r101_result,
>;

// Check every exported function against its C signature.
const _: r101_create_engine_signature = Some(crate::ffi::r101_create_engine);
const _: r101_destroy_engine_signature = Some(crate::ffi::r101_destroy_engine);
const _: r101_register_local_memory_signature = Some(crate::ffi::r101_register_local_memory);
const _: r101_deregister_local_memory_signature = Some(crate::ffi::r101_deregister_local_memory);
const _: r101_import_remote_memory_signature = Some(crate::ffi::r101_import_remote_memory);
const _: r101_remove_remote_memory_signature = Some(crate::ffi::r101_remove_remote_memory);
const _: r101_create_peer_signature = Some(crate::ffi::r101_create_peer);
const _: r101_connect_peer_signature = Some(crate::ffi::r101_connect_peer);
const _: r101_destroy_peer_signature = Some(crate::ffi::r101_destroy_peer);
const _: r101_submit_transfer_signature = Some(crate::ffi::r101_submit_transfer);
const _: r101_get_transfer_statuses_signature = Some(crate::ffi::r101_get_transfer_statuses);
const _: r101_free_batch_signature = Some(crate::ffi::r101_free_batch);
