from __future__ import annotations

import os
import signal
from types import SimpleNamespace

from applypilot.admin import Job, JobRunner


def test_stop_terminates_entire_posix_process_group(tmp_path, monkeypatch):
    runner = JobRunner(tmp_path)
    process = SimpleNamespace(pid=4242, returncode=None, terminate=lambda: None)
    runner.current = Job(process=process)

    if os.name == "posix":
        called = {}

        def fake_killpg(pid, sig):
            called["pid"] = pid
            called["sig"] = sig

        monkeypatch.setattr("applypilot.admin.os.killpg", fake_killpg)
        assert runner.stop() is True
        assert called == {"pid": 4242, "sig": signal.SIGTERM}
    else:
        called = {"terminate": False}

        def terminate():
            called["terminate"] = True

        process.terminate = terminate
        assert runner.stop() is True
        assert called["terminate"] is True


def test_stop_returns_false_without_running_job(tmp_path):
    assert JobRunner(tmp_path).stop() is False
