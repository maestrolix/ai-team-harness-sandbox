"""Загрузка TOML без скрытых настроек; все пути относительны файлу конфига."""

import re
import tomllib
from pathlib import Path
from typing import Any

DEFAULTS = {
    "project_owner_kind": "user",
    "workspace": ".",
    "state_dir": ".state",
    "workers": 1,
    "poll_seconds": 60,
    "job_timeout_seconds": 1800,
    "check_timeout_seconds": 300,
    "check_command": [],
    "required_checks": [],
    "environment_mode": "shared",
}
REQUIRED = {"repository", "project_owner", "project_number", "trusted_users"}


def load_config(path: Path, workers: int | None = None) -> dict[str, Any]:
    """Проверить конфигурацию до GitHub-запросов и запуска процессов."""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    unknown = raw.keys() - (DEFAULTS.keys() | REQUIRED | {"base_branch"})
    if unknown:
        raise ValueError(f"Неизвестные настройки: {', '.join(sorted(unknown))}")
    missing = REQUIRED - raw.keys()
    if missing:
        raise ValueError(f"Не заданы настройки: {', '.join(sorted(missing))}")
    cfg = DEFAULTS | raw
    if workers is not None:
        cfg["workers"] = workers
    for name in (
        "workers",
        "poll_seconds",
        "job_timeout_seconds",
        "check_timeout_seconds",
        "project_number",
    ):
        if type(cfg[name]) is not int or cfg[name] < 1:
            raise ValueError(f"{name}: требуется положительное целое число")
    for name in (
        "repository",
        "project_owner",
        "workspace",
        "state_dir",
        "project_owner_kind",
        "environment_mode",
    ):
        if not isinstance(cfg[name], str) or not cfg[name].strip():
            raise ValueError(f"{name}: требуется непустая строка")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", cfg["repository"]):
        raise ValueError("repository: ожидается owner/repository")
    if cfg["project_owner_kind"] not in {"user", "organization"}:
        raise ValueError("project_owner_kind: user или organization")
    if cfg["environment_mode"] not in {"shared", "isolated"}:
        raise ValueError("environment_mode: shared или isolated")
    if cfg["workers"] > 1 and cfg["environment_mode"] != "isolated":
        raise ValueError(
            "Для нескольких воркеров настройте изоляцию окружений и environment_mode = 'isolated'"
        )
    for name in ("trusted_users", "check_command", "required_checks"):
        if not isinstance(cfg[name], list) or any(
            not isinstance(v, str) or not v.strip() for v in cfg[name]
        ):
            raise ValueError(f"{name}: требуется массив непустых строк")
    if "base_branch" in cfg and (
        not isinstance(cfg["base_branch"], str) or not cfg["base_branch"].strip()
    ):
        raise ValueError("base_branch: требуется непустая строка")
    for name in ("workspace", "state_dir"):
        cfg[name] = str((path.parent / cfg[name]).resolve())
    if cfg["workspace"] == cfg["state_dir"]:
        raise ValueError("state_dir не должен совпадать с workspace")
    return cfg
