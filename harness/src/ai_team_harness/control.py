"""Отмена, общий дедлайн и наследование блокировки дочерними процессами."""

import contextlib
import fcntl
import threading
import time
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


class Cancelled(RuntimeError):
    """Остановка оператором; после неё нельзя публиковать изменения."""


@dataclass
class Control:
    """Общий сигнал остановки и бюджет конкретного запуска."""

    stop: threading.Event
    state: Path
    deadline: float | None = None
    lock_fd: int | None = None
    environment: dict[str, str] | None = None

    def check(self) -> None:
        if self.stop.is_set() or (self.state / "PAUSE").exists():
            raise Cancelled("Выполнение остановлено; рабочая копия сохранена")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise TimeoutError("Исчерпан лимит времени задачи")

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return () if self.lock_fd is None else (self.lock_fd,)


current: ContextVar[Control | None] = ContextVar("harness_control", default=None)


@contextlib.contextmanager
def scope(control: Control) -> Iterator[None]:
    """Контекст локален потоку воркера; очередь SQLite сюда не передаётся."""
    token = current.set(control)
    try:
        yield
    finally:
        current.reset(token)


@contextlib.contextmanager
def exclusive_lock(path: Path) -> Iterator[int]:
    """Один диспетчер на очередь/репозиторий, включая ещё живые процессы после сбоя."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Уже работает harness или его дочерний процесс: {path}") from exc
        # Не вызывать LOCK_UN: унаследовавшие fd процессы должны удерживать lease
        # даже после аварийного завершения диспетчера. Закрываем только свой fd.
        yield handle.fileno()
