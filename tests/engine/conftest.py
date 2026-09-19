"""Device configuration and process fixtures for native Engine acceptance tests."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from rdma101 import Config, Engine
from scripts.build import LIBRARIES
from tests.processes import run_processes
from tests.verbs_probe import build_verbs_probe

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def atomic_device(request):
    command = ["ibv_devinfo", "-v"]
    if device := request.config.getoption("device"):
        command += ["-d", device]
    result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True)
    capability = re.search(r"atomic_cap:\s+(\w+)", result.stdout)
    assert capability, f"ibv_devinfo did not report atomic_cap:\n{result.stdout}"
    if capability[1] == "ATOMIC_NONE":
        pytest.skip("selected RDMA device reports ATOMIC_NONE")


def pytest_collection_modifyitems(config, items):
    if config.getoption("collectonly"):
        return
    engine_tests = any(ROOT / "tests/engine" in item.path.parents for item in items)
    if engine_tests and sys.platform != "linux":
        raise pytest.UsageError(
            "Engine tests require Linux with an RDMA device (RXE is sufficient); "
            "see docs/00-environment.md"
        )
    if config.getoption("rdma"):
        return
    skip = pytest.mark.skip(reason="enable native RDMA tests with --rdma on Linux")
    for item in items:
        if "rdma" in item.keywords:
            item.add_marker(skip)


def pytest_generate_tests(metafunc):
    if "library_path" in metafunc.fixturenames:
        paths = metafunc.config.getoption("library") or list(LIBRARIES.values())
        metafunc.parametrize(
            "library_path",
            paths,
            indirect=True,
            ids=[Path(path).stem for path in paths],
            scope="session",
        )


@pytest.fixture(scope="session")
def library_path(request):
    devices = Path("/sys/class/infiniband")
    selected = request.config.getoption("device")
    available = (devices / selected).is_dir() if selected else any(devices.glob("*"))
    if not available:
        pytest.fail(
            "no RDMA device found; configure RXE or select an existing device with --device. "
            "See docs/00-environment.md"
        )
    if not any(Path("/dev/infiniband").glob("uverbs*")):
        pytest.fail("no uverbs device found; run scripts/doctor.py and check the Linux RDMA setup")
    path = Path(request.param).resolve()
    if not path.is_file():
        pytest.fail(f"missing {path}; build first with uv run python scripts/build.py")
    return path


def _run_case(comm, function, library, config, timeout, *args):
    """Run function(engine, comm, *args), then synchronize before Engine cleanup."""
    with Engine(library, config, timeout=timeout) as engine:
        function(engine, comm, *args)
        comm.barrier()


@pytest.fixture
def engine_config(request):
    options = request.config
    return Config(
        device=options.getoption("device"),
        port=options.getoption("port"),
        gid_index=options.getoption("gid_index"),
    )


@pytest.fixture(scope="session")
def verbs_probe_library():
    return build_verbs_probe()


@pytest.fixture
def run_engines(library_path, engine_config, request):
    timeout = request.config.getoption("rank_timeout")

    def run(function, *args, ranks=2, observe_verbs=False):
        # spawn must inherit LD_PRELOAD before starting the Python interpreter.
        with pytest.MonkeyPatch.context() as environment:
            if observe_verbs:
                probe = request.getfixturevalue("verbs_probe_library")
                preload = f"{probe} {os.environ.get('LD_PRELOAD', '')}".strip()
                environment.setenv("LD_PRELOAD", preload)
            run_processes(
                _run_case,
                function,
                str(library_path),
                engine_config,
                timeout,
                *args,
                ranks=ranks,
                timeout=timeout,
            )

    return run
