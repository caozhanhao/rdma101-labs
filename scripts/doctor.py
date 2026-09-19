#!/usr/bin/env python3
"""Report RDMA devices; --check also runs an independent two-process WRITE check."""

import argparse
from datetime import datetime, timezone
import os
import platform
import resource
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    try:
        return path.read_text().strip()
    except OSError as exc:
        return f"unavailable ({exc})"


def report():
    print(platform.platform())
    if sys.platform != "linux":
        print("Engine development and tests require Linux with an RDMA device (RXE is sufficient).")
        print("See docs/00-environment.md for a Linux VM / RXE setup.")
        return 2
    soft, hard = resource.getrlimit(resource.RLIMIT_MEMLOCK)
    print(f"memlock soft={soft}, hard={hard} bytes (-1 means unlimited)")
    print(
        "uverbs devices:",
        ", ".join(str(p) for p in Path("/dev/infiniband").glob("uverbs*")) or "NONE",
    )
    for command in (["ibv_devices"], ["rdma", "link", "show"]):
        if shutil.which(command[0]):
            print("$ " + " ".join(command), flush=True)
            try:
                result = subprocess.run(command, timeout=5, check=False)
                if result.returncode:
                    print(f"exit status {result.returncode}")
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"could not run {command[0]}: {exc}")
        else:
            print(f"missing tool: {command[0]}")
    devices = sorted(Path("/sys/class/infiniband").glob("*"))
    for device in devices:
        for port in sorted((device / "ports").glob("*")):
            print(
                f"{device.name} port {port.name}: {read(port / 'state')}, {read(port / 'link_layer')}"
            )
            for gid in sorted((port / "gids").glob("*"), key=lambda p: int(p.name)):
                value = read(gid)
                if value == "0000:0000:0000:0000:0000:0000:0000:0000":
                    continue
                attrs = port / "gid_attrs"
                print(
                    f"  GID {gid.name}: {value}; {read(attrs / 'types' / gid.name)}; netdev={read(attrs / 'ndevs' / gid.name)}"
                )
    return 0 if devices else 1


def run_command(command, timeout):
    """Reap the command and stop its process group on failure, timeout or Ctrl-C."""
    process = subprocess.Popen(command, start_new_session=True)
    try:
        result = process.wait(timeout=timeout)
        if result:
            raise subprocess.CalledProcessError(result, command)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def build_probe(directory):
    """Build against the installed libibverbs without loading an Engine implementation."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "rdma_check"
    with tempfile.TemporaryDirectory(prefix=".compile-", dir=directory) as temporary:
        binary = Path(temporary) / "rdma_check"
        command = shlex.split(os.environ.get("CXX", "c++")) + [
            "-std=c++17",
            "-O0",
            "-g",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wpedantic",
            str(ROOT / "scripts/rdma_check.cpp"),
            "-libverbs",
            "-o",
            str(binary),
        ]
        run_command(command, timeout=60)
        binary.replace(output)
    return output


def save_report(path, args):
    """Collect diagnostics even when individual tools or the device are unavailable."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    probe = [sys.executable, str(Path(__file__).resolve())]
    commands = [
        ["uname", "-a"],
        [
            "dpkg-query",
            "-W",
            "rdma-core",
            "libibverbs1",
            "ibverbs-providers",
            "ibverbs-utils",
            "perftest",
        ],
        ["cc", "--version"],
        ["cmake", "--version"],
        ["uv", "--version"],
        ["rustc", "-Vv"],
        ["git", "rev-parse", "HEAD"],
        ["git", "status", "--short"],
        probe,
    ]
    result = 1
    with path.open("w") as output:
        print(f"RDMA environment report: {datetime.now(timezone.utc).isoformat()}", file=output)
        print(read(Path("/etc/os-release")), file=output)
        print(f"Python: {sys.version}\nExecutable: {sys.executable}", file=output)
        print(
            f"Selected device={args.device}, port={args.port}, gid_index={args.gid_index}, "
            f"netdev={os.environ.get('R101_NETDEV')}",
            file=output,
        )
        for command in commands:
            print("\n$ " + shlex.join(command), file=output, flush=True)
            try:
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    timeout=15,
                    check=False,
                )
                if completed.returncode:
                    print(f"exit status {completed.returncode}", file=output)
                if command is probe:
                    result = completed.returncode
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"unavailable: {exc}", file=output)
    print(f"Environment report saved to {path}")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="build and run the WRITE check")
    mode.add_argument("--report", type=Path, metavar="PATH", help="save an environment report")
    parser.add_argument(
        "--device", default=os.environ.get("R101_DEVICE"), help="RDMA device (R101_DEVICE)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=os.environ.get("R101_PORT", "1"),
        help="RDMA port (R101_PORT, otherwise 1)",
    )
    parser.add_argument(
        "--gid-index",
        type=int,
        default=os.environ.get("R101_GID_INDEX"),
        help="GID index (R101_GID_INDEX)",
    )
    parser.add_argument("--timeout", type=int, default=30, help="communication timeout in seconds")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 255:
        parser.error("--port must be between 1 and 255")
    if args.gid_index is not None and not 0 <= args.gid_index <= 255:
        parser.error("--gid-index must be between 0 and 255")
    if not 1 <= args.timeout <= 3600:
        parser.error("--timeout must be between 1 and 3600 seconds")
    if args.check and (not args.device or args.gid_index is None):
        parser.error("--check requires --device and --gid-index, or R101_DEVICE and R101_GID_INDEX")
    stage = "device report"
    try:
        if args.report:
            stage = "save report"
            return save_report(args.report, args)
        result = report()
        if result:
            print("[FAIL] Linux with a visible RDMA device is required; transfer NOT CHECKED.")
            return result
        if not args.check:
            print("Report only; resource access and transfer NOT CHECKED. Use --check next.")
            return 0
        stage = "build"
        print("[build] Compiling scripts/rdma_check.cpp with the installed libibverbs", flush=True)
        binary = build_probe(ROOT / "build/doctor")
        stage = "communication"
        print(
            f"[check] device={args.device} port={args.port} gid_index={args.gid_index} "
            f"timeout={args.timeout}s",
            flush=True,
        )
        run_command(
            [str(binary), args.device, str(args.port), str(args.gid_index), str(args.timeout)],
            timeout=args.timeout,
        )
        print("[PASS] Resource access, RC connection and 4096-byte WRITE with data/guard checks.")
        print("Other RDMA operations, cross-host networking and performance: NOT CHECKED.")
        return 0
    except subprocess.TimeoutExpired:
        print(f"[FAIL] {stage} timed out; see the last stage printed by each process.")
    except subprocess.CalledProcessError as exc:
        print(f"[FAIL] {stage} exited with status {exc.returncode}; see diagnostics above.")
    except OSError as exc:
        print(f"[FAIL] {stage}: {exc}")
    except KeyboardInterrupt:
        print(f"\n[STOP] Interrupted during {stage}; child processes stopped.")
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())
