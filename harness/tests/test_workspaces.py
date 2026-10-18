"""Изоляция проверяется на настоящих локальных Git-репозиториях."""

import json
from unittest.mock import Mock

import pytest

from ai_team_harness.processes import command
from ai_team_harness.workspaces import (
    Workspaces,
    bind_state,
    git_common_dir,
    prepare,
    verify_origin,
)


def git(path, *args):
    return command(["git", *args], cwd=path)


def client():
    gh = Mock(repo="owner/repo", cfg={})
    gh.api.return_value = {"default_branch": "main"}
    return gh


def test_independent_worktrees_reuse_and_remote_branch(repo, tmp_path):
    source, origin = repo
    gh = client()
    manager = Workspaces(source, tmp_path / "state")
    a, branch, base = manager.prepare({"issue": 1}, gh)
    b, _, _ = manager.prepare({"issue": 2}, gh)
    assert branch == "ai/1" and base == "main"
    assert a != b and git(a, "branch", "--show-current") == "ai/1"
    (a / "only-one").write_text("one")
    assert not (b / "only-one").exists()
    assert git(source, "status", "--porcelain") == ""
    with pytest.raises(RuntimeError, match="dirty"):
        manager.prepare({"issue": 1}, gh)
    (a / "only-one").unlink()
    assert manager.prepare({"issue": 1}, gh)[0] == a
    git(a, "push", "origin", "ai/1")
    sha = git(a, "rev-parse", "HEAD")
    assert manager.prepare({"issue": 1, "pr": 11, "branch": "ai/1", "head": sha}, gh)[0] == a
    with pytest.raises(RuntimeError, match="reviewed PR head"):
        manager.prepare({"issue": 1, "head": "outdated"}, gh)
    # Отдельный clone знает remote-ветку, но ещё не имеет её локально.
    other = tmp_path / "other"
    git(tmp_path, "clone", str(origin), str(other))
    other_manager = Workspaces(other, tmp_path / "other-state")
    assert other_manager.prepare({"issue": 1}, gh)[1] == "ai/1"
    # Чистый worktree можно восстановить с сохранённой локальной веткой.
    git(source, "worktree", "remove", str(b))
    assert manager.prepare({"issue": 2}, gh)[0] == b


def test_wrong_branch_and_foreign_worktree(repo, tmp_path):
    source, _ = repo
    gh = client()
    manager = Workspaces(source, tmp_path / "state")
    with pytest.raises(RuntimeError, match="ai/"):
        manager.prepare({"issue": 1, "branch": "main"}, gh)
    path, _, _ = manager.prepare({"issue": 1}, gh)
    git(path, "switch", "-c", "ai/unexpected")
    with pytest.raises(RuntimeError, match="другой ветке"):
        manager.prepare({"issue": 1}, gh)
    with pytest.raises(RuntimeError, match="ai/"):
        prepare(path, {"issue": 1, "branch": "main"}, gh, base="main")
    foreign = manager.root / "issue-2"
    git(tmp_path, "init", str(foreign))
    with pytest.raises(RuntimeError, match="другому репозиторию"):
        manager.prepare({"issue": 2}, gh)
    gh.cfg["base_branch"] = "ai/1"
    with pytest.raises(RuntimeError, match="ai/"):
        manager.prepare({"issue": 1}, gh)


def test_origin_and_state_identity(repo, tmp_path):
    source, _ = repo
    with pytest.raises(ValueError, match="origin"):
        verify_origin(source, "owner/repo")
    git(source, "remote", "set-url", "origin", "git@github.com:Owner/Repo.git")
    verify_origin(source, "owner/repo")
    with pytest.raises(ValueError):
        verify_origin(source, "owner/other")
    common = git_common_dir(source)
    assert common == source / ".git"
    bind_state(tmp_path, "Owner/Repo", common)
    bind_state(tmp_path, "owner/repo", common)
    assert json.loads((tmp_path / "repository.json").read_text())["repository"] == "owner/repo"
    with pytest.raises(ValueError, match="state_dir"):
        bind_state(tmp_path, "owner/other", common)
