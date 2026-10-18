"""CI не переводит PR в Ready при незавершённой работе и непроверенном SHA."""

from unittest.mock import Mock

import pytest
from test_observer import with_comment

from ai_team_harness.publication import reconcile


@pytest.mark.parametrize(
    "case",
    [
        "busy",
        "unlinked",
        "paused",
        "no-item",
        "wrong-status",
        "no-checks",
        "failed-ci",
        "changed-head",
        "ready",
        "already-ready",
    ],
)
def test_reconcile(tmp_path, case):
    board = with_comment()
    item, pr = board["items"][0], board["prs"][0]
    item["fieldValueByName"]["name"] = "In Progress"
    sha = pr["head"]["sha"]
    verified = tmp_path / "verified"
    verified.mkdir()
    (verified / "2").write_text(sha)
    gh = Mock(repo="owner/repo")
    checks = [{"name": "build", "conclusion": "success", "status": "completed"}]
    current = {
        "head": {"sha": sha},
        "state": "open",
        "labels": [],
        "draft": case != "already-ready",
        "node_id": "PR2",
    }
    if case == "unlinked":
        pr["body"] = ""
    elif case == "paused":
        pr["labels"].append({"name": "ai:paused"})
    elif case == "no-item":
        board["items"].clear()
    elif case == "wrong-status":
        item["fieldValueByName"]["name"] = "Blocked"
    elif case == "failed-ci":
        checks[0]["conclusion"] = "failure"
    elif case == "changed-head":
        current["head"]["sha"] = "another"
    gh.api.side_effect = [{"check_runs": checks}, current]
    cfg = {"required_checks": [] if case == "no-checks" else ["build"]}
    reconcile(board, gh, cfg, tmp_path, {"issue:1"} if case == "busy" else set())
    if case in {"ready", "already-ready"}:
        gh.status.assert_called_once_with(board, item["id"], "Review")
        assert gh.graphql.call_count == (case == "ready")
    else:
        gh.status.assert_not_called()
        gh.graphql.assert_not_called()
