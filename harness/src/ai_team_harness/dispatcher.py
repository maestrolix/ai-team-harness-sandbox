"""Один владелец очереди и ограниченный пул независимых задач.

Потоки заняты ожиданием Codex/Git/GitHub, а не вычислениями Python. Каждый
воркер запускает собственную группу процессов; SQLite остаётся в диспетчере.
"""

import contextlib
import copy
import hashlib
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path

from .control import Control, scope
from .events import observe, refresh
from .github import GitHub
from .publication import reconcile
from .store import Queue
from .workflow import execute
from .workspaces import Workspaces, resource_keys, task_name

log = logging.getLogger(__name__)


def run_job(
    event: dict,
    cfg: dict,
    snapshot: dict,
    workspaces: Workspaces,
    stop: threading.Event,
    slot: int,
    lock_fd: int,
) -> None:
    """Воркер не получает SQLite и не может самостоятельно взять следующую задачу."""
    state = Path(cfg["state_dir"])
    name = task_name(event)
    project = hashlib.sha256(f"{cfg['repository']}:{name}".encode()).hexdigest()[:12]
    environment = {
        "HARNESS_TASK_ID": name,
        "HARNESS_WORKER_ID": str(slot),
        "HARNESS_STATE_DIR": str(state),
        "COMPOSE_PROJECT_NAME": f"harness-{project}",
    }
    control = Control(
        stop, state, time.monotonic() + cfg["job_timeout_seconds"], lock_fd, environment
    )
    with scope(control):
        control.check()
        gh = GitHub(cfg)
        path, branch, base = workspaces.prepare(event, gh)
        environment["HARNESS_WORKSPACE"] = str(path)
        local_cfg = cfg | {"workspace": str(path)}
        execute(event, local_cfg, gh, snapshot, state, prepared=(branch, base))


class Dispatcher:
    """Резервирует Issue/PR, пополняет свободные слоты и собирает результаты."""

    def __init__(
        self,
        cfg: dict,
        queue: Queue,
        gh: GitHub,
        workspaces: Workspaces,
        stop: threading.Event,
        lock_fd: int,
        *,
        max_jobs: int | None = None,
        max_seconds: int | None = None,
        once: bool = False,
    ):
        self.cfg, self.queue, self.gh, self.workspaces = cfg, queue, gh, workspaces
        self.stop, self.lock_fd = stop, lock_fd
        self.max_jobs, self.once = max_jobs, once
        self.deadline = time.monotonic() + max_seconds if max_seconds is not None else float("inf")
        self.running: dict[Future, tuple[dict, int]] = {}
        self.started = 0
        self.failed = False
        self.snapshot: dict = {}
        self.state = Path(cfg["state_dir"])

    def failure(self, event: dict, exc: Exception) -> None:
        """Не повторять запись с неизвестным исходом; сохранить задачу для разбора."""
        self.failed = True
        self.queue.save(event)
        self.queue.finish(event["key"], "blocked", str(exc))
        log.error("Blocked %s: %s", event["key"], exc)
        if not self.stop.is_set() and not (self.state / "PAUSE").exists():
            with contextlib.suppress(Exception):
                self.gh.guard(event)
                self.gh.status(self.snapshot, event.get("item"), "Blocked")
                self.gh.reply(
                    dict(event, key=event["key"] + ":blocked"),
                    f"Blocked: {exc}\nРабочая копия сохранена. После разбора используйте --retry.",
                )

    def collect(self) -> bool:
        """Только диспетчер переводит завершённые задания в done/blocked."""
        finished = [future for future in self.running if future.done()]
        for future in finished:
            event, slot = self.running.pop(future)
            try:
                future.result()
            except Exception as exc:
                self.failure(event, exc)
            else:
                self.queue.save(event)
                self.queue.finish(event["key"], "done")
                log.info("Done %s (worker %s)", event["key"], slot)
        return bool(finished)

    def dispatch(self, pool: ThreadPoolExecutor) -> None:
        """Заполнить свободные слоты независимыми событиями текущего снимка."""
        occupied = {slot for _, slot in self.running.values()}
        for slot in range(1, self.cfg["workers"] + 1):
            if self.stop.is_set() or time.monotonic() >= self.deadline:
                break
            if self.max_jobs is not None and self.started >= self.max_jobs:
                break
            if slot in occupied:
                continue
            event = self.queue.claim()
            if event is None:
                break
            self.started += 1
            try:
                event = refresh(event, self.snapshot, self.cfg)
                self.queue.save(event)
                if resource_keys(event) & self.queue.reserved(exclude_key=event["key"]):
                    # Пока событие ждало, PR мог перейти на занятую ветку.
                    self.queue.finish(event["key"], "queued")
                    self.started -= 1
                    continue
                # Снимок нельзя разделять между воркерами: публикация связанных
                # Issue и обновление head изменяют вложенные структуры данных.
                event = copy.deepcopy(event)
                future = pool.submit(
                    run_job,
                    event,
                    self.cfg,
                    copy.deepcopy(self.snapshot),
                    self.workspaces,
                    self.stop,
                    slot,
                    self.lock_fd,
                )
                self.running[future] = event, slot
                log.info("Started %s (worker %s)", event["key"], slot)
            except Exception as exc:
                self.failure(event, exc)

    def run(self) -> int:
        """--once выполняет один пакет до N задач; обычный режим пополняет пул."""
        next_poll = 0.0
        first_batch = True
        pool = ThreadPoolExecutor(max_workers=self.cfg["workers"], thread_name_prefix="harness")
        try:
            while not self.stop.is_set():
                if (self.state / "PAUSE").exists():
                    self.stop.set()
                    break
                finished = self.collect()
                now = time.monotonic()
                accepting = now < self.deadline and (
                    self.max_jobs is None or self.started < self.max_jobs
                )
                if self.once and not first_batch:
                    accepting = False
                if not self.running and not accepting:
                    break
                if now >= next_poll or finished:
                    self.snapshot = self.gh.snapshot()
                    observe(self.snapshot, self.queue, self.cfg)
                    excluded = self.queue.reserved(("running", "queued", "blocked"))
                    reconcile(self.snapshot, self.gh, self.cfg, self.state, excluded)
                    next_poll = time.monotonic() + self.cfg["poll_seconds"]
                if accepting:
                    self.dispatch(pool)
                first_batch = False
                if self.once and not self.running:
                    break
                if self.running:
                    wait(self.running, timeout=0.2)
                else:
                    self.stop.wait(min(0.2, max(0.0, next_poll - time.monotonic())))
        finally:
            # SIGINT/SIGTERM, ошибка опроса и PAUSE отменяют все процессы.
            # При обычном исчерпании лимита принимаемых задач пул уже пуст.
            self.stop.set()
            pool.shutdown(wait=True)
            self.collect()
        return 1 if self.failed else 0
