#ifndef RDMA101_TRANSFER_ENGINE_H
#define RDMA101_TRANSFER_ENGINE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * RDMA101 Transfer Engine C API.
 */

#define R101_ABI_VERSION 1u

/* Engine objects are opaque; create and destroy them through this API. */
typedef struct r101_engine r101_engine;

/* Engine-local integer handles. */
typedef uint64_t r101_peer_handle;
typedef uint64_t r101_local_memory_handle;
typedef uint64_t r101_remote_memory_handle;
typedef uint64_t r101_batch_handle;

/* Result codes. */
typedef enum r101_result {
    R101_OK = 0,             /* The operation succeeded. */
    R101_UNIMPLEMENTED = -1, /* The operation has not been implemented. */
    R101_INVALID = -2,       /* Invalid arguments, handle or memory range. */
    R101_BUSY = -3,          /* The resource is still in use. */
    R101_IO_ERROR = -4,      /* A device or communication operation failed. */
    R101_TIMEOUT = -5,       /* An operation exceeded its deadline. */
    R101_UNSUPPORTED = -6,   /* The operation or capability is unavailable. */
    R101_NO_MEMORY = -7,     /* Insufficient memory for the operation. */
    R101_INVALID_STATE = -8, /* The object's current state disallows the operation. */
    R101_CANCELED = -9,      /* Peer destruction canceled the task. */
} r101_result;

/* Memory permissions, combined with bitwise OR. */
typedef uint32_t r101_access_flags;

enum r101_access_flag {
    R101_LOCAL_WRITE = 1u << 0,   /* Allow the device to write local memory. */
    R101_REMOTE_WRITE = 1u << 1,  /* Allow peers to write this region. */
    R101_REMOTE_READ = 1u << 2,   /* Allow peers to read this region. */
    R101_REMOTE_ATOMIC = 1u << 3, /* Allow remote atomic operations. */
};

/* Transfer states. COMPLETED and FAILED are terminal. */
typedef enum r101_transfer_state {
    R101_PENDING = 0,   /* Accepted, with work still queued or in flight. */
    R101_COMPLETED = 1, /* The task completed successfully. */
    R101_FAILED = 2,    /* Failed; remote changes are not rolled back. */
} r101_transfer_state;

/* The incoming operation that completed a RECV task. */
typedef enum r101_receive_kind {
    R101_RECV_MESSAGE = 1,   /* A SEND message was received. */
    R101_RECV_WRITE_IMM = 2, /* A WRITE_IMM notification was received. */
} r101_receive_kind;

/* Operation tags; map explicitly to the corresponding verbs operations. */
typedef enum r101_opcode {
    R101_WRITE = 1,        /* Copy local bytes to remote memory. */
    R101_READ = 2,         /* Copy remote bytes to local memory. */
    R101_SEND = 3,         /* Send one message to a posted receive. */
    R101_RECV = 4,         /* Receive one message or WRITE_IMM notification. */
    R101_WRITE_IMM = 5,    /* Write data and notify one remote receive. */
    R101_COMPARE_SWAP = 6, /* Compare and swap a remote 64-bit value. */
    R101_FETCH_ADD = 7,    /* Add remotely; return the previous 64-bit value. */
} r101_opcode;

/* Request options. Use zero unless an operation-specific flag is needed. */
typedef uint32_t r101_request_flags;

enum r101_request_flag {
    /* WRITE, SEND and WRITE_IMM: require inline for every data WR. The local
     * source need not be registered. Reject unsupported requests. */
    R101_INLINE = 1u << 0,
};

/* Device and port selection. */
typedef struct r101_config {
    uint32_t abi_version; /* Must equal R101_ABI_VERSION. */
    uint32_t port;        /* RDMA port number, starting at 1. */
    uint32_t gid_index;   /* GID table index, starting at 0. */
    const char *device;   /* NUL-terminated name; NULL auto-selects a device. */
} r101_config;

/* Remote virtual address within a region registered by the peer. */
typedef struct r101_rma {
    uint64_t remote_address;
} r101_rma;

/* WRITE_IMM notifies once, after writing all data in the logical request. */
typedef struct r101_write_imm {
    uint64_t remote_address;
    uint32_t immediate; /* Notification value in host byte order. */
} r101_write_imm;

/* Atomics store the previous remote value in the request's local buffer. */
typedef struct r101_atomic {
    uint64_t remote_address;
    uint64_t compare; /* COMPARE_SWAP expected value; unused by FETCH_ADD. */
    uint64_t value;   /* COMPARE_SWAP replacement or FETCH_ADD increment. */
} r101_atomic;

/* Only the member selected by opcode is read; SEND and RECV do not use args. */
typedef union r101_request_args {
    r101_rma rma;             /* WRITE, READ. */
    r101_write_imm write_imm; /* WRITE_IMM. */
    r101_atomic atomic;       /* COMPARE_SWAP, FETCH_ADD. */
} r101_request_args;

/*
 * Transfer request.
 *
 * WRITE, SEND and WRITE_IMM read the local buffer. READ, RECV and atomics write
 * the local buffer and require R101_LOCAL_WRITE on its registered region.
 * Nonempty ranges require registered memory with the appropriate permissions,
 * except that R101_INLINE permits an unregistered local source.
 *
 * Once accepted, buffers remain borrowed until the task becomes COMPLETED or
 * FAILED. Sources must not be modified and destinations must not be accessed
 * during that interval. The application must prevent conflicting accesses
 * between outstanding tasks and ensure that remote ranges remain available for
 * the duration of their use.
 */
typedef struct r101_request {
    r101_peer_handle peer;

    r101_opcode opcode;
    r101_request_flags flags;

    void *local_address; /* Caller-owned memory; may be NULL when length is zero. */
    uint64_t length;     /* Byte count; receive capacity for RECV. */

    r101_request_args args;
} r101_request;

/*
 * Result of one completed RECV. A receive consumes one SEND message or one
 * WRITE_IMM notification and is not automatically posted again.
 *
 * RECV_MESSAGE writes the payload into the receive buffer, reports its actual
 * byte count in length and sets immediate to zero. RECV_WRITE_IMM reports a
 * host-order immediate value and sets length to zero; the receive buffer is
 * unchanged.
 */
typedef struct r101_receive_result {
    r101_receive_kind kind;
    uint32_t immediate;
    uint64_t length;
} r101_receive_result;

/*
 * Result of one transfer request. A FAILED task has ended or reliably stopped
 * all related local device accesses, so its buffer borrow is released. Failure
 * does not undo remote changes or imply that other tasks in the batch have
 * stopped.
 */
typedef struct r101_transfer_status {
    r101_transfer_state state;
    r101_result error;           /* Negative for FAILED; R101_OK otherwise. */
    r101_receive_result receive; /* Valid only for a COMPLETED RECV. */
} r101_transfer_status;

/*
 * Create an engine.
 *
 * Returns R101_OK on success or a negative error code. An invalid
 * configuration, including an ABI version mismatch, returns R101_INVALID.
 * Failure releases any resources created by this call.
 */
r101_result r101_create_engine(const r101_config *config, r101_engine **engine);

/*
 * Stop execution and release all resources owned by an engine.
 *
 * The application must first ensure that peers no longer access its registered
 * memory. This call stops the worker and local device accesses before releasing
 * peers, memory registrations and batch records.
 *
 * Returns R101_OK on success, after which the engine, peer, memory and batch
 * handles and all exported metadata pointers are invalid. Application memory
 * is not freed and may now be released by its owner. A NULL engine returns
 * R101_INVALID.
 */
r101_result r101_destroy_engine(r101_engine *engine);

/*
 * Register caller-owned memory for local and remote device access.
 *
 * The half-open range [address, address + length) must be nonempty, must not
 * overflow and must not overlap another registered region. access combines
 * r101_access_flag values; remote write or atomic access requires
 * R101_LOCAL_WRITE.
 *
 * On success, *memory is a nonzero engine-local registration handle, never
 * reused during this engine's lifetime. Re-registering an address creates a
 * new registration identity. *metadata points to *metadata_size bytes of
 * non-empty, opaque data describing this registration, independent of any peer.
 * Share them only with peers that need remote access. The engine owns these
 * bytes; the caller must not modify or free them.
 *
 * Returns R101_OK on success, R101_INVALID for an invalid range or permissions,
 * or another negative error code. Failure rolls back this registration.
 */
r101_result r101_register_local_memory(r101_engine *engine,
                                       void *address,
                                       uint64_t length,
                                       r101_access_flags access,
                                       r101_local_memory_handle *memory,
                                       const void **metadata,
                                       uint64_t *metadata_size);

/*
 * Deregister one local region.
 *
 * The application must first stop remote submissions and confirm that all
 * remote accesses to this registration have ended.
 *
 * Returns R101_BUSY if any accepted local task still borrows it, including
 * queued work and unmatched receives. This call does not wait or cancel work.
 * Returns R101_OK on success. The memory handle and metadata pointer are then
 * invalidated. The application may then free or reuse the underlying memory.
 */
r101_result r101_deregister_local_memory(r101_engine *engine, r101_local_memory_handle memory);

/*
 * Import a remote registration for a connected peer.
 *
 * On success, *remote_memory is a nonzero engine-local handle, never reused
 * after removal. Reimporting the same live registration for the same peer
 * returns the existing handle. One successful `r101_remove_remote_memory`
 * invalidates all copies of that handle.
 *
 * Conflicting overlapping registrations for that peer are rejected, not
 * replaced. The application must not import metadata from an already
 * deregistered region.
 *
 * Returns R101_OK on success, R101_INVALID_STATE for a valid but unconnected
 * peer, or R101_INVALID for an unknown peer or malformed/conflicting metadata.
 */
r101_result r101_import_remote_memory(r101_engine *engine,
                                      r101_peer_handle peer,
                                      const void *metadata,
                                      uint64_t metadata_size,
                                      r101_remote_memory_handle *remote_memory);

/*
 * Remove an imported remote memory descriptor from this engine.
 *
 * Returns R101_BUSY while an accepted task uses it, including work still queued in
 * the engine. This call does not wait or cancel work.
 * Returns R101_OK on success. The handle is then invalidated and the range is removed
 * from this peer's lookup table; future requests to that range are rejected until a
 * valid import exists.
 */
r101_result r101_remove_remote_memory(r101_engine *engine, r101_remote_memory_handle remote_memory);

/*
 * Create a local peer and export its connection metadata.
 *
 * Creates local RC connection resources in INIT and encodes only connection
 * parameters, independently of memory registrations. A peer handle is local
 * to its engine and need not match the remote peer's handle.
 *
 * On success, *peer is nonzero and is never reused during this engine's
 * lifetime. *metadata points to a nonempty blob of *metadata_size bytes. The
 * engine owns these bytes; they remain immutable and at a stable address until
 * this peer or the engine is destroyed. The caller must not modify or free them.
 *
 * Returns R101_OK on success or a negative error code. Failure rolls back this
 * creation.
 */
r101_result r101_create_peer(r101_engine *engine,
                             r101_peer_handle *peer,
                             const void **metadata,
                             uint64_t *metadata_size);

/*
 * Complete the local side of a connection using the remote peer's metadata.
 *
 * peer must be a handle returned by r101_create_peer() on this engine. metadata
 * contains metadata_size bytes exported for this connection by the remote peer.
 * Input bytes are borrowed for this call; the engine copies all retained data
 * and configures the local QP for RTR/RTS. RECV may be submitted before this
 * call. For other operations, the application must separately ensure that both
 * sides have connected before submission.
 *
 * Returns R101_OK on local connection success, R101_INVALID for an unknown peer
 * or empty/malformed metadata, and R101_INVALID_STATE if the peer is already
 * connected.
 * Other failures return a negative error code. A peer with failed connection
 * must be destroyed with r101_destroy_peer() or r101_destroy_engine().
 */
r101_result r101_connect_peer(r101_engine *engine,
                              r101_peer_handle peer,
                              const void *metadata,
                              uint64_t metadata_size);

/*
 * Destroy one local peer and remove all remote memory descriptors imported for it.
 *
 * Stop accepting and posting new work for this peer. Tasks still PENDING when
 * shutdown starts become FAILED with R101_CANCELED. Already applied remote changes
 * are not rolled back.
 *
 * Wait for local cleanup, including completion handling, before returning
 * R101_OK. This call sends no close handshake and does not destroy the remote peer;
 * the application coordinates remote shutdown separately. Unconnected peers and
 * peers with failed connection setup can also be destroyed.
 *
 * Returns R101_OK on success. The peer handle, its exported connection metadata and all
 * its imported remote memory handles are then invalidated.
 * Unknown handles return R101_INVALID.
 */
r101_result r101_destroy_peer(r101_engine *engine, r101_peer_handle peer);

/*
 * Submit a nonempty array of transfer requests as a new batch.
 *
 * Returns R101_OK when the whole batch is accepted. Invalid requests or unknown
 * peers return R101_INVALID. An unconnected peer used for an operation other
 * than RECV, or a peer with failed setup or shutdown, returns R101_INVALID_STATE.
 * An unavailable operation or an unsatisfiable Inline requirement returns
 * R101_UNSUPPORTED. Insufficient resources return R101_NO_MEMORY. Any failure
 * accepts no tasks and creates no batch.
 */
r101_result r101_submit_transfer(r101_engine *engine,
                                 const r101_request *requests,
                                 uint64_t count,
                                 r101_batch_handle *batch);

/*
 * Read every task's status into caller-owned storage in submission order.
 *
 * count must equal the batch's original request count, not a capacity or prefix
 * length. statuses must have room for count entries and may be uninitialized. A
 * successful query fills every entry.
 *
 * This call reads state without posting work, polling CQs or scheduling tasks.
 * Query errors do not wait or cancel tasks.
 *
 * Returns R101_OK when the query succeeds, even if tasks are PENDING or FAILED.
 * An unknown batch, NULL statuses or an incorrect count returns R101_INVALID.
 */
r101_result r101_get_transfer_statuses(r101_engine *engine,
                                       r101_batch_handle batch,
                                       r101_transfer_status *statuses,
                                       uint64_t count);

/*
 * Release the task and completion records of a terminal batch.
 *
 * All tasks must be COMPLETED or FAILED. This call does not wait for or cancel
 * pending transfers.
 *
 * Returns R101_OK on success, after which the batch is invalid. Returns
 * R101_BUSY if any task is PENDING, or R101_INVALID if batch is unknown. A live
 * batch remains valid when the call fails.
 */
r101_result r101_free_batch(r101_engine *engine, r101_batch_handle batch);

#ifdef __cplusplus
}
#endif

#endif /* RDMA101_TRANSFER_ENGINE_H */
