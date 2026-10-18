"""CLI, восстановление очереди и совместимые точки входа."""

import json
import runpy
import signal
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from test_config import write_config
from test_dispatcher import event

from ai_team_harness import cli
from ai_team_harness.store import Queue


def config(tmp_path, source):
    return write_config(
        tmp_path / "app.toml", workspace=str(source), state_dir=str(tmp_path / "state")
    )


def test_scan_and_once_restore_signals(repo, tmp_path, capsys):
    source, _ = repo
    cfg = config(tmp_path, source)
    gh = Mock()
    gh.snapshot.return_value = {"items": [], "prs": []}
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    with patch.object(cli, "verify_origin"), patch.object(cli, "GitHub", return_value=gh):
        assert cli.main(["run", "--config", str(cfg), "--scan-only"]) == 0
        assert capsys.readouterr().out.strip() == "[]"
        assert (
            cli.main(
                [
                    "--config",
                    str(cfg),
                    "--once",
                    "--workers",
                    "1",
                    "--max-jobs",
                    "1",
                    "--max-seconds",
                    "5",
                ]
            )
            == 0
        )
    assert before == {s: signal.getsignal(s) for s in before}


@pytest.mark.parametrize(
    "args",
    [
        ["--workers", "0"],
        ["--max-jobs", "-2"],
        ["--max-seconds", "0"],
        ["--scan-only", "--retry", "event"],
    ],
)
def test_bad_arguments_have_no_side_effects(args):
    with patch.object(cli, "load_config") as load, pytest.raises(ValueError):
        cli.main(args)
    load.assert_not_called()


def test_retry_and_signal_handler(repo, tmp_path):
    source, _ = repo
    cfg = config(tmp_path, source)
    state = tmp_path / "state"
    state.mkdir()
    with Queue(state / "queue.sqlite3").db as db:
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?)",
            ("issue:1:1", json.dumps(event(1)), "blocked", "failure", 1),
        )
    captured = {}

    def install(sig, handler):
        captured[sig] = handler
        return signal.SIG_DFL

    def execute(controller):
        assert controller.queue.db.execute("SELECT status FROM jobs").fetchone()[0] == "queued"
        captured[signal.SIGTERM](signal.SIGTERM, None)
        assert controller.stop.is_set()
        return 0

    with (
        patch.object(cli, "verify_origin"),
        patch.object(cli, "GitHub"),
        patch.object(cli.signal, "signal", side_effect=install),
        patch.object(cli.Dispatcher, "run", autospec=True, side_effect=execute),
    ):
        assert cli.main(["--config", str(cfg), "--retry", "issue:1:1"]) == 0


def test_entrypoints_and_friendly_error(capsys):
    with patch.object(cli, "main", return_value=7):
        assert cli.entrypoint() == 7
    with patch.object(cli, "main", side_effect=ValueError("bad config")):
        assert cli.entrypoint() == 1
    assert "bad config" in capsys.readouterr().err
    with patch.object(cli, "entrypoint", return_value=0), pytest.raises(SystemExit) as result:
        runpy.run_module("ai_team_harness", run_name="__main__")
    assert result.value.code == 0
    wrapper = Path(__file__).resolve().parents[1] / "observer.py"
    with patch.object(cli, "entrypoint", return_value=0), pytest.raises(SystemExit) as result:
        runpy.run_path(str(wrapper), run_name="__main__")
    assert result.value.code == 0
