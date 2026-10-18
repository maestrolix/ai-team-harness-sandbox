"""Отмена и блокировки проверяются также настоящими дочерними процессами."""

import os
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import Mock, patch

import pytest

from ai_team_harness import processes
from ai_team_harness.control import Cancelled, Control, current, exclusive_lock, scope


def test_scope_deadline_and_pause(tmp_path):
    control = Control(threading.Event(), tmp_path)
    assert current.get() is None
    assert control.pass_fds == ()
    with scope(control):
        assert current.get() is control
        control.check()
    assert current.get() is None
    control.deadline = time.monotonic() - 1
    with pytest.raises(TimeoutError):
        control.check()
    control.deadline = None
    control.stop.set()
    with pytest.raises(Cancelled):
        control.check()
    control.stop.clear()
    (tmp_path / "PAUSE").touch()
    with pytest.raises(Cancelled):
        control.check()


def test_lease_outlives_parent_handle(tmp_path):
    lock = tmp_path / "runtime.lock"
    with exclusive_lock(lock) as fd:
        control = Control(threading.Event(), tmp_path, lock_fd=fd)
        assert control.pass_fds == (fd,)
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            pass_fds=control.pass_fds,
            start_new_session=True,
        )
    try:
        with pytest.raises(RuntimeError, match="дочерний процесс"), exclusive_lock(lock):
            pass
    finally:
        processes.stop_process(proc)
    with exclusive_lock(lock):
        pass


def test_command_io_and_environment(tmp_path):
    ctl = Control(threading.Event(), tmp_path, environment={"HARNESS_TASK_ID": "issue-1"})
    with scope(ctl):
        output = processes.command(
            [
                sys.executable,
                "-c",
                "import os,sys; print(os.environ['HARNESS_TASK_ID']); print(sys.stdin.read()); print('err',file=sys.stderr)",
            ],
            data="input",
            include_stderr=True,
        )
    assert output == "issue-1\ninput\nerr"
    assert processes.command([sys.executable, "-c", "print('out')"], env=os.environ.copy()) == "out"


def test_command_timeout_and_cancellation_stop_process(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        processes.command([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.3)
    ctl = Control(threading.Event(), tmp_path)
    timer = threading.Timer(0.1, ctl.stop.set)
    timer.start()
    try:
        with scope(ctl), pytest.raises(Cancelled):
            processes.command(
                [sys.executable, "-c", "import time; time.sleep(10)"], guard=lambda: None
            )
    finally:
        timer.join()
    with scope(ctl), pytest.raises(Cancelled):
        processes.command(["must-not-start"])


def test_stop_already_exited_and_stubborn_descendant():
    proc = Mock(pid=123)
    with patch.object(processes.os, "killpg", side_effect=ProcessLookupError):
        processes.stop_process(proc)
    proc.wait.assert_called_once()
    proc.reset_mock()
    with (
        patch.object(processes.os, "killpg") as kill,
        patch.object(processes.time, "monotonic", side_effect=[0, 6]),
    ):
        processes.stop_process(proc)
    assert kill.call_args_list[-1].args == (123, signal.SIGKILL)
    proc.wait.assert_called_once()


def test_stop_reaps_root_while_waiting_for_children():
    proc = Mock(pid=456)
    with (
        patch.object(processes.os, "killpg", side_effect=[None, None, ProcessLookupError]),
        patch.object(processes.time, "monotonic", side_effect=[0, 1, 2]),
        patch.object(processes.time, "sleep") as sleep,
    ):
        processes.stop_process(proc)
    sleep.assert_called_once_with(0.1)
    assert proc.poll.call_count == 2
