"""Защита границ публикации: не терять требования и не заявлять ложный успех."""

import copy
import os
import sys
import threading
from unittest.mock import Mock, patch

import pytest
import test_observer
import test_review_batches

from ai_team_harness import dispatcher, results, workflow
from ai_team_harness.control import Control, scope
from ai_team_harness.processes import command
from ai_team_harness.store import Queue


@pytest.fixture
def deferred():
    case = test_observer.DeferredIssueTests()
    case.setUp()
    yield case
    case.doCleanups()


@pytest.mark.parametrize("case", ["no-inbox", "duplicates", "closed", "moved", "no-status"])
def test_deferred_issue_boundaries(deferred, case):
    if case == "no-inbox":
        deferred.board["field"]["options"].clear()
    else:
        deferred.publish()
        if case == "duplicates":
            deferred.rows.append(copy.deepcopy(deferred.rows[0]))
        elif case == "closed":
            deferred.rows[0]["state"] = "closed"
        elif case == "moved":
            deferred.rows[0]["labels"] = []
            deferred.board["items"][0]["fieldValueByName"]["name"] = "Review"
        elif case == "no-status":
            deferred.board["items"][0].pop("fieldValueByName")
    if case == "no-status":
        deferred.publish()
        assert deferred.board["items"][0]["fieldValueByName"]["name"] == "Inbox"
    else:
        with pytest.raises(RuntimeError):
            deferred.publish()


def test_duplicate_or_overlong_deferred_titles():
    issue = dict(title="Missing page", body="Expected", reason="Independent")
    with pytest.raises(ValueError, match="Duplicate"):
        results.validate_result(
            {},
            dict(outcome="answer", deferred_issues=[issue, issue | {"title": " MISSING   page "}]),
        )
    with pytest.raises(ValueError, match="long"):
        results.validate_result(
            {}, dict(outcome="answer", deferred_issues=[issue | {"title": "a" * 241}])
        )


@pytest.fixture
def publication():
    case = test_review_batches.PublicationTests()
    case.setUp()
    yield case
    case.doCleanups()


@pytest.mark.parametrize(
    "case", ["pause", "branch", "sha", "missing-check", "closed", "undraft", "deferred"]
)
def test_publication_guardrails(publication, case):
    original = publication.command
    original_api = publication.api
    publication.dirty = case in {"missing-check", "undraft", "deferred"}
    decision, outcome = ("fixed", "ready") if publication.dirty else ("answered", "answer")
    if case == "pause":
        (publication.state / "PAUSE").touch()
    elif case == "missing-check":
        publication.cfg["check_command"] = []
    elif case in {"branch", "sha"}:

        def changed(args, **kwargs):
            if args[1] == ("branch" if case == "branch" else "rev-parse"):
                return "unexpected"
            return original(args, **kwargs)

        publication.command = changed
    elif case in {"closed", "undraft"}:

        def changed_api(path, body=None):
            response = original_api(path, body)
            if "/issues/" in path and case == "closed":
                response["state"] = "closed"
            elif case == "undraft" and not isinstance(response, list):
                response["draft"] = False
            return response

        publication.gh.api.side_effect = changed_api
    if case == "deferred":
        with patch.object(
            workflow, "publish_issues", side_effect=[["https://github.com/owner/repo/issues/3"], []]
        ):
            response = publication.execute(decision, outcome)
        assert "не исправлены" in response["reply"]
    elif case == "undraft":
        publication.execute(decision, outcome)
        assert "convertPullRequestToDraft" in publication.gh.graphql.call_args.args[0]
    else:
        with pytest.raises(RuntimeError):
            publication.execute(decision, outcome)
        publication.gh.reply.assert_not_called()


def test_explicit_command_environment_is_preserved(tmp_path):
    with scope(Control(threading.Event(), tmp_path, environment={"HELLO": "from scope"})):
        assert (
            command(
                [sys.executable, "-c", "import os; print(os.environ['HELLO'])"],
                env=os.environ | {"HELLO": "explicit"},
            )
            == "explicit"
        )


def test_unrelated_feedback_does_not_reserve_other_pr(tmp_path):
    queue = Queue(tmp_path / "queue.db")
    try:
        queue.enqueue({"key": "other", "pr": 9})
        assert queue.claimed_feedback({"number": 2}, {}) == set()
    finally:
        queue.close()


def test_total_limit_smaller_than_pool(tmp_path):
    queue = Queue(tmp_path / "queue.db")
    cfg = dict(workers=3, state_dir=str(tmp_path), repository="owner/repo", trusted_users=["owner"])
    obj = dispatcher.Dispatcher(cfg, queue, Mock(), Mock(), threading.Event(), -1, max_jobs=1)
    obj.started = 1
    try:
        obj.dispatch(Mock())
        assert obj.running == {}
    finally:
        queue.close()
