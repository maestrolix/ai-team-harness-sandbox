"""Конфигурация проверяется до любых побочных эффектов."""

import json

import pytest

from ai_team_harness.config import load_config


def write_config(path, **changes):
    values = (
        dict(
            repository="owner/repo",
            project_owner="owner",
            project_number=1,
            trusted_users=["owner"],
            workspace="repo",
            state_dir="state",
        )
        | changes
    )
    path.write_text("\n".join(f"{key} = {json.dumps(value)}" for key, value in values.items()))
    return path


def test_paths_defaults_and_override(tmp_path):
    path = write_config(tmp_path / "app.toml", environment_mode="isolated", base_branch="main")
    config = load_config(path, workers=3)
    assert config["workers"] == 3
    assert config["workspace"] == str(tmp_path / "repo")
    assert config["state_dir"] == str(tmp_path / "state")
    assert config["check_command"] == []
    assert load_config(path)["workers"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"typo": 1},
        {"workers": 0},
        {"workers": True},
        {"poll_seconds": -1},
        {"project_number": "1"},
        {"repository": ""},
        {"repository": "a/b/c"},
        {"project_owner": 2},
        {"project_owner_kind": "team"},
        {"environment_mode": "none"},
        {"workers": 3},
        {"trusted_users": "owner"},
        {"check_command": [""]},
        {"required_checks": [7]},
        {"base_branch": " "},
        {"base_branch": 12},
        {"workspace": "repo", "state_dir": "repo"},
    ],
)
def test_invalid_settings(tmp_path, changes):
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path / "app.toml", **changes))


def test_missing_fields_and_malformed_toml(tmp_path):
    path = tmp_path / "app.toml"
    path.write_text("")
    with pytest.raises(ValueError, match="Не заданы"):
        load_config(path)
    path.write_text("invalid [")
    with pytest.raises(ValueError):
        load_config(path)
