"""Generate Python RPC modules from rdma101/control.proto."""

import argparse
import difflib
import sys
import tempfile
from pathlib import Path

from grpc_tools import protoc

ROOT = Path(__file__).resolve().parents[1]


def generate(directory):
    (directory / "rdma101/generated").mkdir(parents=True, exist_ok=True)
    return protoc.main(
        [
            "grpc_tools.protoc",
            f"-Irdma101/generated={ROOT / 'rdma101'}",
            f"--python_out={directory}",
            f"--grpc_python_out={directory}",
            str(ROOT / "rdma101/control.proto"),
        ]
    )


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
    if not args.check:
        result = generate(ROOT)
        if result == 0:
            print("Generated rdma101/generated")
        return result

    stale = False
    with tempfile.TemporaryDirectory(prefix="r101-rpc-") as directory:
        directory = Path(directory)
        result = generate(directory)
        if result:
            return result
        for generated in sorted(directory.rglob("*.py")):
            name = generated.relative_to(directory)
            destination = ROOT / name
            if not destination.exists():
                stale = True
                print(f"Missing {name}")
                continue
            existing, source = destination.read_text(), generated.read_text()
            if existing != source:
                stale = True
                print(
                    "".join(
                        difflib.unified_diff(
                            existing.splitlines(True),
                            source.splitlines(True),
                            fromfile=str(name),
                            tofile="generated " + str(name),
                        )
                    ),
                    end="",
                )
    if stale:
        print(
            "Generated files need updating; run: uv run --locked python scripts/generate_rpc.py",
            file=sys.stderr,
        )
        return 1
    print("Generated files are up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
