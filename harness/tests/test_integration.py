"""Три настоящих процесса, worktree, проверки, commit и push в локальный remote.

Подменены только внешние сервисы GitHub и модель: тест не тратит токены и не
создаёт пользовательских Issue/PR. Git, SQLite, планировщик и subprocess настоящие.
"""

import copy
import os
import sys
import threading
from pathlib import Path
from unittest.mock import patch

from test_dispatcher import snapshot
from test_workspaces import git

from ai_team_harness import dispatcher
from ai_team_harness.control import exclusive_lock
from ai_team_harness.github import GitHub
from ai_team_harness.store import Queue
from ai_team_harness.workspaces import Workspaces, git_common_dir


class GitHubServer:
    def __init__(self):
        self.lock = threading.RLock()
        self.board = snapshot()
        self.board.update(
            project="P",
            field={
                "id": "S",
                "options": [
                    {"id": status, "name": status}
                    for status in ["Inbox", "In Progress", "Blocked", "Review"]
                ],
            },
        )
        self.pulls = []
        self.comments = []

    def client(self, cfg):
        server = self

        class Client(GitHub):
            def snapshot(self):
                with server.lock:
                    return copy.deepcopy(server.board)

            def graphql(self, query, **variables):
                assert "updateProjectV2ItemFieldValue" in query
                with server.lock:
                    item = next(i for i in server.board["items"] if i["id"] == variables["i"])
                    item["fieldValueByName"]["name"] = variables["o"]
                return {}

            def api(self, path, body=None, method=None):
                with server.lock:
                    if path == "repos/owner/repo":
                        return {"default_branch": "main"}
                    if path.endswith("/labels"):
                        return {}
                    if "/comments" in path:
                        if body:
                            server.comments.append((path, body))
                            return body
                        return [b for p, b in server.comments if p == path.split("?")[0]]
                    if "/issues/" in path:
                        return {"state": "open", "labels": [{"name": "ai:managed"}]}
                    if path.endswith("/pulls"):
                        number = 10 + len(server.pulls)
                        pull = dict(
                            body,
                            number=number,
                            node_id=f"PR{number}",
                            html_url=f"https://github.com/owner/repo/pull/{number}",
                        )
                        server.pulls.append(pull)
                        return pull
                    if "/pulls?" in path:
                        branch = path.split("head=owner:")[1]
                        return [p for p in server.pulls if p["head"] == branch]
                    raise AssertionError(path)

        return Client(cfg)


def test_three_tasks_run_in_parallel_and_publish_independent_branches(repo, tmp_path, monkeypatch):
    source, origin = repo
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    fake = bin_dir / "codex"
    fake.write_text(f"""#!{sys.executable}
import json, os, sys, time
from pathlib import Path
prompt = sys.stdin.read()
event = json.loads(prompt.split("ДАННЫЕ СОБЫТИЯ:\\n", 1)[1])
number = event["issue"]
barrier = Path(os.environ["FAKE_BARRIER"])
(barrier / str(number)).write_text(os.environ["HARNESS_WORKER_ID"])
end = time.monotonic() + 8
while len(list(barrier.iterdir())) != 3:
    if time.monotonic() > end:
        raise SystemExit("Three model processes did not overlap")
    time.sleep(0.02)
workspace = Path(sys.argv[sys.argv.index("-C") + 1])
assert os.environ["HARNESS_WORKSPACE"] == str(workspace)
assert "GH_TOKEN" not in os.environ
(workspace / f"issue-{{number}}.txt").write_text(str(number))
result = dict(outcome="ready", summary=f"Task {{number}}", reply="Checked", checks=["local"], deferred_issues=[], planned_issues=[])
Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps(result))
""")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_BARRIER", str(barrier))
    monkeypatch.setenv("GH_TOKEN", "must-not-reach-the-model")
    state = tmp_path / "state"
    state.mkdir()
    cfg = dict(
        repository="owner/repo",
        trusted_users=["owner"],
        state_dir=str(state),
        workspace=str(source),
        workers=3,
        poll_seconds=1,
        job_timeout_seconds=20,
        check_timeout_seconds=5,
        required_checks=[],
        check_command=[
            sys.executable,
            "-c",
            "from pathlib import Path; assert len(list(Path('.').glob('issue-*.txt'))) == 1",
        ],
    )
    server = GitHubServer()
    queue = Queue(state / "queue.sqlite3")
    try:
        with (
            exclusive_lock(git_common_dir(source) / "ai-team.lock") as fd,
            patch.object(dispatcher, "GitHub", side_effect=server.client),
        ):
            controller = dispatcher.Dispatcher(
                cfg,
                queue,
                server.client(cfg),
                Workspaces(source, state),
                threading.Event(),
                fd,
                once=True,
            )
            assert controller.run() == 0
        assert [row[0] for row in queue.db.execute("SELECT status FROM jobs")] == ["done"] * 3
    finally:
        queue.close()
    assert len(server.pulls) == 3
    assert len(server.comments) == 3
    assert all(p["draft"] and p["base"] == "main" for p in server.pulls)
    assert git(source, "branch", "--show-current") == "main"
    assert git(source, "status", "--porcelain") == ""
    assert len(list((state / "verified").iterdir())) == 3
    for number in (1, 2, 3):
        files = git(origin, "ls-tree", "--name-only", f"ai/{number}").splitlines()
        assert files == ["README.md", f"issue-{number}.txt"]
        assert (state / "worktrees" / f"issue-{number}" / f"issue-{number}.txt").read_text() == str(
            number
        )


def test_sigterm_stops_three_model_processes_and_restart_does_not_replay(repo, tmp_path):
    """Настоящий CLI получает SIGTERM; повторный запуск сохраняет blocked-задачи."""
    import json
    import signal
    import sqlite3
    import subprocess
    import time

    import pytest
    from test_config import write_config

    source, _ = repo
    state = tmp_path / "state"
    barrier = tmp_path / "pids"
    barrier.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    fake.write_text(f"""#!{sys.executable}
import json, os, sys, time
from pathlib import Path
event = json.loads(sys.stdin.read().split("ДАННЫЕ СОБЫТИЯ:\\n", 1)[1])
Path(os.environ['FAKE_BARRIER'], str(event['issue'])).write_text(str(os.getpid()))
time.sleep(30)
""")
    fake.chmod(0o755)
    cfg = write_config(
        tmp_path / "app.toml",
        workspace=str(source),
        state_dir=str(state),
        workers=3,
        environment_mode="isolated",
        poll_seconds=1,
        job_timeout_seconds=30,
    )
    script = """
import sys
from unittest.mock import patch
from ai_team_harness import cli, dispatcher
from test_integration import GitHubServer
server = GitHubServer()
with patch.object(cli, 'verify_origin'), patch.object(cli, 'GitHub', side_effect=server.client), patch.object(dispatcher, 'GitHub', side_effect=server.client):
    raise SystemExit(cli.main(['--config', sys.argv[1], '--once']))
"""
    env = os.environ | {
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "FAKE_BARRIER": str(barrier),
        "PYTHONPATH": str(Path(__file__).parent),
    }
    with (tmp_path / "cli.log").open("w") as output:
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(cfg)],
            env=env,
            stdout=output,
            stderr=output,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while len(list(barrier.iterdir())) < 3 and time.monotonic() < deadline:
                assert proc.poll() is None, (tmp_path / "cli.log").read_text()
                time.sleep(0.02)
            assert len(list(barrier.iterdir())) == 3
            pids = {p.name: int(p.read_text()) for p in barrier.iterdir()}
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=10) == 1
            for pid in pids.values():
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
            second = subprocess.run(
                [sys.executable, "-c", script, str(cfg)],
                env=env,
                stdout=output,
                stderr=output,
                timeout=10,
            )
            assert second.returncode == 0, (tmp_path / "cli.log").read_text()
            assert pids == {p.name: int(p.read_text()) for p in barrier.iterdir()}
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    db = sqlite3.connect(state / "queue.sqlite3")
    try:
        rows = db.execute("SELECT payload,status FROM jobs").fetchall()
        assert len(rows) == 3
        assert all(status == "blocked" for _, status in rows)
        assert {json.loads(payload)["issue"] for payload, _ in rows} == {1, 2, 3}
    finally:
        db.close()
