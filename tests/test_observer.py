import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import tomllib
import unittest
from unittest.mock import patch

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
        self.assertEqual(len(roles), 5)
        for path in roles:
            role = tomllib.loads(path.read_text())
            self.assertEqual(role["model"], "gpt-6.1-sol")
            self.assertEqual(role["model_reasoning_effort"], "high")
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


if __name__ == "__main__":
    unittest.main()
