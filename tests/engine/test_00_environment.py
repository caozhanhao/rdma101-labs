"""Lab 0: independent RDMA communication and Engine C ABI smoke checks."""

import pytest

from rdma101.bindings import constants as R101
from rdma101.bindings import ffi, library
from scripts import doctor

pytestmark = pytest.mark.lab0


@pytest.mark.rdma
def test_rdma_environment(engine_config):
    assert engine_config.device, "set R101_DEVICE or pass --device for the environment check"
    result = doctor.main(
        [
            "--check",
            "--device",
            engine_config.device,
            "--port",
            str(engine_config.port),
            "--gid-index",
            str(engine_config.gid_index),
        ]
    )
    if result == 130:
        raise KeyboardInterrupt
    assert result == 0, "RDMA environment check failed; see doctor output"


def test_library_exports_all_api_symbols(library_path):
    library(library_path)


def test_create_and_destroy_reject_null_pointers(library_path):
    lib = library(library_path)
    handle = ffi.new("r101_engine **", ffi.cast("r101_engine *", 123))
    assert lib.r101_create_engine(ffi.NULL, handle) == R101.INVALID
    assert handle[0] == ffi.NULL
    assert lib.r101_create_engine(ffi.new("r101_config *"), ffi.NULL) == R101.INVALID
    assert lib.r101_destroy_engine(ffi.NULL) == R101.INVALID


def test_create_rejects_wrong_abi_version_and_clears_output(library_path):
    lib = library(library_path)
    config = ffi.new("r101_config *", {"abi_version": R101.ABI_VERSION + 1})
    handle = ffi.new("r101_engine **", ffi.cast("r101_engine *", 123))
    assert lib.r101_create_engine(config, handle) == R101.INVALID
    assert handle[0] == ffi.NULL


def test_peer_functions_reject_null_engine(library_path):
    lib = library(library_path)
    peer = ffi.new("r101_peer_handle *", 123)
    metadata = ffi.new("const void **", ffi.cast("void *", 123))
    size = ffi.new("uint64_t *", 123)
    assert lib.r101_create_peer(ffi.NULL, peer, metadata, size) == R101.INVALID
    assert peer[0] == size[0] == 0 and metadata[0] == ffi.NULL
    data = ffi.new("char[]", b"metadata")
    assert lib.r101_connect_peer(ffi.NULL, 1, data, len(data)) == R101.INVALID
    assert lib.r101_destroy_peer(ffi.NULL, 1) == R101.INVALID


def test_memory_functions_reject_null_engine(library_path):
    lib = library(library_path)
    storage = ffi.new("char[]", 8)
    memory = ffi.new("r101_local_memory_handle *", 123)
    metadata = ffi.new("const void **", ffi.cast("void *", 123))
    size = ffi.new("uint64_t *", 123)
    assert (
        lib.r101_register_local_memory(
            ffi.NULL, storage, len(storage), R101.LOCAL_WRITE, memory, metadata, size
        )
        == R101.INVALID
    )
    assert memory[0] == size[0] == 0 and metadata[0] == ffi.NULL
    remote = ffi.new("r101_remote_memory_handle *", 123)
    assert lib.r101_import_remote_memory(ffi.NULL, 1, storage, len(storage), remote) == R101.INVALID
    assert remote[0] == 0
    assert lib.r101_deregister_local_memory(ffi.NULL, 1) == R101.INVALID
    assert lib.r101_remove_remote_memory(ffi.NULL, 1) == R101.INVALID


def test_batch_functions_reject_null_engine(library_path):
    lib = library(library_path)
    requests = ffi.new("r101_request[]", 1)
    batch = ffi.new("r101_batch_handle *", 123)
    assert lib.r101_submit_transfer(ffi.NULL, requests, len(requests), batch) == R101.INVALID
    assert batch[0] == 0
    statuses = ffi.new("r101_transfer_status[]", 1)
    assert lib.r101_get_transfer_statuses(ffi.NULL, 1, statuses, len(statuses)) == R101.INVALID
    assert lib.r101_free_batch(ffi.NULL, 1) == R101.INVALID
