"""Observe standard verbs in test processes without changing the Engine API."""

import ctypes
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path


def build_verbs_probe():
    source = Path(__file__).with_suffix(".c")
    output = source.parents[1] / "build/tests/libverbs_probe.so"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".verbs-probe-{os.getpid()}.so")
    try:
        subprocess.run(
            [
                "cc",
                "-std=gnu11",
                "-O1",
                "-g",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-fPIC",
                "-shared",
                "-pthread",
                str(source),
                "-ldl",
                "-Wl,--no-as-needed",
                "-libverbs",
                "-o",
                str(temporary),
            ],
            check=True,
        )
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


class _CallStats(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "application_tid contexts live_contexts "
            "post_calls posted_wrs application_posts last_post_tid "
            "poll_calls held_polls application_polls "
            "completions application_completions last_completion_tid shutdown_releases"
        ).split()
    ]

    def __repr__(self):
        return repr({name: getattr(self, name) for name, _ in self._fields_})


class _WriteStats(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "posted completed signaled cqes in_flight peak_in_flight post_batch poll_entries "
            "post_errors completion_errors capacity_errors untracked_writes"
        ).split()
    ]

    def __repr__(self):
        return repr({name: getattr(self, name) for name, _ in self._fields_})


class VerbsProbe:
    def __init__(self, timeout):
        self.timeout = timeout
        self.lib = ctypes.CDLL(None)
        try:
            self.lib.r101_probe_hold_completions.argtypes = [ctypes.c_int]
            self.lib.r101_probe_hold_completions.restype = None
            self.lib.r101_probe_get_call_stats.argtypes = [ctypes.POINTER(_CallStats)]
            self.lib.r101_probe_get_call_stats.restype = None
            self.lib.r101_probe_observe_writes.argtypes = [ctypes.c_int]
            self.lib.r101_probe_observe_writes.restype = None
            self.lib.r101_probe_get_write_stats.argtypes = [ctypes.POINTER(_WriteStats)]
            self.lib.r101_probe_get_write_stats.restype = None
        except AttributeError as error:
            raise RuntimeError("run this case with run_engines(..., observe_verbs=True)") from error
        self.lib.r101_probe_hold_completions(0)
        assert self.call_stats().live_contexts > 0, "verbs probe did not observe device creation"

    def call_stats(self):
        stats = _CallStats()
        self.lib.r101_probe_get_call_stats(ctypes.byref(stats))
        return stats

    def write_stats(self):
        stats = _WriteStats()
        self.lib.r101_probe_get_write_stats(ctypes.byref(stats))
        return stats

    @contextmanager
    def observe_writes(self):
        """Measure one WRITE workload, starting with no outstanding requests."""
        self.lib.r101_probe_observe_writes(1)
        try:
            yield
        finally:
            self.lib.r101_probe_observe_writes(0)

    def assert_writes_completed(self, minimum_wrs, *, concurrent=False, batching=False):
        stats = self.write_stats()
        assert stats.untracked_writes == 0, f"WRITE QP creation was not observed: {stats}"
        assert stats.post_errors == 0, f"post_send failed: {stats}"
        assert stats.completion_errors == 0, f"failed or unaccounted WRITE completion: {stats}"
        assert stats.capacity_errors == 0, f"unretired WRs exceeded QP capacity: {stats}"
        assert stats.posted >= minimum_wrs, f"missing WRITE posts: {stats}"
        assert stats.completed == stats.posted and stats.in_flight == 0, (
            f"tasks completed without completion evidence for every WR: {stats}"
        )
        if concurrent:
            assert stats.peak_in_flight >= 2, f"no concurrent WRs: {stats}"
        if batching:
            assert stats.post_batch >= 2, f"no chained WR submission: {stats}"
            assert stats.poll_entries >= 2, f"CQ polling accepts only one WC: {stats}"
            assert 0 < stats.cqes < stats.posted, f"no selective signaling: {stats}"
        return stats

    @contextmanager
    def hold_completions(self):
        """Keep real CQEs in the CQ until release or QP shutdown; polling returns 0."""
        self.lib.r101_probe_hold_completions(1)
        before = self.call_stats()
        print("verbs probe: CQE delivery paused", flush=True)
        try:
            yield before
        finally:
            self.lib.r101_probe_hold_completions(0)
            print("verbs probe: CQE delivery resumed", flush=True)

    def assert_background_only(self, before):
        """Check that submit/query/free did not post or poll on the calling thread."""
        current = self.call_stats()
        assert current.application_posts == before.application_posts, (
            f"application thread posted WRs: {current}"
        )
        assert current.application_polls == before.application_polls, (
            f"application thread polled CQ: {current}"
        )
        return current

    def _wait_for_counter(self, field, before, minimum=1):
        deadline = time.monotonic() + self.timeout
        while True:
            current = self.assert_background_only(before)
            if getattr(current, field) - getattr(before, field) >= minimum:
                print(f"verbs probe: observed {field}: {current}", flush=True)
                return current
            assert time.monotonic() < deadline, (
                f"no background {field} without Engine API calls: {current}"
            )
            time.sleep(0.001)

    def wait_for_post(self, before, minimum=1):
        """Wait for successful background WR posts, without calling the Engine."""
        return self._wait_for_counter("posted_wrs", before, minimum)

    def wait_for_completion(self, before):
        """Wait for real CQE delivery on a background thread, without calling the Engine."""
        return self._wait_for_counter("completions", before)
