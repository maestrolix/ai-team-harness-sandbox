"""Behavioral regressions for published-review batches; no network or model runs."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("review_batch_observer", ROOT / "harness/observer.py")
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)
CFG = {"repository": "owner/repo", "trusted_users": ["owner"]}


def comment(number, kind="inline", body=None, **extra):
    result = {"id": number, "kind": kind, "body": body or f"Please inspect {number}",
              "user": {"login": "owner"}, "updated_at": "2026-10-17T12:00:00Z",
              "html_url": f"https://github.com/owner/repo/pull/2#discussion_r{number}"}
    if kind == "review":
        result.update(state="CHANGES_REQUESTED", submitted_at="2026-10-17T12:00:00Z")
    elif kind == "inline":
        result["pull_request_review_id"] = 80
    result.update(extra)
    return result


def snapshot():
    return {"items": [], "prs": [{"number": 2, "body": "<!-- ai-team-issue:1 -->",
            "labels": [{"name": "ai:managed"}],
            "head": {"ref": "ai/1", "sha": "old", "repo": {"full_name": "owner/repo"}},
            "discussion": [comment(80, "review"), comment(7), comment(8)]}]}


def event():
    pr = snapshot()["prs"][0]
    return {"key": "batch", "kind": "feedback", "pr": 2, "issue": 1,
            "head": "old", "branch": "ai/1", "context": {"pr": pr},
            "feedback": observer.feedback_groups(pr, CFG)["review:80"]}


def result(ev, decision="answered", outcome="answer"):
    return {"outcome": outcome, "summary": "Review complete", "reply": "Batch complete", "checks": [],
            "decisions": [{"id": f["id"], "role": "backend", "decision": decision,
                           "reply": f"Disposition for {f['id']}"} for f in ev["feedback"]]}


class BatchQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "queue.db"
        self.queue = observer.Queue(self.path)
        self.addCleanup(self.queue.db.close)

    def jobs(self, status=None):
        rows = self.queue.db.execute("SELECT * FROM jobs ORDER BY created").fetchall()
        return [dict(row, event=json.loads(row["payload"])) for row in rows if status is None or row["status"] == status]

    def test_pending_review_and_inline_are_absent_even_from_model_context(self):
        gh = observer.GitHub(CFG)
        pending = comment(81, "review", state="PENDING", submitted_at=None)
        published = comment(80, "review")
        paths = []
        def pages(path):
            paths.append(path)
            if path.endswith("/reviews"):
                return [pending, published]
            if path.endswith("/reviews/80/comments"):
                return [comment(7)]
            if path.endswith("/issues/2/comments"):
                return []
            self.fail(f"Pending review comments must not be fetched: {path}")
        with patch.object(gh, "pages", side_effect=pages):
            discussion = gh.discussion(2)
        self.assertEqual({c["id"] for c in discussion}, {80, 7})
        data = snapshot()
        data["prs"][0]["discussion"] = [pending, comment(9, pull_request_review_id=81)]
        observer.observe(data, self.queue, CFG)
        self.assertEqual(self.jobs(), [])

    def test_review_body_and_all_inline_are_one_persistent_job(self):
        data = snapshot()
        observer.observe(data, self.queue, CFG)
        observer.observe(data, self.queue, CFG)
        reopened = observer.Queue(self.path)
        self.addCleanup(reopened.db.close)
        observer.observe(data, reopened, CFG)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual({f["id"] for f in self.jobs()[0]["event"]["feedback"]},
                         {"review:80", "inline:7", "inline:8"})

    def test_completed_legacy_comments_not_replayed_and_queued_ones_migrate(self):
        ev = event()
        for status, feedback in zip(["done", "queued", "blocked"], ev["feedback"]):
            legacy = {"key": feedback["key"], "kind": feedback["comment"]["kind"], "pr": 2}
            self.queue.enqueue(legacy)
            self.queue.finish(legacy["key"], status)
        observer.observe(snapshot(), self.queue, CFG)
        queued = self.jobs("queued")
        self.assertEqual(len(queued), 1)
        self.assertEqual([f["id"] for f in queued[0]["event"]["feedback"]], ["inline:7"])
        self.assertEqual(len(self.jobs("superseded")), 1)
        observer.observe(snapshot(), self.queue, CFG)
        self.assertEqual(len(self.jobs("queued")), 1)

    def test_editing_queued_comment_preserves_other_unprocessed_comments(self):
        data = snapshot()
        observer.observe(data, self.queue, CFG)
        data["prs"][0]["discussion"][1]["body"] = "Updated concern"
        observer.observe(data, self.queue, CFG)
        active = self.jobs("queued")
        self.assertEqual(len(active), 1)
        self.assertEqual({f["id"] for f in active[0]["event"]["feedback"]},
                         {"review:80", "inline:7", "inline:8"})
        self.assertEqual(next(f["comment"]["body"] for f in active[0]["event"]["feedback"]
                              if f["id"] == "inline:7"), "Updated concern")

    def test_comment_arriving_during_running_batch_is_next_batch_only(self):
        data = snapshot()
        observer.observe(data, self.queue, CFG)
        first = self.jobs()[0]
        self.queue.finish(first["key"], "running")
        data["prs"][0]["discussion"].append(comment(9))
        observer.observe(data, self.queue, CFG)
        observer.observe(data, self.queue, CFG)
        self.assertEqual(len(self.jobs("running")), 1)
        self.assertEqual(len(self.jobs("queued")), 1)
        self.assertEqual([f["id"] for f in self.jobs("queued")[0]["event"]["feedback"]], ["inline:9"])
        self.assertEqual(len(self.jobs("running")[0]["event"]["feedback"]), 3)

    def test_changed_deleted_or_untrusted_feedback_cannot_execute_old_batch(self):
        for mutation in [lambda p: p["discussion"][1].update(body="Edited"),
                         lambda p: p["discussion"].pop(),
                         lambda p: p["discussion"][1]["user"].update(login="outsider"),
                         lambda p: p["discussion"][0].update(state="PENDING", submitted_at=None)]:
            with self.subTest(mutation=mutation):
                data = snapshot()
                mutation(data["prs"][0])
                with self.assertRaisesRegex(RuntimeError, "feedback"):
                    observer.refresh(event(), data, CFG)


class DecisionTests(unittest.TestCase):
    def test_every_id_requires_one_valid_disposition(self):
        ev = event()
        good = result(ev)
        observer.validate_result(ev, good)
        for decisions in [good["decisions"][:-1], good["decisions"] + [good["decisions"][0]],
                          good["decisions"][:-1] + [dict(good["decisions"][-1], id="inline:foreign")],
                          [dict(d, role="unknown") for d in good["decisions"]],
                          [dict(d, reply="  ") for d in good["decisions"]]]:
            with self.subTest(decisions=decisions), self.assertRaises(ValueError):
                observer.validate_result(ev, dict(good, decisions=decisions))

    def test_disposition_controls_publication_and_missing_business_input_blocks(self):
        ev = event()
        for decision, outcome in [("answered", "ready"), ("disagreed", "ready"),
                                  ("fixed", "answer"), ("needs_input", "answer"), ("needs_input", "ready")]:
            with self.subTest(decision=decision, outcome=outcome), self.assertRaises(ValueError):
                observer.validate_result(ev, result(ev, decision, outcome))
        observer.validate_result(ev, result(ev, "needs_input", "blocked"))

    def test_replies_use_original_inline_root_and_do_not_duplicate_after_retry(self):
        ev = event()
        ev["feedback"][1]["comment"]["in_reply_to_id"] = 3
        gh = observer.GitHub(CFG)
        stored, writes = {}, []
        def api(path, body=None):
            writes.append((path, body))
            stored.setdefault(path, []).append(body)
        with patch.object(gh, "pages", side_effect=lambda path: stored.get(path, [])), \
             patch.object(gh, "api", side_effect=api):
            observer.reply_decisions(ev, result(ev), gh)
            observer.reply_decisions(ev, result(ev), gh)
        self.assertEqual(len(writes), 3)
        inline = [body for path, body in writes if "/pulls/" in path]
        self.assertEqual([p["in_reply_to"] for p in inline], [3, 8])
        self.assertIn(ev["feedback"][0]["comment"]["html_url"], writes[0][1]["body"])

    def test_fixed_reply_is_withheld_until_publication_has_sha(self):
        ev = event()
        gh = Mock()
        observer.reply_decisions(ev, result(ev, "fixed", "ready"), gh)
        gh.reply.assert_not_called()
        observer.reply_decisions(ev, result(ev, "fixed", "ready"), gh, "published123")
        self.assertEqual(gh.reply.call_count, 3)
        self.assertTrue(all("published123" in call.args[1] for call in gh.reply.call_args_list))


class PublicationTests(unittest.TestCase):
    """Exercise execute's boundaries with an entirely mocked git/GitHub/model."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.ev = event()
        self.cfg = dict(CFG, workspace=str(self.state), check_command=["test-check"])
        self.gh = Mock(repo="owner/repo")
        self.gh.discussion.return_value = copy.deepcopy(self.ev["context"]["pr"]["discussion"])
        self.calls = []
        self.head = "old"
        self.dirty = False
        self.fail = None
        self.gh.api.side_effect = self.api

    def api(self, path, body=None):
        if "/issues/" in path:
            return {"state": "open", "labels": [{"name": "ai:managed"}]}
        pr = {"number": 2, "draft": True, "node_id": "PR2", "head": {"sha": self.head},
              "html_url": "https://github.com/owner/repo/pull/2"}
        return [pr] if "?state=open" in path else pr

    def command(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == "test-check":
            if self.fail == "check":
                raise RuntimeError("check failed")
            return "passed"
        self.assertEqual(args[0], "git")
        op = args[1]
        if op == "branch":
            return "ai/1"
        if op == "rev-parse":
            return self.head
        if op == "status":
            return " M file.py" if self.dirty else ""
        if op == "diff":
            return "file.py | 1 +" if "--stat" in args else ""
        if op == "-c":
            self.head, self.dirty = "new", False
        if op == "push" and self.fail == "push":
            raise RuntimeError("push failed")
        return ""

    def execute(self, decision, outcome):
        response = result(self.ev, decision, outcome)
        def model(workspace, ev, cfg, run_dir, guard):
            run_dir.mkdir(parents=True)
            return response
        with patch.object(observer, "prepare", return_value=("ai/1", "main")), \
             patch.object(observer, "run_codex", side_effect=model), \
             patch.object(observer, "command", side_effect=self.command):
            return observer.execute(self.ev, self.cfg, self.gh, {"items": []}, self.state)

    def test_questions_and_disagreement_never_commit_push_or_run_checks(self):
        for decision in ["answered", "disagreed"]:
            with self.subTest(decision=decision):
                self.calls.clear()
                self.execute(decision, "answer")
                self.assertFalse(any(c[0] == "test-check" or c[1] in {"push", "add", "-c"} for c in self.calls))
        self.assertFalse((self.state / "verified").exists())

    def test_failed_check_or_push_never_claims_fixed(self):
        for failure in ["check", "push"]:
            with self.subTest(failure=failure):
                self.calls.clear()
                self.gh.reply.reset_mock()
                self.head, self.dirty, self.fail = "old", True, failure
                with self.assertRaisesRegex(RuntimeError, f"{failure} failed"):
                    self.execute("fixed", "ready")
                self.gh.reply.assert_not_called()
                self.assertFalse((self.state / "verified").exists())
                if failure == "check":
                    self.assertFalse(any(c[0] == "git" and c[1] == "push" for c in self.calls))

    def test_all_fixes_checked_and_published_once_before_thread_replies(self):
        self.dirty = True
        self.execute("fixed", "ready")
        self.assertEqual(sum(c[0] == "test-check" for c in self.calls), 1)
        self.assertEqual(sum(c[1] == "push" for c in self.calls if c[0] == "git"), 1)
        self.assertEqual((self.state / "verified/2").read_text(), "new")
        self.assertEqual(self.gh.reply.call_count, 4)  # Three original remarks plus batch summary.
        for call in self.gh.reply.call_args_list[:-1]:
            self.assertIn("new", call.args[1])

    def test_stale_feedback_during_model_run_blocks_commit_push_and_replies(self):
        self.dirty = True
        self.gh.discussion.return_value[1]["body"] = "Changed while model worked"
        with self.assertRaisesRegex(RuntimeError, "feedback"):
            self.execute("fixed", "ready")
        self.assertFalse(any(c[0] == "git" and c[1] in {"push", "add", "-c"} for c in self.calls))
        self.gh.reply.assert_not_called()

    def test_changed_head_before_replies_blocks_answers(self):
        original = self.api
        def moved(path, body=None):
            response = original(path, body)
            if path.endswith("/pulls/2"):
                response["head"]["sha"] = "someone-elses-commit"
            return response
        self.gh.api.side_effect = moved
        with self.assertRaisesRegex(RuntimeError, "head changed"):
            self.execute("answered", "answer")
        self.gh.reply.assert_not_called()

    def test_fixed_requires_actual_edits(self):
        with self.assertRaisesRegex(RuntimeError, "without changing"):
            self.execute("fixed", "ready")
        self.gh.reply.assert_not_called()
        self.assertFalse(any(c[0] == "git" and c[1] == "push" for c in self.calls))

    def test_answer_that_changed_files_is_not_published(self):
        self.dirty = True
        with self.assertRaisesRegex(RuntimeError, "answer-only"):
            self.execute("answered", "answer")
        self.gh.reply.assert_not_called()
        self.assertFalse(any(c[0] == "git" and c[1] in {"push", "add", "-c"} for c in self.calls))

    def test_business_question_blocks_publication_even_with_local_edits(self):
        self.dirty = True
        with self.assertRaisesRegex(RuntimeError, "Batch complete"):
            self.execute("needs_input", "blocked")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.gh.reply.call_count, 3)
        self.assertFalse((self.state / "verified").exists())

    def test_feedback_edited_during_push_is_not_answered_as_fixed(self):
        self.dirty = True
        original = self.command
        def edit_during_push(args, **kwargs):
            output = original(args, **kwargs)
            if args[:2] == ["git", "push"]:
                self.gh.discussion.return_value[1]["body"] = "Reviewer changed the requirement during push"
            return output
        self.command = edit_during_push
        with self.assertRaisesRegex(RuntimeError, "feedback"):
            self.execute("fixed", "ready")
        self.gh.reply.assert_not_called()
        self.assertEqual(self.ev["head"], "new")  # The outer failure handler can now mark the job Blocked.

    def test_real_github_guard_blocks_changed_head(self):
        gh = observer.GitHub(CFG)
        def api(path):
            if "/issues/" in path:
                return {"state": "open", "labels": [{"name": "ai:managed"}]}
            return {"head": {"sha": "different", "repo": {"full_name": "owner/repo"}}}
        with patch.object(gh, "api", side_effect=api), self.assertRaisesRegex(RuntimeError, "head changed"):
            gh.guard(self.ev)


if __name__ == "__main__":
    unittest.main()
