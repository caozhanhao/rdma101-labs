"""Generate Rust declarations from transfer-engine/transfer_engine.h."""

import argparse
import difflib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "transfer-engine" / "transfer_engine.h"
BINDGEN_VERSION = "0.72.1"


def run(command):
    return subprocess.check_output(command, text=True)


def declarations():
    """Let Clang parse C; keep only declarations belonging to this API."""
    clang = shlex.split(os.environ.get("CLANG", "clang"))
    tree = json.loads(
        run(
            clang
            + ["-x", "c", "-std=c11", "-Xclang", "-ast-dump=json", "-fsyntax-only", str(HEADER)]
        )
    )
    return {
        node["name"]: node
        for node in tree["inner"]
        if node["kind"] == "FunctionDecl" and node.get("name", "").startswith("r101_")
    }


def rust_bindings(functions):
    bindgen = shlex.split(os.environ.get("BINDGEN", "bindgen"))
    version = run(bindgen + ["--version"]).strip()
    if version != f"bindgen {BINDGEN_VERSION}":
        raise RuntimeError(f"expected bindgen {BINDGEN_VERSION}, got {version}")
    with tempfile.TemporaryDirectory(prefix="r101-bindgen-") as directory:
        wrapper = Path(directory) / "wrapper.h"
        # Function pointer types come from the header too. They let Rust check
        # the implementation's exports without calling or linking another engine.
        # Preserve aliases such as uint64_t from Clang's declaration spelling;
        # typeof would erase them to platform-specific long / long long types.
        signatures = [
            function["type"]["qualType"].replace("(", f"(*{name}_signature)(", 1)
            for name, function in functions.items()
        ]
        wrapper.write_text(
            f'#include "{HEADER.as_posix()}"\n'
            + "".join(f"typedef {signature};\n" for signature in signatures)
        )
        source = run(
            bindgen
            + [
                str(wrapper),
                "--allowlist-type",
                "r101_.*",
                "--allowlist-var",
                "R101_.*",
                "--generate",
                "types,vars",
                "--rust-target",
                "1.77",
                "--no-layout-tests",
                "--no-doc-comments",
                "--no-derive-debug",
                "--no-prepend-enum-name",
                "--disable-header-comment",
                "--",
                "-std=c11",
            ]
        )
    source = (
        "// Generated from transfer-engine/transfer_engine.h by scripts/generate_rust_bindings.py.\n"
        "// Do not edit; regenerate with bindgen " + BINDGEN_VERSION + ".\n"
        "#![allow(non_camel_case_types, dead_code)]\n" + source
    )
    source += "\n// Check every exported function against its C signature.\n"
    source += "".join(
        f"const _: {name}_signature = Some(crate::ffi::{name});\n" for name in functions
    )
    return subprocess.run(
        ["rustfmt", "--edition", "2021"],
        input=source,
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, epilog="Without --check, regenerate files in place."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="check without writing; exit 1 if generated files are missing or stale",
    )
    args = parser.parse_args(argv)
    api = declarations()
    generated = {"transfer-engine/rust/src/bindings.rs": rust_bindings(api)}
    stale = False
    for name, source in generated.items():
        destination = ROOT / name
        if args.check:
            if not destination.exists():
                stale = True
                print(f"Missing {name}")
                continue
            existing = destination.read_text()
            if existing != source:
                stale = True
                print(
                    "".join(
                        difflib.unified_diff(
                            existing.splitlines(True),
                            source.splitlines(True),
                            fromfile=name,
                            tofile="generated " + name,
                        )
                    ),
                    end="",
                )
        else:
            destination.write_text(source)
            print(f"Generated {name}")
    if stale:
        print(
            "Generated files need updating; run: "
            "uv run --locked python scripts/generate_rust_bindings.py",
            file=sys.stderr,
        )
        return 1
    if args.check:
        print("Generated files are up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
