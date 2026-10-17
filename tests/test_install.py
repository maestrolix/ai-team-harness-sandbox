import contextlib
import io
from pathlib import Path
import tempfile
import tomllib
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / "install.sh").read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
installer = types.ModuleType("installer")
exec(compile(source, str(ROOT / "install.sh"), "exec"), installer.__dict__)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        paths = ["AGENTS.md", "harness/observer.py"] + [f".codex/agents/{r}.toml" for r in installer.ROLES]
        self.files = {p: (ROOT / p).read_text() for p in paths}
        self.cfg = dict(repository="owner/app", project_owner="team", project_owner_kind="organization",
                        project_number=8, trusted_users=["developer"], workspace=".", state_dir=".state",
                        check_command=["make", "check"], required_checks=["build"])

    def test_origin_and_project_accept_expected_forms(self):
        for origin in ("git@github.com:owner/app.git", "https://github.com/owner/app.git",
                       "ssh://git@github.com/owner/app.git", "https://github.com/owner/app"):
            self.assertEqual(installer.repository_from_origin(origin), "owner/app")
        self.assertEqual(installer.project_from_url("https://github.com/orgs/team/projects/8/views/1"),
                         ("organization", "team", 8))
        self.assertEqual(installer.project_from_url("https://github.com/users/person/projects/2"),
                         ("user", "person", 2))
        for origin in ("https://github.com.evil/owner/app", "/tmp/repo", "git@gitlab.com:owner/app.git"):
            with self.assertRaises(ValueError):
                installer.repository_from_origin(origin)

    def test_install_preserves_instructions_and_does_not_import_skeleton_checks(self):
        original = "# Наш проект\nСохрани правила продукта.\n"
        (self.root / "AGENTS.md").write_text(original)
        (self.root / "README.md").write_text("Описание приложения")
        planned = installer.prepare_files(self.root, self.files, self.cfg)
        self.assertTrue(planned["AGENTS.md"].startswith(original))
        self.assertNotIn("Разрабатываемого бизнес-приложения пока нет", planned["AGENTS.md"])
        self.assertNotIn("unittest discover", planned["AGENTS.md"])
        self.assertIn("QA — уровень рассуждений `low`", planned["AGENTS.md"])
        self.assertNotIn("README.md", planned)
        self.assertFalse(any(p.startswith(("tests/", ".github/")) for p in planned))
        self.assertEqual(tomllib.loads(planned["harness.toml"]), self.cfg)

    def test_repeat_preserves_local_config_and_does_not_duplicate_blocks(self):
        planned = installer.prepare_files(self.root, self.files, self.cfg)
        for name, text in planned.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        repeated = installer.prepare_files(self.root, self.files, dict(self.cfg, check_command=[]))
        self.assertEqual(planned, repeated)
        self.assertEqual(repeated["AGENTS.md"].count(installer.START), 1)
        with self.assertRaisesRegex(ValueError, "другой репозиторий"):
            installer.prepare_files(self.root, self.files, dict(self.cfg, repository="other/app"))

    def test_conflicting_role_is_not_overwritten(self):
        path = self.root / ".codex/agents/backend.toml"
        path.parent.mkdir(parents=True)
        path.write_text("custom role")
        with self.assertRaisesRegex(ValueError, "существует и отличается"):
            installer.prepare_files(self.root, self.files, self.cfg)
        self.assertEqual(path.read_text(), "custom role")
        self.assertFalse((self.root / "harness.toml").exists())

    def test_symlink_parent_and_override_stop_installation(self):
        (self.root / ".codex").symlink_to(self.root / "outside", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            installer.prepare_files(self.root, self.files, self.cfg)
        (self.root / ".codex").unlink()
        (self.root / "AGENTS.override.md").write_text("override")
        with self.assertRaisesRegex(ValueError, "приоритет"):
            installer.prepare_files(self.root, self.files, self.cfg)

    def test_edited_agent_block_requires_manual_merge(self):
        text = installer.appended_block("# Local", "rules")
        with self.assertRaisesRegex(ValueError, "отличается"):
            installer.appended_block(text, "different rules")

    def test_existing_status_ids_and_custom_labels_are_preserved(self):
        original = {"id": "todo-id", "name": "Todo", "color": "BLUE", "description": "Keep me"}
        project = {"id": "project", "fields": {"nodes": [
            {"id": "status", "name": "Status", "options": [original]}]}}
        with patch.object(installer, "graphql") as graphql, patch.object(installer, "api") as api:
            api.return_value = [{"name": "ai:managed"}]
            installer.configure_github("owner/app", "repo-id", project)
        options = graphql.call_args_list[0].kwargs["input"]["singleSelectOptions"]
        self.assertEqual(options[0], original)
        self.assertEqual([v["name"] for v in options[1:]], list(installer.STATUSES))
        self.assertEqual(graphql.call_args_list[1].kwargs, {"p": "project", "r": "repo-id"})
        created = [call.args[1]["name"] for call in api.call_args_list if len(call.args) == 2]
        self.assertNotIn("ai:managed", created)
        self.assertIn("ai:paused", created)

    def test_existing_statuses_and_labels_need_no_recreation(self):
        project = {"id": "project", "fields": {"nodes": [
            {"id": "status", "name": "Status", "options": [{"name": n} for n in installer.STATUSES]}]}}
        labels = ["ai:managed", "ai:paused"] + [f"role:{r}" for r in installer.ROLES]
        with patch.object(installer, "graphql") as graphql, patch.object(installer, "api") as api:
            api.return_value = [{"name": n} for n in labels]
            installer.configure_github("owner/app", "repo-id", project)
        self.assertEqual(graphql.call_count, 1)  # Idempotent repository link only.
        self.assertEqual(api.call_count, 1)  # Read labels only.

    def invoke_main(self, remote_setup):
        def run(args, **kwargs):
            if args[:2] == ["git", "rev-parse"]:
                return str(self.root)
            if args[:2] == ["git", "remote"]:
                return "git@github.com:owner/app.git"
            return ""

        def api(path):
            return {"login": "developer"} if path == "user" else {
                "full_name": "owner/app", "node_id": "repo", "has_issues": True, "permissions": {"push": True}}

        project = {"organization": {"projectV2": {"id": "project", "closed": False, "viewerCanUpdate": True}}}
        args = ["install.sh", "--project", "https://github.com/orgs/team/projects/8",
                "--check-command", "make check", "--required-check", "build"]
        with patch.object(installer.sys, "argv", args), patch.object(installer.shutil, "which", return_value="tool"), \
             patch.object(installer, "run", side_effect=run), patch.object(installer, "api", side_effect=api), \
             patch.object(installer, "graphql", return_value=project), \
             patch.object(installer, "download", return_value=("abc123", self.files)), \
             patch.object(installer, "configure_github", remote_setup), contextlib.redirect_stdout(io.StringIO()):
            installer.main()

    def test_main_installs_to_repository_root_and_can_repeat(self):
        from unittest.mock import Mock
        remote = Mock()
        self.invoke_main(remote)
        self.invoke_main(remote)
        cfg = tomllib.loads((self.root / "harness.toml").read_text())
        self.assertEqual(cfg["repository"], "owner/app")
        self.assertEqual(cfg["project_owner"], "team")
        self.assertEqual(cfg["trusted_users"], ["developer"])
        self.assertEqual(cfg["check_command"], ["make", "check"])
        self.assertTrue((self.root / "harness/observer.py").exists())
        self.assertEqual((self.root / "AGENTS.md").read_text().count(installer.START), 1)
        self.assertEqual(remote.call_count, 2)

    def test_local_conflict_is_detected_before_remote_writes(self):
        from unittest.mock import Mock
        (self.root / "harness").mkdir()
        (self.root / "harness/observer.py").write_text("my observer")
        remote = Mock()
        with self.assertRaises(ValueError):
            self.invoke_main(remote)
        remote.assert_not_called()

    def test_remote_failure_does_not_leave_partial_local_installation(self):
        from unittest.mock import Mock
        with self.assertRaises(RuntimeError):
            self.invoke_main(Mock(side_effect=RuntimeError("no project access")))
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
