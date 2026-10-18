"""Ошибки процесса модели не превращаются в успешный результат."""

import os
import sys
from unittest.mock import Mock

import pytest

from ai_team_harness.runner import run_codex


@pytest.mark.parametrize("mode", ["timeout", "exit", "invalid", "valid"])
def test_runner_outcomes(tmp_path, monkeypatch, mode):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(f"""#!{sys.executable}
import json, sys, time
from pathlib import Path
mode = {mode!r}
if mode == 'timeout':
    time.sleep(10)
    raise SystemExit(1)
sys.stdin.read()
if mode == 'exit':
    raise SystemExit(7)
output = dict(outcome='answer', summary='OK', reply='OK', checks=[], deferred_issues=[])
if mode == 'invalid':
    output['outcome'] = 'unknown'
Path(sys.argv[sys.argv.index('-o') + 1]).write_text(json.dumps(output))
""")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    run_dir = tmp_path / "run"
    guard = Mock()
    if mode == "valid":
        assert run_codex(tmp_path, {}, {}, run_dir, guard)["outcome"] == "answer"
        guard.assert_called()
    else:
        error = {"timeout": TimeoutError, "exit": RuntimeError, "invalid": ValueError}[mode]
        with pytest.raises(error):
            run_codex(tmp_path, {}, {"job_timeout_seconds": 0.3}, run_dir, guard)
    assert (run_dir / "events.jsonl").exists()
    assert (run_dir / "invocation.json").exists()
