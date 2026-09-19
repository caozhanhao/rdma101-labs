"""One set of C API checks against both native adapters and small Engine doubles."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rdma101.bindings import constants as R101
from rdma101.bindings import ffi, library

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Engine adapters target Linux")


@pytest.fixture(scope="session", params=["cpp", "rust"])
def adapter_library(request, tmp_path_factory):
    directory = tmp_path_factory.mktemp("native-" + request.param)
    output = directory / "adapter.so"
    if request.param == "cpp":
        source = ROOT / "transfer-engine/cpp"
        subprocess.run(
            shlex.split(os.environ.get("CXX", "c++"))
            + [
                "-std=c++20",
                "-shared",
                "-fPIC",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-Wpedantic",
                "-I",
                str(source),
                "-I",
                str(source.parent),
                str(source / "ffi.cpp"),
                str(Path(__file__).with_name("fake_engine.cpp")),
                "-o",
                str(output),
            ],
            check=True,
        )
    else:
        source = ROOT / "transfer-engine/rust"
        modules = {
            "native": source / "src/engine.rs",
            "bindings": source / "src/bindings.rs",
            "ffi": source / "src/ffi.rs",
            "engine": Path(__file__).with_name("fake_engine.rs"),
        }
        (directory / "lib.rs").write_text(
            "".join(
                f"#[path = {json.dumps(str(path))}] mod {name};\n" for name, path in modules.items()
            )
        )
        manifest = (
            (source / "Cargo.toml")
            .read_text()
            .replace("[lib]", '[lib]\nname = "rdma101_adapter_test"\npath = "lib.rs"')
        )
        (directory / "Cargo.toml").write_text(manifest)
        shutil.copyfile(source / "Cargo.lock", directory / "Cargo.lock")
        target = ROOT / "build/rust"
        subprocess.run(
            [
                "cargo",
                "build",
                "--manifest-path",
                str(directory / "Cargo.toml"),
                "--target-dir",
                str(target),
                "--locked",
                "--offline",
            ],
            check=True,
        )
        shutil.copyfile(target / "debug/librdma101_adapter_test.so", output)
    return library(output)


@pytest.mark.parametrize("device", [None, b"", "设备-rxe0".encode()])
def test_config_and_peer_metadata(adapter_library, device):
    lib = adapter_library
    storage = ffi.new("char[]", device) if device is not None else ffi.NULL
    config = ffi.new(
        "r101_config *",
        dict(abi_version=R101.ABI_VERSION, device=storage, port=3, gid_index=5),
    )
    handle = ffi.new("r101_engine **")
    assert lib.r101_create_engine(config, handle) == R101.OK
    try:
        if storage != ffi.NULL:
            ffi.buffer(storage)[:] = b"x" * (len(storage) - 1) + b"\0"
        peer, metadata, size = (
            ffi.new("r101_peer_handle *"),
            ffi.new("const void **"),
            ffi.new("uint64_t *"),
        )
        assert lib.r101_create_peer(handle[0], peer, metadata, size) == R101.OK
        expected = (device if device is not None else b"<auto>") + b"|3|5"
        assert peer[0] == 41
        assert bytes(ffi.buffer(metadata[0], size[0])) == expected
        assert lib.r101_connect_peer(handle[0], peer[0], metadata[0], size[0]) == R101.OK
    finally:
        assert lib.r101_destroy_engine(handle[0]) == R101.OK


@pytest.fixture
def native_engine(adapter_library):
    handle = ffi.new("r101_engine **")
    config = ffi.new(
        "r101_config *", dict(abi_version=R101.ABI_VERSION, device=ffi.NULL, port=1, gid_index=0)
    )
    assert adapter_library.r101_create_engine(config, handle) == R101.OK
    memory = ffi.new("uint64_t[]", 8)
    try:
        region, metadata, size = memory_outputs()
        assert (
            adapter_library.r101_register_local_memory(
                handle[0], memory, 16, 15, region, metadata, size
            )
            == R101.OK
        )
        yield adapter_library, handle[0], memory
    finally:
        assert adapter_library.r101_destroy_engine(handle[0]) == R101.OK


def descriptors(memory):
    opcodes = [
        R101.WRITE,
        R101.READ,
        R101.SEND,
        R101.RECV,
        R101.WRITE_IMM,
        R101.COMPARE_SWAP,
        R101.FETCH_ADD,
        R101.SEND,
    ]
    entries = ffi.new("r101_request[]", len(opcodes))
    for index, (entry, opcode) in enumerate(zip(entries, opcodes)):
        entry.opcode = opcode
        entry.flags = R101.INLINE if index in (0, 2, 4) else 0
        entry.peer, entry.local_address, entry.length = 41, memory, 8
        if opcode in (R101.WRITE, R101.READ):
            entry.args.rma.remote_address = 0xFEDCBA9876543210
        elif opcode == R101.WRITE_IMM:
            entry.args.write_imm.remote_address = 0xFEDCBA9876543210
            entry.args.write_imm.immediate = 0x89ABCDEF
        elif opcode in (R101.COMPARE_SWAP, R101.FETCH_ADD):
            entry.args.atomic.remote_address = 0xFEDCBA9876543210
            entry.args.atomic.compare = 0x1122334455667788
            entry.args.atomic.value = 0xFFEEDDCCBBAA0099
    return entries


def test_request_ownership_and_status_conversion(native_engine):
    lib, handle, memory = native_engine
    entries, batch = descriptors(memory), ffi.new("r101_batch_handle *")
    assert lib.r101_submit_transfer(handle, entries, len(entries), batch) == R101.OK
    assert batch[0] == 1
    ffi.buffer(entries)[:] = b"\0" * ffi.sizeof(entries)
    statuses = ffi.new("r101_transfer_status[]", 8)
    ffi.buffer(statuses)[:] = b"\xa5" * ffi.sizeof(statuses)
    assert lib.r101_get_transfer_statuses(handle, batch[0], statuses, 8) == R101.OK
    assert [
        (s.state, s.error, s.receive.kind, s.receive.immediate, s.receive.length) for s in statuses
    ] == [
        (R101.PENDING, R101.OK, 0, 0, 0),
        (R101.FAILED, R101.IO_ERROR, 0, 0, 0),
        (R101.COMPLETED, R101.OK, R101.RECV_MESSAGE, 0, 8),
        (R101.COMPLETED, R101.OK, R101.RECV_WRITE_IMM, 0x89ABCDEF, 0),
        (R101.FAILED, R101.CANCELED, 0, 0, 0),
        (R101.FAILED, R101.UNSUPPORTED, 0, 0, 0),
        (R101.FAILED, R101.NO_MEMORY, 0, 0, 0),
        (R101.COMPLETED, R101.OK, 0, 0, 0),
    ]


@pytest.mark.parametrize(
    "index,field,value",
    [
        (7, "opcode", 0),
        (7, "opcode", 255),
        (7, "opcode", 0xFFFFFFFF),
        (1, "flags", R101.INLINE),
        (3, "flags", R101.INLINE),
        (5, "flags", R101.INLINE),
        (6, "flags", R101.INLINE),
        (2, "flags", 0x80000000),
    ],
)
def test_invalid_tag_or_flags_reject_entire_batch(native_engine, index, field, value):
    lib, handle, memory = native_engine
    entries, batch = descriptors(memory), ffi.new("r101_batch_handle *", 123)
    setattr(entries[index], field, value)
    assert lib.r101_submit_transfer(handle, entries, len(entries), batch) == R101.INVALID
    assert batch[0] == 0
    entries = descriptors(memory)
    assert lib.r101_submit_transfer(handle, entries, len(entries), batch) == R101.OK
    assert batch[0] == 1  # The rejected array never reached Engine::submit_transfer.


def test_failed_status_query_keeps_c_output(native_engine):
    lib, handle, memory = native_engine
    entries, batch = descriptors(memory), ffi.new("r101_batch_handle *")
    assert lib.r101_submit_transfer(handle, entries, len(entries), batch) == R101.OK
    statuses = ffi.new("r101_transfer_status[]", 8)
    before = b"\xa5" * ffi.sizeof(statuses)
    ffi.buffer(statuses)[:] = before
    for batch_id, count in [(999, 8), (batch[0], 7), (batch[0], 0)]:
        assert lib.r101_get_transfer_statuses(handle, batch_id, statuses, count) == R101.INVALID
        assert bytes(ffi.buffer(statuses)) == before


def memory_outputs():
    return (
        ffi.new("r101_local_memory_handle *", 123),
        ffi.new("const void **", ffi.cast("void *", 123)),
        ffi.new("uint64_t *", 123),
    )


def test_memory_access_and_metadata_conversion(native_engine):
    lib, handle, memory = native_engine
    for mask in range(16):
        region, metadata, size = memory_outputs()
        assert (
            lib.r101_register_local_memory(handle, memory, mask + 1, mask, region, metadata, size)
            == R101.OK
        )
        assert region[0] == 77
        assert bytes(ffi.buffer(metadata[0], size[0])) == b"\x00\xffMR\x80"
        remote = ffi.new("r101_remote_memory_handle *")
        assert lib.r101_import_remote_memory(handle, 41, metadata[0], size[0], remote) == R101.OK
        assert remote[0] == 91
        assert lib.r101_remove_remote_memory(handle, remote[0]) == R101.OK
        assert lib.r101_deregister_local_memory(handle, region[0]) == R101.OK


@pytest.mark.parametrize("length,access", [(16, 0x8000000F), (32, 15)])
def test_failed_registration_clears_outputs(native_engine, length, access):
    lib, handle, memory = native_engine
    region, metadata, size = memory_outputs()
    assert (
        lib.r101_register_local_memory(handle, memory, length, access, region, metadata, size)
        == R101.INVALID
    )
    assert region[0] == size[0] == 0 and metadata[0] == ffi.NULL


def test_memory_lifecycle_errors_are_forwarded(native_engine):
    lib, handle, _ = native_engine
    remote = ffi.new("r101_remote_memory_handle *", 123)
    metadata = ffi.new("char[]", b"wrong memory descriptor")
    assert (
        lib.r101_import_remote_memory(handle, 41, metadata, len(metadata), remote) == R101.INVALID
    )
    assert remote[0] == 0
    assert lib.r101_deregister_local_memory(handle, 78) == R101.BUSY
    assert lib.r101_deregister_local_memory(handle, 999) == R101.INVALID
    assert lib.r101_remove_remote_memory(handle, 92) == R101.BUSY
    assert lib.r101_remove_remote_memory(handle, 999) == R101.INVALID


def test_result_codes(native_engine):
    lib, handle, _ = native_engine
    errors = [
        R101.UNIMPLEMENTED,
        R101.INVALID,
        R101.BUSY,
        R101.IO_ERROR,
        R101.TIMEOUT,
        R101.UNSUPPORTED,
        R101.NO_MEMORY,
        R101.INVALID_STATE,
        R101.CANCELED,
    ]
    for batch, code in enumerate(errors, 1):
        assert lib.r101_free_batch(handle, batch) == code
    assert lib.r101_free_batch(handle, len(errors) + 1) == R101.OK


@pytest.mark.parametrize(
    "peer,result", [(0, R101.INVALID), (41, R101.OK), (42, R101.IO_ERROR), (999, R101.INVALID)]
)
def test_peer_destruction_result_conversion(native_engine, peer, result):
    lib, handle, _ = native_engine
    assert lib.r101_destroy_peer(handle, peer) == result
