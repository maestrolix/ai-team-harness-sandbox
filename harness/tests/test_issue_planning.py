import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai_team_harness import events, issues, results, workflow


class IssuePlanningTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.event = {
            "key": "issue:1:planning",
            "kind": "issue",
            "issue": 1,
            "item": "source",
            "context": {},
        }
        self.board = {
            "project": "P1",
            "field": {"options": [{"name": "Inbox", "id": "inbox"}]},
            "items": [
                {
                    "id": "source",
                    "fieldValueByName": {"name": "AI Open"},
                    "content": {"number": 1, "repository": {"nameWithOwner": "owner/repo"}},
                }
            ],
        }
        self.rows = []
        self.gh = Mock(repo="owner/repo")
        self.gh.pages.side_effect = lambda path: (
            [] if path.endswith("/comments") else list(self.rows)
        )
        self.gh.api.side_effect = self.api
        self.gh.graphql.side_effect = self.add_item
        self.gh.status.side_effect = self.status
        self.plan = self.make_plan("parent", "frontend", "backend", "qa")

    @staticmethod
    def make_plan(*roles):
        return [
            {
                "title": f"Task for {role}",
                "body": f"Scope and acceptance criteria for {role}",
                "role": role,
            }
            for role in roles
        ]

    def api(self, path, body=None):
        if body is None and path == "repos/owner/repo/issues/1":
            return {"state": "open", "labels": [{"name": "ai:managed"}]}
        if path == "repos/owner/repo/issues":
            number = 10 + len(self.rows)
            created = dict(
                body,
                number=number,
                node_id=str(number),
                state="open",
                labels=[],
                html_url=f"https://github.com/owner/repo/issues/{number}",
            )
            self.rows.append(created)
            return created
        number = int(path.split("/")[-2])
        self.assertEqual(path, f"repos/owner/repo/issues/{number}/labels")
        item = next(i for i in self.board["items"] if i["content"]["number"] == number)
        self.assertEqual(item["fieldValueByName"]["name"], "Inbox")
        row = next(i for i in self.rows if i["number"] == number)
        row["labels"].extend({"name": name} for name in body["labels"])

    def add_item(self, query, **variables):
        number = int(variables["c"])
        item_id = f"item-{number}"
        self.board["items"].append(
            {
                "id": item_id,
                "content": {"number": number, "repository": {"nameWithOwner": "owner/repo"}},
            }
        )
        return {"addProjectV2ItemById": {"item": {"id": item_id}}}

    def status(self, board, item_id, name):
        next(i for i in board["items"] if i["id"] == item_id)["fieldValueByName"] = {"name": name}

    def result(self):
        return {
            "outcome": "answer",
            "summary": "Requirements prepared",
            "reply": "Created tasks.",
            "checks": [],
            "deferred_issues": [],
            "planned_issues": copy.deepcopy(self.plan),
        }

    def publish(self):
        results.validate_result(self.event, self.result())
        return issues.publish_issues(
            self.event, self.plan, self.gh, self.board, self.state, lambda: None, kind="planned"
        )

    def test_default_plan_creates_four_linked_issues_in_inbox(self):
        urls = self.publish()
        self.assertEqual(len(urls), 4)
        for row, issue in zip(self.rows, self.plan, strict=True):
            self.assertEqual(row["title"], issue["title"])
            self.assertIn(issue["body"], row["body"])
            self.assertIn("Источник: #1", row["body"])
            self.assertNotIn("Причина переноса", row["body"])
            expected = {"ai:managed"}
            if issue["role"] != "parent":
                self.assertIn(f"Общая задача: {urls[0]}", row["body"])
                expected.add(f"role:{issue['role']}")
            self.assertEqual(events.labels(row), expected)
        self.assertEqual(
            [i["fieldValueByName"]["name"] for i in self.board["items"][1:]], ["Inbox"] * 4
        )

    def test_explicit_frontend_and_backend_create_only_two_issues(self):
        self.plan = self.make_plan("frontend", "backend")
        self.assertEqual(len(self.publish()), 2)
        self.assertFalse(any("Общая задача:" in row["body"] for row in self.rows))
        self.assertEqual(
            [events.labels(row) for row in self.rows],
            [{"ai:managed", "role:frontend"}, {"ai:managed", "role:backend"}],
        )

    def test_explicit_fullstack_creates_only_one_issue(self):
        self.plan = self.make_plan("fullstack")
        self.assertEqual(len(self.publish()), 1)
        self.assertEqual(events.labels(self.rows[0]), {"ai:managed", "role:fullstack"})
        self.assertNotIn("Общая задача:", self.rows[0]["body"])

    def test_partial_publication_recovers_parent_and_children_without_duplicates(self):
        def timeout_after_second_create(path, body=None):
            result = self.api(path, body)
            if path == "repos/owner/repo/issues" and len(self.rows) == 2:
                raise RuntimeError("i/o timeout")
            return result

        self.gh.api.side_effect = timeout_after_second_create
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            self.publish()
        self.gh.api.side_effect = self.api
        urls = self.publish()
        self.assertEqual(len(self.rows), 4)
        self.assertTrue(all(urls[0] in row["body"] for row in self.rows[1:]))
        self.gh.reset_mock()
        self.assertEqual(self.publish(), urls)
        self.gh.api.assert_not_called()
        self.gh.graphql.assert_not_called()
        self.gh.status.assert_not_called()

    def test_unknown_creation_is_not_replayed_and_plan_cannot_be_replaced(self):
        self.gh.api.side_effect = RuntimeError("i/o timeout")
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            self.publish()
        self.gh.api.side_effect = self.api
        with self.assertRaisesRegex(RuntimeError, "unknown outcome"):
            self.publish()
        self.plan = []
        with self.assertRaisesRegex(RuntimeError, "list changed"):
            self.publish()
        self.assertEqual(self.rows, [])

    def test_schema_and_validation_keep_plans_separate_from_implementation_and_feedback(self):
        self.assertIn("planned_issues", results.result_schema(self.event)["required"])
        self.assertNotIn(
            "planned_issues", results.result_schema({"kind": "feedback"})["properties"]
        )
        for changes in (
            {"outcome": "ready"},
            {"outcome": "blocked"},
            {"deferred_issues": [{"title": "Bug", "body": "Missing", "reason": "Independent"}]},
            {"planned_issues": "not a list"},
            {"planned_issues": [{"title": "Incomplete"}]},
            {"planned_issues": self.make_plan("analyst")},
            {"planned_issues": self.make_plan("frontend", "frontend")},
            {"planned_issues": self.make_plan("frontend", "parent")},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                results.validate_result(self.event, dict(self.result(), **changes))
        with self.assertRaises(ValueError):
            results.validate_result(dict(self.event, kind="feedback"), self.result())

    def execute(self, dirty=False):
        cfg = {"workspace": str(self.state), "check_command": ["unexpected-check"]}

        def command(args, **kwargs):
            if args == ["git", "branch", "--show-current"]:
                return "ai/1"
            if args == ["git", "status", "--porcelain"]:
                return " M file.py" if dirty else ""
            self.assertEqual(args, ["git", "diff", "origin/main...HEAD", "--stat"])
            return ""

        with (
            patch.object(workflow, "run_codex", return_value=self.result()),
            patch.object(workflow, "command", side_effect=command),
        ):
            return workflow.execute(
                self.event, cfg, self.gh, self.board, self.state, prepared=("ai/1", "main")
            )

    def test_execution_publishes_tasks_and_finishes_without_code_checks_commit_push_or_pr(self):
        result = self.execute()
        self.assertEqual(len(self.rows), 4)
        self.assertIn("реализация не запускалась", result["reply"])
        self.gh.reply.assert_called_once_with(
            dict(self.event, context={"discussion": []}), result["reply"]
        )
        self.assertEqual(self.board["items"][0]["fieldValueByName"]["name"], "Review")
        self.assertFalse((self.state / "verified").exists())

    def test_unrequested_code_changes_block_issue_publication(self):
        with self.assertRaisesRegex(RuntimeError, "answer-only"):
            self.execute(dirty=True)
        self.assertEqual(self.rows, [])
        self.gh.reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
