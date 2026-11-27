"""Exercise execution and interruption with real, disposable Git worktrees."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex import RoleExecutionError, RoleResult
from planning import Check, Task, render_plan
from worktree import prepare_worktree


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.main = Path(self.temp.name).resolve()
        self.git(self.main, "init", "-q")
        self.git(self.main, "config", "user.name", "Theseus test")
        self.git(self.main, "config", "user.email", "test@example.invalid")
        self.note = "Sources/block.md"
        self.plan = "Plans/plan.md"
        self.files = ("Sources/block/code.py", "Sources/block/check.py")
        for folder in ("Sources", "Plans"):
            (self.main / folder).mkdir()
        (self.main / self.note).write_text(
            "## User requirements\n\nKeep this.\n\n## Specification\n\n"
            "### Architecture and techniques\n\n*Use Python.*\n\n"
            "### Acceptance\n\n*python3 Sources/block/check.py*\n\n"
            "## Working context\n\n*Initial context.*\n"
        )
        check = Check("python3 Sources/block/check.py", ".", "No dependencies", "Pass")
        second = Check("python3 Sources/block/check2.py && printf verified", ".", "No dependencies", "Pass")
        self.tasks = (
            Task("Implement print value", self.files, "coder_light", "specified", (), (check,)),
            Task("Add further check", ("Sources/block/check2.py",), "coder", "contract", (1,), (second,)),
        )
        (self.main / self.plan).write_bytes(render_plan(self.note, "first", "Run checks", self.tasks, {}, ()))
        (self.main / "unrelated.txt").write_text("original")
        self.git(self.main, "add", ".")
        self.git(self.main, "commit", "-qm", "base")
        self.main_head = self.git(self.main, "rev-parse", "HEAD").strip()
        self.worktree = prepare_worktree(self.main, ticket_id="KOL-102", slug="execution",
                                         snapshot_a=self.main_head, inputs=(self.note, self.plan))
        self.calls = []

    def git(self, root, *args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                              text=True).stdout

    def run_execution(self, runner, **kwargs):
        from execution import run_execution

        return run_execution(self.main, ticket_id="KOL-102", slug="execution", block=self.note,
                             plan=self.plan, work="first", settings={}, runner=runner,
                             **kwargs)

    def coder(self, role, settings, *, cwd, context, allowed_paths, allowed_sections, checks, task,
              poison=None, interrupt=False):
        brief = json.loads(context)
        number = brief["number"]
        self.calls.append((number, role))
        self.assertEqual(cwd, self.worktree.path)
        self.assertEqual(allowed_paths, list(self.tasks[number - 1].files))
        self.assertEqual(allowed_sections, {})
        self.assertEqual(brief["checks"], [vars(c) for c in self.tasks[number - 1].checks])
        check_path = cwd / allowed_paths[-1]
        check_path.parent.mkdir(exist_ok=True)
        check_path.write_text("from code import VALUE\nassert VALUE == %d\n" % number)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        command = checks[0]
        red = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, env=env)
        self.assertNotEqual(red.returncode, 0)
        if number == 1:
            (cwd / allowed_paths[0]).write_text("VALUE = 1\n")
        else:
            (cwd / allowed_paths[-1]).write_text("from code import VALUE\nassert VALUE != 2\n")
        green = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, env=env)
        self.assertEqual(green.returncode, 0)
        self.git(cwd, "add", "--", *allowed_paths)
        if poison == "commit":
            (cwd / "unrelated.txt").write_text("changed")
            self.git(cwd, "add", "unrelated.txt")
        evidence = {"red": {"command": checks[0], "cwd": ".", "exit": red.returncode,
                            "observed": red.stderr.strip()},
                    "green": [{"command": checks[0], "cwd": ".", "exit": 0, "observed": "Pass"}]}
        self.git(cwd, "commit", "-qm", brief["commit_subject"],
                 "-m", "Theseus-evidence: " + json.dumps(evidence))
        if poison == "staged":
            (cwd / "unrelated.txt").write_text("changed")
            self.git(cwd, "add", "unrelated.txt")
        if poison == "untracked":
            (cwd / "untracked.txt").write_text("changed")
        if interrupt:
            raise RoleExecutionError("interrupted after commit")
        return RoleResult("success", "Commit ready.")

    def test_sequential_tasks_use_role_and_commit_progress_separately(self):
        heads = []

        def approve(candidate):
            heads.append(candidate.commit)
            return True

        result = self.run_execution(self.coder, review=approve)
        self.assertEqual(result.stage, "complete")
        self.assertEqual(self.calls, [(1, "coder_light"), (2, "coder")])
        self.assertEqual(self.git(self.worktree.path, "show", "--format=", "--name-only", heads[0]).split(),
                         sorted(self.files))
        self.assertEqual(self.git(self.worktree.path, "show", "--format=", "--name-only", "HEAD").split(),
                         [self.plan, self.note])
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD").strip(), self.main_head)
        self.assertEqual(self.git(self.main, "diff", "--cached", "--name-only"), "")
        self.assertIn("*Done", (self.worktree.path / self.plan).read_text())
        self.run_execution(self.coder, review=approve)
        self.assertEqual(self.calls, [(1, "coder_light"), (2, "coder")])
        self.assertEqual(len(heads), 2)

        (self.worktree.path / "unrelated.txt").write_text("clean foreign commit")
        self.git(self.worktree.path, "commit", "-qam", "Foreign commit after completion")
        with self.assertRaisesRegex(ValueError, "unrecorded worktree HEAD"):
            self.run_execution(self.coder, review=approve)
        self.assertEqual(self.calls, [(1, "coder_light"), (2, "coder")])

    def test_installed_coder_helper_records_and_commits_observed_checks(self):
        source = Path(__file__).resolve().parents[1]
        revision = json.loads((source / "upstream.lock").read_text())["commit"]
        archive = source.parents[1] / "References" / f"superpowers-{revision}.tar.gz"
        bundle = self.main / ".git" / "installed"
        subprocess.run([sys.executable, str(source / "build.py"), "--archive", str(archive),
                        "--output", str(bundle)], check=True, capture_output=True)
        helper = bundle / "skills" / "using-theseus" / "scripts" / "execution.py"

        def coder(role, settings, *, cwd, context, allowed_paths, **kwargs):
            brief = json.loads(context)
            number = brief["number"]
            self.calls.append(number)

            def call(key, *, expected=0):
                argv = shlex.split(brief[key])
                result = subprocess.run([sys.executable, str(helper), *argv[2:]], cwd=cwd,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stderr or result.stdout)
                return json.loads(result.stdout)

            check = cwd / allowed_paths[-1]
            check.parent.mkdir(exist_ok=True)
            check.write_text(f"from code import VALUE\nassert VALUE == {number}\n")
            self.assertIn("failing red check", call("commit_command", expected=1)["error"])
            red = call("red_command")["red"]
            self.assertEqual(red["exit"], 1)
            self.assertIn("Error", red["observed"])
            self.assertIn("check failed", call("commit_command", expected=1)["error"])
            self.assertEqual(self.git(cwd, "diff", "--cached", "--name-only"), "")
            if number == 1:
                (cwd / allowed_paths[0]).write_text("VALUE = 1\n")
            else:
                check.write_text("from code import VALUE\nassert VALUE == 1\n")
            unrelated = cwd / "unrelated.txt"
            unrelated.write_text("outside scope")
            self.assertIn("unrelated worktree files", call("commit_command", expected=1)["error"])
            unrelated.write_text("original")
            result = call("commit_command")
            self.assertEqual(result["producer"], "execution.py")
            self.assertEqual(result["red"], red)
            self.assertEqual(result["green"][0]["observed"], "Exit 0, no output" if number == 1 else "verified")
            return RoleResult("success", "Helper created the commit.")

        candidates = []

        def approve(candidate):
            self.assertEqual(candidate.producer, "execution.py")
            candidates.append(candidate.commit)
            return True

        self.assertEqual(self.run_execution(coder, review=approve).stage, "complete")
        self.assertEqual(self.run_execution(coder, review=approve).stage, "complete")
        self.assertEqual(self.calls, [1, 2])
        self.assertEqual(len(candidates), 2)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD").strip(), self.main_head)
        self.assertEqual(self.git(self.main, "diff", "--cached", "--name-only"), "")

        from execution import _candidate
        from scope import capture
        from worktree import _git

        root = self.worktree.path
        index = self.main / ".git" / "test-coder-evidence-index"
        _git(root, "read-tree", candidates[0], index=index)
        blob = _git(root, "hash-object", "-w", "--stdin", data=b"VALUE = 99\n").stdout.decode().strip()
        _git(root, "update-index", "--cacheinfo", "100644", blob, self.files[0], index=index)
        tree = _git(root, "write-tree", index=index).stdout.strip()
        dispatch = self.git(root, "rev-parse", candidates[0] + "^").strip()
        message = self.git(root, "show", "-s", "--format=%B", candidates[0]).strip()
        copied_evidence = self.git(root, "commit-tree", tree, "-p", dispatch, "-m", message).strip()
        with self.assertRaisesRegex(ValueError, "does not match the execution helper journal"):
            _candidate(root, copied_evidence, 1, self.tasks[0], capture(root), self.note, "first")

    def test_resume_recovers_progress_commit_failure(self):
        import execution

        real_git = execution._git

        def fail_progress_commit(root, *args, **kwargs):
            if "--only" in args and any(str(arg).startswith("Accept Theseus task 1") for arg in args):
                raise subprocess.CalledProcessError(1, "git", stderr="interrupted progress commit")
            return real_git(root, *args, **kwargs)

        with patch.object(execution, "_git", side_effect=fail_progress_commit):
            with self.assertRaisesRegex(subprocess.CalledProcessError, "non-zero exit"):
                self.run_execution(self.coder, review=lambda candidate: True)
        result = self.run_execution(self.coder)
        self.assertEqual(result.stage, "await_review")
        self.assertEqual(result.candidate.number, 2)
        self.assertEqual(self.calls, [(1, "coder_light"), (2, "coder")])

    def test_rejects_amended_dispatch_with_unrelated_file(self):
        def stop(*args, **kwargs):
            raise RoleExecutionError("interrupted before commit")

        with self.assertRaises(RoleExecutionError):
            self.run_execution(stop)
        root = self.worktree.path
        (root / "unrelated.txt").write_text("foreign")
        self.git(root, "commit", "--amend", "-qa", "--no-edit")
        with self.assertRaisesRegex(ValueError, "progress commit changed files outside"):
            self.run_execution(self.coder)
        self.assertEqual(self.calls, [])

    def test_rejects_progress_commit_rewriting_a_future_task(self):
        def stop(*args, **kwargs):
            raise RoleExecutionError("interrupted before commit")

        with self.assertRaises(RoleExecutionError):
            self.run_execution(stop)
        root = self.worktree.path
        plan = root / self.plan
        original = plan.read_bytes()
        modified = original.replace(b"Add further check", b"Rewrite a different task")
        self.assertNotEqual(original, modified)
        plan.write_bytes(modified)
        self.git(root, "commit", "--amend", "-qa", "--no-edit")
        with self.assertRaisesRegex(ValueError, "progress commit changed task definitions"):
            self.run_execution(self.coder)
        self.assertEqual(self.calls, [])

    def test_resume_after_real_coder_commit_without_redispatch(self):
        with self.assertRaisesRegex(RoleExecutionError, "interrupted"):
            self.run_execution(lambda *a, **kw: self.coder(*a, **kw, interrupt=True))
        result = self.run_execution(self.coder)
        self.assertEqual(result.stage, "await_review")
        self.assertEqual(self.calls, [(1, "coder_light")])
        self.assertEqual(result.candidate.commit, self.git(self.worktree.path, "rev-parse", "HEAD").strip())

    def test_role_switch_resumes_first_unfinished_task(self):
        def stop_before_second_commit(role, settings, **kwargs):
            number = json.loads(kwargs["context"])["number"]
            if number == 2:
                self.calls.append((number, role))
                raise RoleExecutionError("interrupted before commit")
            return self.coder(role, settings, **kwargs)

        with self.assertRaisesRegex(RoleExecutionError, "interrupted"):
            self.run_execution(stop_before_second_commit, review=lambda candidate: True)
        result = self.run_execution(self.coder, review=lambda candidate: True,
                                    coder_overrides={2: "coder_heavy"})
        self.assertEqual(result.stage, "complete")
        self.assertEqual(self.calls, [(1, "coder_light"), (2, "coder"), (2, "coder_heavy")])

    def test_rejects_unrecorded_head_before_coder_dispatch(self):
        root = self.worktree.path
        (root / "unrelated.txt").write_text("foreign")
        self.git(root, "commit", "-qam", "Foreign commit")
        with self.assertRaisesRegex(ValueError, "unrecorded worktree HEAD"):
            self.run_execution(self.coder)
        self.assertEqual(self.calls, [])
        self.assertEqual((root / "unrelated.txt").read_text(), "foreign")

    def test_coder_commit_cannot_create_a_descendant_block_note(self):
        from execution import _commit_states
        from scope import authorize, capture

        root = self.worktree.path
        parent = self.git(root, "rev-parse", "HEAD").strip()
        path = "Sources/block/child.note.md"
        (root / path).parent.mkdir()
        (root / path).write_text("---\nTemplate: Building block\n---\n## User requirements\n")
        self.git(root, "add", "--", path)
        self.git(root, "commit", "-qm", "Add descendant block note")
        commit = self.git(root, "rev-parse", "HEAD").strip()
        before, after = _commit_states(root, parent, commit, capture(root))
        with self.assertRaisesRegex(ValueError, "not writable"):
            authorize(before, after, self.note, role="coder", stage="execute", work="first", files=(path,))

    def test_rejects_foreign_commit_index_or_worktree_changes(self):
        for poison, problem in (("commit", "not writable"), ("staged", "index"), ("untracked", "untracked")):
            with self.subTest(poison=poison):
                if poison != "commit":
                    self.tearDown()
                    self.setUp()
                with self.assertRaisesRegex(ValueError, problem):
                    self.run_execution(lambda *a, **kw: self.coder(*a, **kw, poison=poison))
                self.assertIn("*Running", (self.worktree.path / self.plan).read_text())
                if poison == "untracked":
                    self.assertEqual((self.worktree.path / "untracked.txt").read_text(), "changed")
                else:
                    self.assertEqual((self.worktree.path / "unrelated.txt").read_text(), "changed")


if __name__ == "__main__":
    unittest.main()
