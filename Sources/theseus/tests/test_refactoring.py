"""Exercise confirmed refactoring in disposable real Git worktrees."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex import RoleResult
from execution import run_execution, verify_completed_execution
from notes import read_section
from planning import ACCEPTANCE, ARCHITECTURE, run_planning
from refactoring import refactoring_execution_base, run_refactoring_planning
from routing import Decision
from scope import capture
from verification import report_updates, run_verification


class RefactoringTests(unittest.TestCase):
    note = "Sources/block.md"
    plan = "Plans/rebuild.md"
    current_code = "Sources/block/replacement.py"
    discussions = "\n#KOL-12🍏 Keep thread\n\n*◇ Existing answer*\n#KOL-12🛑\n"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name).resolve()
        self.git(self.vault, "init", "-q")
        self.git(self.vault, "config", "user.name", "Refactoring Test")
        self.git(self.vault, "config", "user.email", "test@example.invalid")
        self.note_text = (
            "## User requirements\n\nPublic API is stable.\n\n"
            "## Specification\n\n### Old\n\nLegacy architecture.\n"
            + self.discussions
            + "\n## Working context\n\nLegacy context.\n\n"
            + "## Other\n\nKeep neighboring notes.\n"
        )
        self.fixtures = {
            self.note: self.note_text,
            "Sources/block/old.py": "OLD = True\n",
            "Sources/block/helpers/tests/test_old.py": "assert False\n",
            "Sources/block/child.py": "CONTRACT = 'immutable'\n",
            "Sources/block/child.py.md": "## User requirements\n\nPreserve independent API.\n",
            "Sources/block/pending.md": "---\nTemplate: Building block\n---\nKeep contract.\n",
            "Sources/block/folder.md": "---\nTemplate: Building block\n---\nKeep folder contract.\n",
            "Sources/block/folder/value.py": "VALUE = 2\n",
            "Plans/previous.md": "Archived plan\n",
            "Plans/one/two/old.md": "Another archived plan\n",
            self.plan: "Former plan at same path\n",
            ".gitignore": ".worktrees/\n*.ignored\n",
            "outside.txt": "snapshot only",
        }
        for relative, contents in self.fixtures.items():
            self.write(self.vault, relative, contents)
        self.git(self.vault, "add", ".")
        self.git(self.vault, "commit", "-qm", "Snapshot A")
        self.snapshot = self.git(self.vault, "rev-parse", "HEAD")
        self.write(self.vault, "outside.txt", "main commit")
        self.git(self.vault, "add", "outside.txt")
        self.git(self.vault, "commit", "-qm", "New main commit")
        self.write(self.vault, self.note, self.note_text.replace("Public API", "Current API"))
        self.write(self.vault, "outside.txt", "staged main change")
        self.git(self.vault, "add", "outside.txt")
        self.write(self.vault, "outside.txt", "unstaged main change")
        self.main_head = self.git(self.vault, "rev-parse", "HEAD")
        self.index = (self.vault / ".git/index").read_bytes()
        self.calls = 0

    @staticmethod
    def git(root, *args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    @staticmethod
    def write(root, path, contents):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)

    def plan_work(self, **options):
        arguments = dict(requested=True, confirmed=True, ticket_id="KOL-13", slug="refactor",
                         ticket="Please refactor and clear the isolated workspace.", plan_path=self.plan,
                         snapshot_a=self.snapshot, files=(self.current_code,), runner=self.planner, settings={})
        arguments.update(options)
        decision = arguments.pop("decision", Decision("prepare_refactoring", self.note, "refactoring"))
        return run_refactoring_planning(self.vault, decision, **arguments)

    def planner(self, role, settings, *, cwd, context, **options):
        self.calls += 1
        self.assertEqual(role, "planner")
        self.assertTrue(options["read_only"])
        self.assertIn("refactoring", json.loads(context))
        self.assertEqual(self.git(cwd, "show", "-s", "--format=%s", "HEAD"), "Prepare Theseus refactoring cleanup")
        self.assertFalse((cwd / "Sources/block/old.py").exists())
        self.assertFalse((cwd / "Sources/block/helpers/tests/test_old.py").exists())
        self.assertFalse((cwd / "Plans").exists())
        for relative in ("Sources/block/child.py", "Sources/block/child.py.md", "Sources/block/folder.md",
                         "Sources/block/folder/value.py", "Sources/block/pending.md"):
            self.assertEqual((cwd / relative).read_text(), self.fixtures[relative])
        self.assertEqual((cwd / "outside.txt").read_text(), "snapshot only")
        self.assertEqual((cwd / self.note).read_text(),
                         "## User requirements\n\nCurrent API is stable.\n\n## Specification\n"
                         + self.discussions.strip("\n") + "\n## Working context\n"
                         + "## Other\n\nKeep neighboring notes.\n")
        return RoleResult("success", json.dumps({
            "goal": "Provide equivalent API.",
            "specification": [
                {"note": self.note, "section": ARCHITECTURE, "text": "Preserve the child contract."},
                {"note": self.note, "section": ACCEPTANCE, "text": f"python3 {self.current_code}"},
            ],
            "tasks": [{
                "description": "Implement equivalent API.",
                "files": [self.current_code],
                "coder": "coder_light", "complexity": "specified", "depends_on": [],
                "checks": [{"command": f"python3 {self.current_code}", "cwd": ".",
                            "setup": "None", "expected": "Pass"}],
            }],
            "external_dependencies": [],
        }))

    def assert_vault_preserved(self):
        self.assertEqual(self.git(self.vault, "rev-parse", "HEAD"), self.main_head)
        self.assertEqual((self.vault / ".git/index").read_bytes(), self.index)
        self.assertEqual((self.vault / self.note).read_text(), self.note_text.replace("Public API", "Current API"))
        self.assertEqual((self.vault / "outside.txt").read_text(), "unstaged main change")
        for name in ("Plans/previous.md", self.plan, "Sources/block/old.py"):
            self.assertEqual((self.vault / name).read_text(), self.fixtures[name])

    def test_explicit_request_and_confirmation_required_before_worktree(self):
        for request, confirm in ((False, False), (False, True), (True, False), ("yes", True)):
            with self.subTest(request=request, confirm=confirm), self.assertRaises(ValueError):
                self.plan_work(requested=request, confirmed=confirm)
        self.assertFalse((self.vault / ".worktrees").exists())
        self.assertEqual(self.calls, 0)
        self.assert_vault_preserved()
        with self.assertRaisesRegex(ValueError, "confirmed refactoring helper"):
            run_planning(self.vault, Decision("plan", self.note, "refactoring"), ticket="bypass")

    def test_separate_cleanup_and_plan_leave_main_vault_and_contracts_intact(self):
        prepared = self.plan_work()
        root = prepared.worktree.path
        self.assertEqual(self.git(root, "rev-parse", "HEAD"), prepared.execution_base)
        self.assertEqual(self.git(root, "rev-parse", "HEAD^"), prepared.cleanup)
        self.assertEqual(self.git(root, "rev-parse", "HEAD^^"), prepared.worktree.base)
        self.assertEqual(self.git(root, "rev-parse", "HEAD^^^"), self.snapshot)
        self.assertEqual(self.git(root, "diff", "--name-only", prepared.cleanup, prepared.execution_base).splitlines(),
                         [self.plan, self.note])
        self.assertIn("Plan type: Refactoring", (root / self.plan).read_text())
        self.assertEqual(read_section((root / self.note).read_bytes(), ACCEPTANCE, root_level=2).strip(),
                         f"*python3 {self.current_code}*".encode())
        self.assertEqual(refactoring_execution_base(prepared.worktree, self.note, self.plan), prepared.execution_base)
        self.assertFalse((root / "Plans/previous.md").exists())
        self.assert_vault_preserved()
        again = self.plan_work(snapshot_a=None)
        self.assertEqual(again.worktree, prepared.worktree)
        self.assertEqual(again.execution_base, prepared.execution_base)
        self.assertEqual(again.planning.tasks, prepared.planning.tasks)
        self.assertEqual(self.calls, 1)
        with self.assertRaisesRegex(ValueError, "another block, plan"):
            self.plan_work(plan_path="Plans/another.md")

    def test_resume_after_planner_requests_decision(self):
        def question(*args, **kwargs):
            self.planner(*args, **kwargs)
            return RoleResult("error", "Which API version must be supported?")

        paused = self.plan_work(runner=question)
        self.assertEqual(paused.planning.stage, "await_decision")
        self.assertIsNone(paused.execution_base)
        root = paused.worktree.path
        self.assertEqual(self.git(root, "rev-parse", "HEAD"), paused.cleanup)
        resumed = self.plan_work(snapshot_a=None)
        self.assertEqual(resumed.worktree, paused.worktree)
        self.assertEqual(resumed.cleanup, paused.cleanup)
        self.assertEqual(self.calls, 2)
        self.assert_vault_preserved()

    def test_resume_saved_plan_after_interrupted_publication_without_rerunning_planner(self):
        from refactoring import _publish

        def interrupted(root, parent, commit):
            if self.calls:
                raise OSError("interrupted publication")
            _publish(root, parent, commit)

        with patch("refactoring._publish", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "interrupted publication"):
                self.plan_work()
        finished = self.plan_work(snapshot_a=None)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.git(finished.worktree.path, "rev-parse", "HEAD"), finished.execution_base)

    def test_execution_reconciles_from_the_verified_plan_commit(self):
        prepared = self.plan_work()

        def coder(role, settings, *, cwd, context, checks, **options):
            brief = json.loads(context)
            self.write(cwd, self.current_code, "print('Pass')\n")
            self.git(cwd, "add", "--", self.current_code)
            evidence = {"red": {"command": checks[0], "cwd": ".", "exit": 1, "observed": "missing script"},
                        "green": [{"command": checks[0], "cwd": ".", "exit": 0, "observed": "Pass"}]}
            self.git(cwd, "commit", "-qm", brief["commit_subject"],
                     "-m", "Theseus-evidence: " + json.dumps(evidence))
            return RoleResult("success", "Implemented")

        result = run_execution(self.vault, ticket_id="KOL-13", slug="refactor",
                               block=self.note, plan=self.plan, work="refactoring",
                               settings={}, runner=coder, review=lambda candidate: True)
        self.assertEqual(result.stage, "complete")
        root = prepared.worktree.path
        live = capture(root)
        self.assertEqual(len(verify_completed_execution(prepared.worktree, live, self.note,
                                                        self.plan, "refactoring")), 1)

        def reviewer(role, settings, *, context, **options):
            brief = json.loads(context)
            self.assertEqual(brief["work"], "refactoring")
            self.assertEqual(brief["current_plan"], self.plan)
            archived = self.git(root, "ls-tree", "-r", "--name-only", prepared.worktree.base, "--", "Plans/")
            excluded = [path for path in archived.splitlines() if path != self.plan]
            self.assertEqual(brief["checkout_only_plan_deletions"], excluded)
            for path in excluded:
                self.assertIn(f"diff --git a/{path} b/{path}", brief["diff"])
            report = {key: brief[key] for key in ("base", "head", "diff_sha256", "verification_sha256")}
            return RoleResult("success", json.dumps(dict(report, findings=[], dispositions=[])))

        checks = {self.note: {"acceptance": [{"command": f"python3 {self.current_code}", "cwd": "."}],
                              "tests": [], "mandatory": []}}
        verified = run_verification(self.vault, ticket_id="KOL-13", slug="refactor", block=self.note,
                                    plan=self.plan, work="refactoring", accepted_head=self.git(root, "rev-parse", "HEAD"),
                                    checks=checks, settings={}, runner=reviewer)
        self.assertEqual(verified["status"], "verified", verified["problems"])
        self.assertEqual(verified["version"]["base"], prepared.worktree.base)
        report = report_updates(live, verified, block=self.note, work="refactoring", plan=self.plan)
        self.assertIn(self.current_code.encode(),
                      read_section(report[self.note].data, ("Working context", "Files"), root_level=2))
        self.assertIn(b"No verification blockers",
                      read_section(report[self.note].data, ("Working context", "Problems"), root_level=2))
        self.assertIn(self.discussions.strip().encode(), report[self.note].data)
        self.assertEqual(capture(root), live)
        marker = Path(self.git(root, "rev-parse", "--absolute-git-dir")) / "theseus-refactoring.json"
        record = json.loads(marker.read_text())
        record["execution_base"] = self.git(root, "rev-parse", "HEAD")
        marker.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "single-parent history"):
            verify_completed_execution(prepared.worktree, capture(root), self.note, self.plan, "refactoring")
        self.assert_vault_preserved()


if __name__ == "__main__":
    unittest.main()
