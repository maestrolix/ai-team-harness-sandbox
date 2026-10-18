"""Общие изолированные репозитории для интеграционных проверок."""

import pytest

from ai_team_harness.processes import command


def git(path, *args):
    return command(["git", *args], cwd=path)


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    source = tmp_path / "source"
    git(tmp_path, "clone", str(origin), str(source))
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "README.md").write_text("initial\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "initial")
    git(source, "push", "origin", "main")
    return source, origin
