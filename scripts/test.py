#!/usr/bin/env python3
"""Build one Engine, then run pytest. Example: test.py cpp -m lab1 -vv."""

import argparse
import os
import subprocess
import sys

from build import LIBRARIES, ROOT, build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("language", choices=LIBRARIES)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER, help="arguments passed to pytest")
    args = parser.parse_args()
    forwarded = args.pytest_args
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    if any(arg == "--library" or arg.startswith("--library=") for arg in forwarded):
        parser.error("the language selects the library; use pytest directly for custom libraries")
    try:
        library = build(args.language)
        os.chdir(ROOT)
        os.execv(
            sys.executable,
            [sys.executable, "-m", "pytest", "--rdma", "--library", str(library), *forwarded],
        )
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    except subprocess.CalledProcessError as exc:
        return exc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
