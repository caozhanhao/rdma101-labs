#!/usr/bin/env python3
"""Format C/C++, Rust, Python and Markdown, or check formatting without writing."""

import argparse
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
C_SUFFIXES = {".c", ".h"}
CPP_SUFFIXES = {".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "language",
        nargs="?",
        choices=("c", "cpp", "rust", "python", "markdown", "all"),
        default="all",
    )
    parser.add_argument(
        "--check", action="store_true", help="report formatting changes without writing"
    )
    args = parser.parse_args()
    commands = []
    if args.language in ("c", "cpp", "all"):
        paths = [ROOT / "transfer-engine/transfer_engine.h"]
        suffixes = {"c": C_SUFFIXES, "cpp": CPP_SUFFIXES, "all": C_SUFFIXES | CPP_SUFFIXES}[
            args.language
        ]
        directories = [ROOT / "tests", ROOT / "scripts"]
        directories.extend(
            ROOT / "transfer-engine" / language
            for language in ("c", "cpp")
            if args.language in (language, "all")
        )
        for directory in directories:
            paths.extend(
                path
                for path in directory.rglob("*")
                if path.is_file()
                and path.suffix in suffixes
                and not {"build", "target"}.intersection(path.relative_to(ROOT).parts)
            )
        options = ["--dry-run", "--Werror"] if args.check else ["-i"]
        commands.append(
            ["clang-format", "--style=file", *options]
            + [str(path.relative_to(ROOT)) for path in sorted(paths)]
        )
    if args.language in ("rust", "all"):
        commands.append(
            [
                "cargo",
                "fmt",
                "--manifest-path",
                "transfer-engine/rust/Cargo.toml",
                *(["--", "--check"] if args.check else []),
            ]
        )
    if args.language in ("python", "all"):
        commands.append(["ruff", "format", *(["--check"] if args.check else []), "."])
    if args.language in ("markdown", "all"):
        commands.append(["mdformat", *(["--check"] if args.check else []), "README.md", "docs"])
    failed = False
    for command in commands:
        try:
            result = subprocess.run(command, cwd=ROOT)
        except FileNotFoundError:
            parser.exit(
                1,
                f"error: {command[0]} not found; "
                "run uv sync --locked and invoke this script with uv run\n",
            )
        failed = result.returncode != 0 or failed
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
