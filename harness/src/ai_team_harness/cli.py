"""Командная строка самостоятельного harness; глобальные записи делает диспетчер."""

import argparse
import contextlib
import json
import logging
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .config import load_config
from .control import Control, exclusive_lock, scope
from .dispatcher import Dispatcher
from .events import observe
from .github import GitHub
from .store import Queue
from .workspaces import Workspaces, bind_state, git_common_dir, verify_origin


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Harness: параллельная обработка независимых задач GitHub"
    )
    result.add_argument("action", nargs="?", choices=["run"], default="run")
    result.add_argument("--config", type=Path, default=Path("harness.toml"))
    result.add_argument("--workers", type=int, help="Количество одновременно выполняемых задач")
    result.add_argument(
        "--once", action="store_true", help="Один пакет не более N независимых задач"
    )
    result.add_argument(
        "--scan-only",
        action="store_true",
        help="Прочитать GitHub и очередь без запуска модели и публикации",
    )
    result.add_argument(
        "--max-jobs", type=int, help="Лимит задач за запуск; по умолчанию не ограничен"
    )
    result.add_argument(
        "--max-seconds",
        type=int,
        help="Через N секунд перестать брать новые задачи; активные завершить",
    )
    result.add_argument(
        "--retry", help="Повторить конкретное blocked-событие после ручного разбора"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    for name in ("workers", "max_jobs", "max_seconds"):
        value = getattr(args, name)
        if value is not None and value < 1:
            raise ValueError(f"--{name.replace('_', '-')}: требуется положительное число")
    if args.scan_only and args.retry:
        raise ValueError("--scan-only нельзя совмещать с --retry")
    cfg = load_config(args.config.resolve(), args.workers)
    workspace, state = Path(cfg["workspace"]), Path(cfg["state_dir"])
    verify_origin(workspace, cfg["repository"])
    git_dir = git_common_dir(workspace)
    stop = threading.Event()
    with (
        exclusive_lock(git_dir / "ai-team.lock") as lock_fd,
        exclusive_lock(state / "dispatcher.lock"),
    ):
        bind_state(
            state,
            cfg["repository"],
            git_dir,
            f"{cfg['project_owner_kind']}/{cfg['project_owner']}/{cfg['project_number']}",
        )
        with contextlib.closing(Queue(state / "queue.sqlite3")) as queue:
            gh = GitHub(cfg)
            if args.scan_only:
                observe(gh.snapshot(), queue, cfg)
                print(
                    json.dumps(
                        [
                            dict(row)
                            for row in queue.db.execute("SELECT key,status,error FROM jobs")
                        ],
                        ensure_ascii=False,
                    )
                )
                return 0
            queue.recover()
            if args.retry:
                queue.retry(args.retry)
            previous = {
                sig: signal.signal(sig, lambda *_: stop.set())
                for sig in (signal.SIGINT, signal.SIGTERM)
            }
            try:
                dispatcher = Dispatcher(
                    cfg,
                    queue,
                    gh,
                    Workspaces(workspace, state),
                    stop,
                    lock_fd,
                    max_jobs=args.max_jobs,
                    max_seconds=args.max_seconds,
                    once=args.once,
                )
                with scope(Control(stop, state, lock_fd=lock_fd)):
                    return dispatcher.run()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)


def entrypoint() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return main()
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Harness остановлен: {exc}", file=sys.stderr)
        return 1
