"""Register Engine test options before pytest chooses its test directory."""

import os


def pytest_addoption(parser):
    group = parser.getgroup("rdma101")
    group.addoption(
        "--library", action="append", help="engine library; repeat to test multiple implementations"
    )
    group.addoption("--rdma", action="store_true", help="also run native RDMA behavior tests")
    group.addoption("--device", default=os.environ.get("R101_DEVICE"))
    group.addoption("--port", type=int, default=os.environ.get("R101_PORT", "1"))
    group.addoption("--gid-index", type=int, default=os.environ.get("R101_GID_INDEX", "0"))
    group.addoption("--rank-timeout", type=float, default=30)
