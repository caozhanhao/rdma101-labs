#!/usr/bin/env python3
"""Build Engine implementations on Linux with libibverbs."""

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LIBRARIES = {
    "c": ROOT / "build/c/librdma101_c.so",
    "cpp": ROOT / "build/cpp/librdma101_cpp.so",
    "rust": ROOT / "build/rust/debug/librdma101_rust.so",
}


def build(language, jobs=4):
    """Incrementally build one implementation and return its library path."""
    if sys.platform != "linux":
        raise ValueError(
            "build the Engine inside Linux; see docs/00-environment.md for a Linux VM and RXE setup"
        )
    if language not in LIBRARIES or jobs < 1:
        raise ValueError("choose c, cpp or rust, with a positive job count")
    if language in ("c", "cpp"):
        subprocess.run(
            ["cmake", "-S", f"transfer-engine/{language}", "-B", f"build/{language}"],
            cwd=ROOT,
            check=True,
        )
        subprocess.run(
            ["cmake", "--build", f"build/{language}", "--parallel", str(jobs)],
            cwd=ROOT,
            check=True,
        )
    else:
        subprocess.run(
            [
                "cargo",
                "build",
                "--manifest-path",
                "transfer-engine/rust/Cargo.toml",
                "--target-dir",
                "build/rust",
                "--locked",
                "--jobs",
                str(jobs),
            ],
            cwd=ROOT,
            check=True,
        )
    library = LIBRARIES[language]
    if not library.is_file():
        raise FileNotFoundError(f"build succeeded but did not produce {library}")
    return library


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("language", nargs="?", choices=(*LIBRARIES, "all"), default="all")
    parser.add_argument("-j", "--jobs", type=int, default=4)
    args = parser.parse_args()
    try:
        for language in LIBRARIES if args.language == "all" else [args.language]:
            build(language, args.jobs)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    except subprocess.CalledProcessError as exc:
        return exc.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
