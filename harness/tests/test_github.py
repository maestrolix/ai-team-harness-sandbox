"""Граничные ответы GitHub: пагинация, доступ, статусы, идемпотентность."""

import copy
import subprocess
from unittest.mock import patch

import pytest
from test_observer import with_comment

from ai_team_harness import events, github


def project_page(has_next=False):
    return {
        "id": "P",
        "fields": {
            "nodes": [
                {
                    "id": "S",
                    "name": "Status",
                    "options": [
                        {"name": s, "id": s}
                        for s in ["Inbox", "AI Open", "In Progress", "Review", "Blocked", "Done"]
                    ],
                }
            ]
        },
        "items": {"nodes": [], "pageInfo": {"hasNextPage": has_next, "endCursor": "next"}},
    }


def test_api_empty_post_and_graphql_errors():
    gh = github.GitHub({"repository": "owner/repo"})
    with patch.object(github, "command", return_value="") as run:
        assert gh.api("path", {"body": "yes"}) is None
    assert run.call_args.args[0][-2:] == ["--input", "-"]
    with (
        patch.object(gh, "api", return_value={"errors": ["denied"]}),
        pytest.raises(RuntimeError, match="denied"),
    ):
        gh.graphql("query {}")
    with patch.object(gh, "api", return_value={"data": {"result": 1}}):
        assert gh.graphql("query {}") == {"result": 1}
    with (
        patch.object(github, "command", side_effect=[subprocess.TimeoutExpired("gh", 45), "{}"]),
        patch.object(github.time, "sleep"),
    ):
        assert gh.api("path") == {}
    with (
        patch.object(github, "command", side_effect=RuntimeError("forbidden")) as run,
        pytest.raises(RuntimeError),
    ):
        gh.api("path")
    assert run.call_count == 1
    with (
        patch.object(github, "command", side_effect=RuntimeError("timeout")) as run,
        patch.object(github.time, "sleep"),
        pytest.raises(RuntimeError),
    ):
        gh.api("path")
    assert run.call_count == 2


def test_pagination():
    gh = github.GitHub({"repository": "owner/repo"})
    with patch.object(gh, "api", side_effect=[list(range(100)), [100]]) as api:
        assert gh.pages("path?kind=all") == list(range(101))
    assert api.call_args_list[1].args[0] == "path?kind=all&per_page=100&page=2"


def test_snapshot_validation_and_pagination():
    gh = github.GitHub(dict(repository="owner/repo", project_owner="owner", project_number=1))
    pr = with_comment()["prs"][0]
    other = copy.deepcopy(pr)
    other["head"]["repo"]["full_name"] = "foreign/repo"
    plain = dict(pr, labels=[])
    with (
        patch.object(
            gh,
            "graphql",
            side_effect=[
                {"user": {"projectV2": project_page(True)}},
                {"user": {"projectV2": project_page()}},
            ],
        ),
        patch.object(gh, "pages", return_value=[pr, other, plain]),
        patch.object(gh, "discussion", return_value=[]) as discussion,
    ):
        snapshot = gh.snapshot()
    assert snapshot["prs"] == [pr]
    discussion.assert_called_once()
    with (
        patch.object(gh, "graphql", return_value={"user": {"projectV2": None}}),
        pytest.raises(RuntimeError, match="accessible"),
    ):
        gh.snapshot()
    page = project_page()
    page["fields"]["nodes"][0]["options"] = []
    with (
        patch.object(gh, "graphql", return_value={"user": {"projectV2": page}}),
        pytest.raises(RuntimeError, match="Status"),
    ):
        gh.snapshot()
    gh.cfg["project_owner_kind"] = "invalid"
    with pytest.raises(ValueError, match="project_owner_kind"):
        gh.snapshot()


def test_guard_issue_pr_and_status():
    gh = github.GitHub({"repository": "owner/repo"})
    good = {"state": "open", "labels": [{"name": "ai:managed"}]}
    with patch.object(gh, "api", return_value=good):
        gh.guard({"issue": 1})
        gh.guard({})
    with (
        patch.object(gh, "api", return_value=good | {"state": "closed"}),
        pytest.raises(RuntimeError, match="Paused"),
    ):
        gh.guard({"issue": 1})
    with patch.object(
        gh, "api", side_effect=[good, {"head": {"repo": {"full_name": "owner/repo"}, "sha": "sha"}}]
    ):
        gh.guard({"pr": 2, "head": "sha"})
    with patch.object(gh, "graphql") as mutate:
        gh.status({}, None, "Review")
        mutate.assert_not_called()
        gh.status(
            {"project": "P", "field": {"id": "S", "options": [{"name": "Review", "id": "R"}]}},
            "I",
            "Review",
        )
        assert mutate.call_args.kwargs == dict(p="P", i="I", f="S", o="R")


def test_refresh_rejects_unmanaged_issue_and_missing_pr():
    cfg = {"repository": "owner/repo"}
    with pytest.raises(RuntimeError, match="closed"):
        events.refresh({"pr": 1}, {"prs": []}, cfg)
    snapshot = {
        "items": [
            {
                "id": "item",
                "fieldValueByName": {"name": "AI Open"},
                "content": {"repository": {"nameWithOwner": "owner/repo"}, "labels": []},
            }
        ]
    }
    with pytest.raises(RuntimeError, match="opted in"):
        events.refresh({"item": "item"}, snapshot, cfg)


def test_refresh_does_not_follow_relinked_issue():
    board = with_comment()
    ev = {"issue": 99, "pr": 2, "feedback": []}
    with pytest.raises(RuntimeError, match="issue link changed"):
        events.refresh(ev, board, {"repository": "owner/repo", "trusted_users": ["owner"]})
