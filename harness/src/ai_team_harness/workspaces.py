"""Подготовка изолированных рабочих копий задач."""

import json
import re
import threading
from pathlib import Path

from .processes import command


def resource_keys(event: dict) -> set[str]:
    """Issue, её PR и ветка образуют общую область взаимного исключения."""
    keys = {f"{name}:{event[name]}" for name in ("issue", "pr", "branch") if event.get(name)}
    if event.get("issue"):
        keys.add(f"branch:ai/{event['issue']}")
    return keys


def task_name(event: dict) -> str:
    """Стабильное имя рабочей копии сохраняется между реализацией и ревью."""
    kind = "issue" if event.get("issue") else "pr"
    number = event.get(kind)
    if type(number) is not int or number < 1:
        raise ValueError("У события отсутствует корректный номер Issue/PR")
    return f"{kind}-{number}"


class Workspaces:
    """Общий clone служит источником; изменения выполняются только в worktree."""

    def __init__(self, source: Path, state: Path):
        self.source = source
        self.root = state / "worktrees"
        self.lock = threading.Lock()

    def prepare(self, event: dict, gh) -> tuple[Path, str, str]:
        """Сериализовать fetch и операции общих Git refs, затем отдать копию воркеру."""
        with self.lock:
            gh.guard(event)
            command(["git", "fetch", "origin"], cwd=self.source)
            base = gh.cfg.get("base_branch") or gh.api(f"repos/{gh.repo}")["default_branch"]
            branch = event.get("branch") or f"ai/{event['issue']}"
            if not branch.startswith("ai/") or branch == base:
                raise RuntimeError("Only ai/* task branches are allowed")
            command(["git", "check-ref-format", "--branch", branch], cwd=self.source)
            path = self.root / task_name(event)
            if not path.exists():
                self.root.mkdir(parents=True, exist_ok=True)
                refs = command(
                    ["git", "for-each-ref", "--format=%(refname:short)"], cwd=self.source
                ).splitlines()
                if branch in refs:
                    args = [str(path), branch]
                else:
                    start = f"origin/{branch}" if f"origin/{branch}" in refs else f"origin/{base}"
                    args = ["-b", branch, str(path), start]
                command(["git", "worktree", "add", *args], cwd=self.source)
            expected = git_common_dir(self.source)
            if git_common_dir(path) != expected:
                raise RuntimeError("Рабочая копия принадлежит другому репозиторию")
            branch, base = prepare(path, event, gh, base=base)
            return path, branch, base


def git_common_dir(workspace: Path) -> Path:
    raw = Path(command(["git", "rev-parse", "--git-common-dir"], cwd=workspace))
    return (workspace / raw).resolve()


def verify_origin(workspace: Path, repository: str) -> None:
    """Не публиковать код из случайно подключённого репозитория."""
    origin = command(["git", "remote", "get-url", "origin"], cwd=workspace)
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?",
        origin,
    )
    if not match or match[1].casefold() != repository.casefold():
        raise ValueError("origin рабочей копии не соответствует repository в конфиге")


def bind_state(state: Path, repository: str, git_dir: Path, project: str = "") -> None:
    """Одна постоянная очередь относится ровно к одному clone приложения."""
    identity = {"repository": repository.casefold(), "git_dir": str(git_dir), "project": project}
    path = state / "repository.json"
    if path.exists():
        if json.loads(path.read_text()) != identity:
            raise ValueError("state_dir уже используется другим репозиторием/clone")
    else:
        path.write_text(json.dumps(identity), encoding="utf-8")


def prepare(workspace, event, gh, *, base):
    def git(*args):
        return command(["git", *args], cwd=workspace)

    if git("status", "--porcelain"):
        raise RuntimeError("Checkout is dirty; preserve and inspect it before retry")
    gh.guard(event)
    default = base
    branch = event.get("branch") or f"ai/{event['issue']}"
    if not branch.startswith("ai/") or branch == default:
        raise RuntimeError("Only ai/* task branches are allowed")
    if git("branch", "--show-current") != branch:
        raise RuntimeError("Рабочая копия находится на другой ветке; требуется разбор")
    refs = git("for-each-ref", "--format=%(refname:short)").splitlines()
    if f"origin/{branch}" in refs:
        git("merge", "--ff-only", f"origin/{branch}")
    if event.get("head") and git("rev-parse", "HEAD") != event["head"]:
        raise RuntimeError("Local checkout does not match the reviewed PR head")
    return branch, default
