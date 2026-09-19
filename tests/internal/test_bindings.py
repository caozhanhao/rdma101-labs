"""Check the Python CFFI declarations against the C compiler."""

import os
import shlex
import subprocess

from rdma101.bindings import HEADER, constants as R101, ffi


def test_cffi_layout_matches_header(tmp_path):
    expected = {}
    for name in ffi.list_types()[0]:
        if not name.startswith("r101_"):
            continue
        kind = ffi.typeof(name)
        if kind.kind == "struct" and kind.fields is None:
            expected[f"sizeof({name} *)"] = ffi.sizeof(name + " *")
            expected[f"_Alignof({name} *)"] = ffi.alignof(name + " *")
            continue
        expected[f"sizeof({name})"] = ffi.sizeof(name)
        expected[f"_Alignof({name})"] = ffi.alignof(name)
        if kind.kind in ("struct", "union"):
            for field, _ in kind.fields:
                expected[f"offsetof({name}, {field})"] = ffi.offsetof(name, field)
        else:
            expected[f"(({name})-1 < ({name})0)"] = int(ffi.cast(name, -1)) < 0
    expected.update({"R101_" + name: value for name, value in vars(R101).items()})
    source = (
        "#include <stddef.h>\n#include <stdio.h>\n" + f'#include "{HEADER}"\nint main(void) {{\n'
    )
    source += "".join(f'printf("%lld\\n", (long long)({expr}));\n' for expr in expected)
    probe, binary = tmp_path / "layout.c", tmp_path / "layout"
    probe.write_text(source + "return 0;\n}\n")
    subprocess.run(
        shlex.split(os.environ.get("CC", "cc")) + ["-std=c11", str(probe), "-o", str(binary)],
        check=True,
    )
    actual = list(map(int, subprocess.check_output([str(binary)], text=True).splitlines()))
    assert dict(zip(expected, actual)) == expected
