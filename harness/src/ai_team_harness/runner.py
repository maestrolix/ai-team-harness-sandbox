"""Изолированный процесс Codex с тайм-аутом и журналом JSONL."""

import json
import os
import subprocess
import time

from .constants import EFFORT, MODEL
from .control import current
from .processes import stop_process
from .prompts import build_prompt
from .results import result_schema, validate_result


def run_codex(workspace, event, cfg, run_dir, guard):
    run_dir.mkdir(parents=True, exist_ok=True)
    schema = result_schema(event)
    schema_path, result_path = run_dir / "schema.json", run_dir / "result.json"
    schema_path.write_text(json.dumps(schema))
    prompt = build_prompt(event, cfg)
    args = [
        "codex",
        "exec",
        "--ignore-user-config",
        "--model",
        MODEL,
        "-c",
        f'model_reasoning_effort="{EFFORT}"',
        "-c",
        'approval_policy="never"',
        "-c",
        "features.apps=false",
        "-c",
        "features.plugins=false",
        "-c",
        "agents.max_concurrent_threads_per_session=1",
        "-c",
        f'agents.default_subagent_model="{MODEL}"',
        "-c",
        f'agents.default_subagent_reasoning_effort="{EFFORT}"',
        "--sandbox",
        "workspace-write",
        "--json",
        "--output-schema",
        str(schema_path),
        "-o",
        str(result_path),
        "-C",
        str(workspace),
        "-",
    ]
    control = current.get()
    environment = os.environ | ((control.environment or {}) if control else {})
    child_env = {
        k: v
        for k, v in environment.items()
        if k not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GH_CONFIG_DIR"}
    }
    (run_dir / "invocation.json").write_text(
        json.dumps({"model": MODEL, "effort": EFFORT, "command": args})
    )
    with (run_dir / "events.jsonl").open("w") as log, (run_dir / "stderr.log").open("w") as err:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=err,
            text=True,
            env=child_env,
            start_new_session=True,
            pass_fds=control.pass_fds if control else (),
        )
        try:
            deadline = time.monotonic() + cfg.get("job_timeout_seconds", 300)
            while True:
                guard()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Codex job exceeded its time budget")
                try:
                    proc.communicate(input=prompt, timeout=min(0.2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    prompt = None  # communicate продолжит отправку того же ввода.
            if proc.returncode:
                raise RuntimeError(
                    f"Codex exited {proc.returncode}; inspect {run_dir / 'stderr.log'}"
                )
        finally:
            # Also stop any surviving descendants of this job after the root exits.
            stop_process(proc)
    result = json.loads(result_path.read_text())
    validate_result(event, result)
    return result
