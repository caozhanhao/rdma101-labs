"""Local process coordination, failure cleanup and diagnostic capture."""

import os
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from rdma101 import Config
from tests.engine import conftest as engine_fixtures
from tests.processes import run_processes


def _exchange(comm, directory):
    Path(directory, f"{comm.rank}.pid").write_text(str(os.getpid()))
    for _ in range(2):
        assert comm.allgather(comm.rank) == list(range(comm.size))
        comm.barrier()
    if comm.rank in (0, 1) and comm.size > 1:
        other = 1 - comm.rank
        payload = bytes([comm.rank]) * 65536
        assert comm.sendrecv(payload, dest=other, source=other) == bytes([other]) * 65536
        if comm.rank == 0:
            comm.send(None, dest=1)
            comm.send(b"second", dest=1)
        else:
            assert comm.recv(source=0) is None
            assert comm.recv(source=0) == b"second"
    os.write(2, f"native diagnostic from rank {comm.rank}\n".encode())


def _failure(comm, directory):
    Path(directory, f"{comm.rank}.pid").write_text(str(os.getpid()))
    if comm.rank == 0:
        comm.recv(source=1)
        os.write(2, b"full-log-start\n" + b"x" * 18000 + b"\n")
        raise RuntimeError("deliberate rank failure")
    comm.send(None, dest=0)
    time.sleep(60)


def _hang(comm, directory):
    Path(directory, f"{comm.rank}.pid").write_text(str(os.getpid()))
    time.sleep(60)


def _missing_message(comm, directory):
    Path(directory, f"{comm.rank}.pid").write_text(str(os.getpid()))
    print(f"waiting for rank-{1 - comm.rank}", flush=True)
    comm.recv(source=1 - comm.rank)


@pytest.mark.parametrize("ranks", [1, 3])
def test_processes_exchange_and_capture_native_logs(tmp_path, capsys, ranks):
    run_processes(_exchange, str(tmp_path), ranks=ranks, timeout=10, log_dir=tmp_path / "logs")
    pids = {int(path.read_text()) for path in tmp_path.glob("*.pid")}
    assert len(pids) == ranks and os.getpid() not in pids
    output = capsys.readouterr().out
    for rank in range(ranks):
        assert f"native diagnostic from rank {rank}" in output
    assert not list((tmp_path / "logs").iterdir())


def test_engine_case_synchronizes_before_cleanup(monkeypatch):
    events = []
    comm = SimpleNamespace(rank=2, barrier=lambda: events.append("barrier"))
    config = Config(gid_index=3)

    @contextmanager
    def engine(library, options, **kwargs):
        assert library == "test-library" and options is config
        assert kwargs == dict(timeout=7)
        try:
            yield SimpleNamespace(endpoint="127.0.0.1:1234", **kwargs)
        finally:
            events.append("closed")

    def application(engine, communicator, argument):
        assert communicator is comm
        events.append((comm.rank, argument))

    monkeypatch.setattr(engine_fixtures, "Engine", engine)
    engine_fixtures._run_case(comm, application, "test-library", config, 7, "value")
    assert events == [(2, "value"), "barrier", "closed"]


@pytest.mark.parametrize(
    "function,error,timeout",
    [(_failure, RuntimeError, 10), (_hang, TimeoutError, 2), (_missing_message, TimeoutError, 2)],
)
def test_failure_and_timeout_reap_every_rank(function, error, timeout, tmp_path, capsys):
    with pytest.raises(error):
        run_processes(function, str(tmp_path), timeout=timeout, log_dir=tmp_path / "logs")
    for pid_file in tmp_path.glob("*.pid"):
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
    output = capsys.readouterr().out
    (directory,) = (tmp_path / "logs").iterdir()
    assert str(directory) in output
    assert (directory / "session.txt").is_file()
    assert {path.name for path in directory.glob("rank-*.log")} == {"rank-0.log", "rank-1.log"}
    if function is _failure:
        assert "deliberate rank failure" in output
        assert "full-log-start" not in output
        log = (directory / "rank-0.log").read_text()
        assert "full-log-start" in log and "deliberate rank failure" in log
    if function is _missing_message:
        assert "waiting for rank-" in output
