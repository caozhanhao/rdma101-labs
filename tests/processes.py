"""Local test processes, application coordination and per-process diagnostics."""

import math
import multiprocessing
import os
import shutil
import signal
import sys
import tempfile
import time
import traceback
from multiprocessing.connection import wait
from pathlib import Path


class Communicator:
    """Test-only queues and barriers; independent of the Engine control plane."""

    def __init__(self, rank, size, messages, barrier):
        self.rank, self.size = rank, size
        self._messages, self._barrier = messages, barrier

    def send(self, value, dest):
        self._messages[self.rank, dest].put(value)

    def recv(self, source):
        return self._messages[source, self.rank].get()

    def sendrecv(self, value, *, dest, source):
        self.send(value, dest)
        return self.recv(source)

    def allgather(self, value):
        for other in range(self.size):
            if other != self.rank:
                self.send(value, other)
        return [value if other == self.rank else self.recv(other) for other in range(self.size)]

    def barrier(self):
        self._barrier.wait()


def _run_process(function, args, comm, directory):
    os.setsid()
    with (directory / f"rank-{comm.rank}.log").open("w") as log:
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
        print(f"rank-{comm.rank} pid={os.getpid()}", flush=True)
        try:
            function(comm, *args)
        except BaseException:
            traceback.print_exc()
            raise


def _stop(process):
    if process.pid is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        if process.is_alive():
            process.terminate()
    process.join(timeout=2)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        if process.is_alive():
            process.kill()
    process.join()


def run_processes(function, *args, ranks=2, timeout=30, log_dir=None):
    """Run function(comm, *args) with isolated processes and a total deadline."""
    if type(ranks) is not int or ranks < 1 or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("positive process count and finite timeout required")
    if sys.platform not in ("linux", "darwin"):
        raise RuntimeError("run tests in Linux; macOS supports Python-only internal tests")
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(ranks)
    messages = {(a, b): context.Queue() for a in range(ranks) for b in range(ranks) if a != b}
    base = (
        Path(log_dir) if log_dir is not None else Path(__file__).resolve().parents[1] / "build/logs"
    )
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="r101-", dir=base)).resolve()
    processes, succeeded = [], False
    deadline = time.monotonic() + timeout
    try:
        for rank in range(ranks):
            comm = Communicator(rank, ranks, messages, barrier)
            process = context.Process(target=_run_process, args=(function, args, comm, directory))
            (directory / f"rank-{rank}.log").touch()
            process.start()
            processes.append(process)
        (directory / "session.txt").write_text(
            f"callback={function.__module__}.{function.__qualname__}\n"
            f"ranks={ranks} timeout={timeout} pids={[p.pid for p in processes]}\n"
        )
        pending = set(processes)
        while pending:
            ready = wait([p.sentinel for p in pending], max(0, deadline - time.monotonic()))
            if not ready:
                raise TimeoutError(f"test processes exceeded the {timeout:g}s time limit")
            for process in list(pending):
                if process.sentinel in ready:
                    process.join()
                    pending.remove(process)
                    if process.exitcode:
                        raise RuntimeError(
                            f"process {process.pid} exited with code {process.exitcode}"
                        )
        succeeded = True
    finally:
        for process in processes:
            _stop(process)
        for queue in messages.values():
            queue.close()
            queue.join_thread()
        for logfile in sorted(directory.glob("*.log")):
            output = logfile.read_text(errors="replace")[-16000:]
            if output:
                print(f"[{logfile.stem}]\n{output}", end="", flush=True)
        if succeeded:
            shutil.rmtree(directory)
        else:
            print(f"[logs] Full process logs retained at {directory}", flush=True)
