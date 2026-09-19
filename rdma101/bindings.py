"""CFFI declarations read from the shared C header."""

from pathlib import Path
from types import SimpleNamespace

from cffi import FFI

HEADER = Path(__file__).resolve().parents[1] / "transfer-engine/transfer_engine.h"
source = HEADER.read_text()
# The public declarations lie between the C/C++ linkage guards. CFFI supplies
# stdint types itself; it cannot parse includes or conditional preprocessing.
declarations = source[
    source.index("#define R101_ABI_VERSION") : source.rindex("#ifdef __cplusplus")
]
ffi = FFI()
ffi.cdef(declarations)
_symbols = ffi.dlopen(None)
constants = SimpleNamespace(
    **{
        name.removeprefix("R101_"): getattr(_symbols, name)
        for name in dir(_symbols)
        if name.startswith("R101_")
    }
)
FUNCTIONS = tuple(name for name in dir(_symbols) if name.startswith("r101_"))


def library(path):
    lib = ffi.dlopen(str(Path(path).resolve()))
    for name in FUNCTIONS:
        getattr(lib, name)
    return lib
