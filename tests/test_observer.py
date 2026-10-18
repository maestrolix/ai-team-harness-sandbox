import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import tomllib
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("observer", ROOT / "harness/observer.py")
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


def snapshot():
    return {"items": [{"id": "item-1", "fieldValueByName": {"name": "AI Open"},
                      "content": {"number": 1, "title": "Small change", "state": "OPEN",
                                  "repository": {"nameWithOwner": "owner/repo"},
                                  "labels": {"nodes": [{"name": "ai:managed"}]}}}], "prs": []}


def with_comment():
    data = snapshot()
    data["items"][0]["fieldValueByName"]["name"] = "Review"
    data["prs"] = [{"number": 2, "body": "<!-- ai-team-issue:1 -->", "labels": [{"name": "ai:managed"}],
                    "head": {"ref": "ai/1", "sha": "old", "repo": {"full_name": "owner/repo"}},
                    "discussion": [{"id": 7, "kind": "inline", "body": "Why this behavior?",
                                    "pull_request_review_id": 80, "html_url": "https://github.com/owner/repo/pull/2#discussion_r7",
                                    "updated_at": "2026-10-17T10:00:00Z", "user": {"login": "owner"}},
                                   {"id": 80, "kind": "review", "body": "", "state": "COMMENTED",
                                    "submitted_at": "2026-10-17T10:01:00Z", "user": {"login": "owner"},
                                    "html_url": "https://github.com/owner/repo/pull/2#pullrequestreview-80"}]}]
    return data


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.queue = observer.Queue(Path(self.temp.name) / "queue.db")
        self.addCleanup(self.queue.db.close)
        self.cfg = {"repository": "owner/repo", "trusted_users": ["owner"]}

    def jobs(self):
        return [dict(r) for r in self.queue.db.execute("SELECT * FROM jobs")]

    def test_ready_issue_only_once_across_restarts(self):
        observer.observe(snapshot(), self.queue, self.cfg)
        observer.observe(snapshot(), self.queue, self.cfg)
        reopened = observer.Queue(Path(self.temp.name) / "queue.db")
        self.addCleanup(reopened.db.close)
        observer.observe(snapshot(), reopened, self.cfg)
        self.assertEqual(len(self.jobs()), 1)

    def test_ready_transition_allows_explicit_resume(self):
        data = snapshot()
        observer.observe(data, self.queue, self.cfg)
        data["items"][0]["fieldValueByName"]["name"] = "Blocked"
        observer.observe(data, self.queue, self.cfg)
        data["items"][0]["fieldValueByName"]["name"] = "AI Open"
        observer.observe(data, self.queue, self.cfg)
        self.assertEqual(len(self.jobs()), 2)

    def test_closed_paused_other_repo_and_inbox_do_not_start(self):
        variants = []
        for change in [lambda d: d["items"][0]["content"].update(state="CLOSED"),
                       lambda d: d["items"][0]["content"]["labels"]["nodes"].append({"name": "ai:paused"}),
                       lambda d: d["items"][0]["content"]["repository"].update(nameWithOwner="other/repo"),
                       lambda d: d["items"][0]["fieldValueByName"].update(name="Inbox"),
                       lambda d: d["items"][0]["content"]["labels"].update(nodes=[])]:
            data = snapshot()
            change(data)
            variants.append(data)
        for data in variants:
            observer.observe(data, self.queue, self.cfg)
        self.assertEqual(self.jobs(), [])

    def test_comment_edit_is_new_event_and_own_reply_is_ignored(self):
        data = with_comment()
        observer.observe(data, self.queue, self.cfg)
        observer.observe(data, self.queue, self.cfg)
        data["prs"][0]["discussion"][0]["body"] = "Actually please explain this case"
        observer.observe(data, self.queue, self.cfg)
        data["prs"][0]["discussion"][0]["body"] = observer.MARKER + "\nAgent answer"
        observer.observe(data, self.queue, self.cfg)
        self.assertEqual(len(self.jobs()), 2)

    def test_untrusted_comment_and_paused_pr_are_ignored(self):
        data = with_comment()
        data["prs"][0]["discussion"][0]["user"]["login"] = "outsider"
        observer.observe(data, self.queue, self.cfg)
        data["prs"][0]["discussion"][0]["user"]["login"] = "owner"
        data["prs"][0]["labels"].append({"name": "ai:paused"})
        observer.observe(data, self.queue, self.cfg)
        self.assertEqual(self.jobs(), [])

    def test_refresh_uses_current_head_and_discussion(self):
        data = with_comment()
        observer.observe(data, self.queue, self.cfg)
        event = json.loads(self.jobs()[0]["payload"])
        data["prs"][0]["head"]["sha"] = "new"
        fresh = observer.refresh(event, data, self.cfg)
        self.assertEqual(fresh["head"], "new")
        self.assertEqual(fresh["feedback"][0]["comment"]["id"], 7)

    def test_withdrawn_issue_does_not_execute_queued_work(self):
        data = snapshot()
        observer.observe(data, self.queue, self.cfg)
        event = json.loads(self.jobs()[0]["payload"])
        data["items"][0]["fieldValueByName"]["name"] = "Inbox"
        with self.assertRaisesRegex(RuntimeError, "AI Open"):
            observer.refresh(event, data, self.cfg)

    def test_interrupted_job_becomes_blocked_and_not_replayed(self):
        observer.observe(snapshot(), self.queue, self.cfg)
        key = self.jobs()[0]["key"]
        self.queue.finish(key, "running")
        self.queue.recover()
        observer.observe(snapshot(), self.queue, self.cfg)
        self.assertEqual(self.jobs()[0]["status"], "blocked")
        self.assertEqual(len(self.jobs()), 1)

    def test_failed_ingestion_rolls_back_cursor_and_jobs(self):
        with patch.object(self.queue, "enqueue", side_effect=RuntimeError("disk failure")):
            with self.assertRaises(RuntimeError):
                observer.observe(snapshot(), self.queue, self.cfg)
        observer.observe(snapshot(), self.queue, self.cfg)
        self.assertEqual(len(self.jobs()), 1)

    def test_reconcile_never_approves_unverified_commit(self):
        data = with_comment()
        data["items"][0]["fieldValueByName"]["name"] = "In Progress"
        data["prs"][0]["head"]["repo"] = {"full_name": "owner/repo"}
        gh = observer.GitHub(self.cfg)
        with patch.object(gh, "api", side_effect=AssertionError("must not approve")):
            observer.reconcile(data, gh, {"required_checks": ["team-check"]}, Path(self.temp.name))


class RuntimeTests(unittest.TestCase):
    def test_network_read_retries_once_but_writes_never_replay(self):
        gh = observer.GitHub({"repository": "owner/repo"})
        with patch.object(observer, "command", side_effect=[RuntimeError("i/o timeout"), '{"state":"open"}']) as cmd, patch.object(observer.time, "sleep"):
            self.assertEqual(gh.api("repos/owner/repo/issues/1")["state"], "open")
            self.assertEqual(cmd.call_count, 2)
        with patch.object(observer, "command", side_effect=[RuntimeError("i/o timeout"), '{"data":{}}']) as cmd, patch.object(observer.time, "sleep"):
            self.assertEqual(gh.api("graphql", {"query": "query { viewer { login } }"}), {"data": {}})
            self.assertEqual(cmd.call_count, 2)
        with patch.object(observer, "command", side_effect=RuntimeError("i/o timeout")) as cmd:
            with self.assertRaises(RuntimeError):
                gh.api("repos/owner/repo/issues/1/comments", {"body": "A reply"})
            self.assertEqual(cmd.call_count, 1)

    def test_roles_use_requested_model_and_effort(self):
        roles = list((ROOT / ".codex/agents").glob("*.toml"))
        if not roles:  # Docker runtime copies role files from its mounted project.
            self.skipTest("Role files live in the project checkout")
        names = [tomllib.loads(path.read_text())["name"] for path in roles]
        self.assertEqual(set(names), set(observer.ROLES) - {"orchestrator"})
        self.assertEqual(len(names), len(set(names)))
        for path in roles:
            role = tomllib.loads(path.read_text())
            self.assertEqual(role["model"], "gpt-6.1-sol")
            self.assertEqual(role["model_reasoning_effort"], "low" if role["name"] == "qa" else "high")
            self.assertTrue(role["developer_instructions"])

    def test_process_group_is_stopped(self):
        proc = subprocess.Popen(["python3", "-c", "import time; time.sleep(60)"], start_new_session=True)
        start = time.monotonic()
        observer.stop_process(proc)
        self.assertIsNotNone(proc.poll())
        self.assertLess(time.monotonic() - start, 6)

    def test_command_failure_is_not_reported_as_success(self):
        with self.assertRaises(RuntimeError):
            observer.command(["python3", "-c", "raise SystemExit(1)"])


class DeferredIssueTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.event = {"key": "issue:1:revision", "issue": 1}
        self.issues = [{"title": "Missing optional page", "body": "Expected: open the future page.",
                        "reason": "The main scenario works without this independent page."}]
        self.board = {"project": "P1", "field": {"options": [{"name": "Inbox", "id": "inbox"}]}, "items": []}
        self.rows = []
        self.gh = Mock(repo="owner/repo")
        self.gh.pages.side_effect = lambda _: list(self.rows)
        self.gh.api.side_effect = self.api
        self.gh.graphql.return_value = {"addProjectV2ItemById": {"item": {"id": "item-10"}}}
        self.gh.status.side_effect = self.status

    def api(self, path, body):
        if path == "repos/owner/repo/issues":
            created = dict(body, number=10, node_id="issue-10", state="open", labels=[],
                           html_url="https://github.com/owner/repo/issues/10")
            self.rows.append(created)
            return created
        self.assertEqual(path, "repos/owner/repo/issues/10/labels")
        self.rows[0]["labels"] = [{"name": name} for name in body["labels"]]

    def status(self, board, item_id, name):
        board["items"] = [{"id": item_id, "fieldValueByName": {"name": name},
                           "content": {"number": 10, "repository": {"nameWithOwner": "owner/repo"}}}]

    def publish(self):
        return observer.publish_deferred(self.event, self.issues, self.gh, self.board, self.state, lambda: None)

    def test_inbox_before_managed_label_and_retry_preserves_user_status(self):
        self.assertEqual(self.publish(), ["https://github.com/owner/repo/issues/10"])
        self.assertIn("Источник: #1", self.rows[0]["body"])
        self.gh.status.assert_called_once_with(self.board, "item-10", "Inbox")
        calls = self.gh.method_calls
        self.assertLess(next(i for i, c in enumerate(calls) if c[0] == "status"),
                        next(i for i, c in enumerate(calls) if c[0] == "api" and c.args[0].endswith("/labels")))
        self.board["items"][0]["fieldValueByName"]["name"] = "In Progress"
        self.gh.reset_mock()
        self.publish()
        self.gh.api.assert_not_called()
        self.gh.status.assert_not_called()
        self.assertEqual(len(self.rows), 1)

    def test_unknown_write_recovers_existing_issue_without_duplicate(self):
        def timeout_after_create(path, body):
            self.api(path, body)
            raise RuntimeError("i/o timeout")
        self.gh.api.side_effect = timeout_after_create
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            self.publish()
        self.gh.api.side_effect = self.api
        self.publish()
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(sum(c.args[0] == "repos/owner/repo/issues" for c in self.gh.api.call_args_list), 1)

    def test_unknown_write_with_no_visible_issue_is_not_replayed(self):
        self.gh.api.side_effect = RuntimeError("i/o timeout")
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            self.publish()
        self.gh.api.side_effect = self.api
        with self.assertRaisesRegex(RuntimeError, "unknown outcome"):
            self.publish()
        self.assertEqual(self.gh.api.call_count, 1)

    def test_retry_cannot_silently_drop_or_replace_deferred_requirement(self):
        self.publish()
        self.issues = []
        self.gh.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "list changed"):
            self.publish()
        self.gh.api.assert_not_called()

    def test_blocked_result_and_incomplete_deferral_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "blocked"):
            observer.validate_result({}, {"outcome": "blocked", "deferred_issues": self.issues})
        with self.assertRaisesRegex(ValueError, "reason"):
            observer.validate_result({}, {"outcome": "ready", "deferred_issues": [{"title": "Bug", "body": "Details"}]})
        observer.validate_result({}, {"outcome": "ready", "deferred_issues": self.issues})


if __name__ == "__main__":
    unittest.main()
