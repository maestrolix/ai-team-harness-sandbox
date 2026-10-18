"""Запуск команд и остановка всей группы дочерних процессов."""

import contextlib
import os
import signal
import subprocess
import time

from .control import current


def command(args, cwd=None, timeout=45, data=None, env=None, include_stderr=False, guard=None):
    """Выполнить команду без shell; тайм-аут/отмена останавливают также потомков."""
    control = current.get()
    if control:
        control.check()
        if env is None:
            env = os.environ | (control.environment or {})
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
        pass_fds=control.pass_fds if control else (),
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            if control:
                control.check()
            if guard:
                guard()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            try:
                stdout, stderr = proc.communicate(input=data, timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                data = None  # communicate сохраняет недописанный ввод между вызовами.
        if proc.returncode:
            raise RuntimeError(f"{args[0]} failed ({proc.returncode}): {stderr[-1500:]}")
        return (stdout + (stderr if include_stderr else "")).strip()
    finally:
        stop_process(proc)


def stop_process(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        proc.wait()
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        proc.poll()  # Reap the root even when its descendants outlive it.
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
