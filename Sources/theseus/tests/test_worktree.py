import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

from worktree import _git, owned_checkout, prepare_worktree, verify_worktree


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.name", "Worktree tests")
        self.git("config", "user.email", "worktree@example.test")
        self.write("Sources/demo.md", "snapshot specification")
        self.write("Plans/demo.md", "snapshot plan")
        self.write("snapshot.txt", "snapshot-only")
        self.git("add", ".")
        self.git("commit", "-qm", "Snapshot A")
        self.snapshot = self.git("rev-parse", "HEAD")
        self.write("snapshot.txt", "newer commit")
        self.git("add", ".")
        self.git("commit", "-qm", "Main vault")
        self.write("Sources/demo.md", "dirty specification")
        self.write("Plans/demo.md", "staged plan")
        self.git("add", "Plans/demo.md")
        self.write("Plans/demo.md", "current plan")
        self.write("Plans/new.md", "new plan")
        self.write("excluded.txt", "unrelated input")

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd or self.root, text=True).strip()

    def write(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data)

    def prepare(self, **overrides):
        args = dict(ticket_id="KOL-102/1", slug="demo", snapshot_a=self.snapshot,
                    inputs=["Sources/demo.md", "Plans/demo.md", "Plans/new.md"])
        args.update(overrides)
        return prepare_worktree(self.root, **args)

    def test_uses_snapshot_and_explicit_current_inputs_preserving_vault(self):
        head = self.git("rev-parse", "HEAD")
        index = (self.root / ".git/index").read_bytes()
        work = self.prepare()
        self.assertEqual(work.branch, "theseus/kol-102-1-demo")
        self.assertEqual(work.path, self.root / ".worktrees/kol-102-1-demo")
        self.assertEqual(self.git("rev-parse", "HEAD^", cwd=work.path), self.snapshot)
        for path, expected in {"Sources/demo.md": "dirty specification",
                               "Plans/demo.md": "current plan", "Plans/new.md": "new plan",
                               "snapshot.txt": "snapshot-only"}.items():
            self.assertEqual((work.path / path).read_text(), expected)
        self.assertFalse((work.path / "excluded.txt").exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual((self.root / ".git/index").read_bytes(), index)
        self.assertEqual(self.git("status", "--porcelain", cwd=work.path), "")

    def test_owned_checkout_resolves_vault_with_separate_git_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            admin = Path(directory) / "admin"
            self.git("init", "--separate-git-dir", str(admin))
            work = self.prepare()
            vault, identity, selected = owned_checkout(work.path)
            self.assertEqual(vault, self.root)
            self.assertEqual(identity, {"ticket_id": "KOL-102/1", "slug": "demo", "attempt": 1})
            self.assertEqual(selected, work)

    def test_resume_and_independent_attempt_from_pinned_inputs(self):
        first = self.prepare()
        (first.path / "trial.txt").write_text("first attempt")
        self.write("Plans/new.md", "changed since preparation")
        collision = self.root / ".worktrees/kol-102-1-demo-2"
        collision.mkdir()
        self.git("worktree", "add", "-qb", "theseus/kol-102-1-demo-3", str(self.root / "external"))
        self.assertEqual(self.prepare(), first)
        alternative = self.prepare(independent=True)
        self.assertEqual(alternative.branch, "theseus/kol-102-1-demo-4")
        self.assertEqual(alternative.path.name, "kol-102-1-demo-4")
        self.assertEqual(alternative.attempt, 4)
        self.assertEqual(self.prepare(attempt=4), alternative)
        self.assertEqual(
            verify_worktree(self.root, ticket_id="KOL-102/1", slug="demo", attempt=4),
            alternative,
        )
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=alternative.path), first.base)
        self.assertEqual((alternative.path / "Plans/new.md").read_text(), "new plan")
        self.assertFalse((alternative.path / "trial.txt").exists())
        self.assertEqual((first.path / "trial.txt").read_text(), "first attempt")
        self.assertTrue((self.root / "external").exists())

    def test_matching_branch_and_path_without_owner_cannot_be_resumed(self):
        path = self.root / ".worktrees/kol-102-1-demo"
        self.git("worktree", "add", "-qb", "theseus/kol-102-1-demo", str(path))
        with self.assertRaisesRegex(ValueError, "owned"):
            self.prepare()
        with self.assertRaisesRegex(ValueError, "owned"):
            verify_worktree(self.root, ticket_id="KOL-102/1", slug="demo")
        self.assertEqual(self.git("branch", "--show-current", cwd=path), "theseus/kol-102-1-demo")

    def test_missing_inputs_skip_absent_both_and_remove_snapshot_files(self):
        (self.root / "Plans/demo.md").unlink()
        work = self.prepare(inputs=["Plans/demo.md", "Plans/missing.md"])
        self.assertEqual(work.inputs, ("Plans/demo.md", "Plans/missing.md"))
        self.assertFalse((work.path / "Plans/demo.md").exists())
        self.assertFalse((work.path / "Plans/missing.md").exists())
        self.assertEqual(self.git("diff", "--name-status", self.snapshot, work.base), "D\tPlans/demo.md")

    def test_watcher_ticket_id_formats(self):
        for ticket_id, suffix in (("A2B-8/4/1", "a2b-8-4-1"), ("8/4/1", "8-4-1")):
            with self.subTest(ticket_id=ticket_id):
                work = self.prepare(ticket_id=ticket_id)
                self.assertEqual(work.branch, f"theseus/{suffix}-demo")
                self.assertEqual(self.prepare(ticket_id=ticket_id), work)
        for ticket_id in ("a2b-8", "2AB-8", "AB-08", "AB-8/0"):
            with self.subTest(ticket_id=ticket_id), self.assertRaisesRegex(ValueError, "ticket ID"):
                self.prepare(ticket_id=ticket_id)

    def test_inherited_git_environment_and_hooks_cannot_redirect_preparation(self):
        index = self.root / ".git/index"
        alternate_index = self.root / "alternate-index"
        alternate_index.write_bytes(index.read_bytes())
        hook = self.root / ".git/hooks/post-checkout"
        hook.write_text("#!/bin/sh\ntouch hook-fired\nexit 1\n")
        hook.chmod(0o755)
        original = index.read_bytes()
        with patch.dict(os.environ, {
            "GIT_INDEX_FILE": str(alternate_index), "GIT_DIR": "/wrong",
            "GIT_COMMON_DIR": "/wrong", "GIT_WORK_TREE": "/wrong",
            "GIT_OBJECT_DIRECTORY": "/wrong", "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(hook.parent),
        }):
            work = self.prepare()
            self.assertEqual(self.prepare(), work)
        self.assertFalse((work.path / "hook-fired").exists())
        self.assertEqual(original, index.read_bytes())
        self.assertEqual(original, alternate_index.read_bytes())

    def test_setup_failure_before_or_after_registration_is_retryable(self):
        path = self.root / ".worktrees/kol-102-1-demo"
        branch = "theseus/kol-102-1-demo"
        for register in (False, True):
            with self.subTest(register=register):
                def fail_add(root, *args, **options):
                    if args[:2] == ("worktree", "add"):
                        if register:
                            _git(root, *args, **options)
                        raise subprocess.CalledProcessError(1, args, stderr="injected add failure")
                    return _git(root, *args, **options)

                with patch("worktree._git", side_effect=fail_add):
                    with self.assertRaises(subprocess.CalledProcessError):
                        self.prepare()
                self.assertFalse(path.exists())
                self.assertNotIn(branch, self.git("branch", "--list"))
                self.assertNotIn(str(path), self.git("worktree", "list", "--porcelain"))
        self.assertEqual(self.prepare().path, path)

    def test_failed_setup_preserves_a_branch_moved_by_another_owner(self):
        branch = "theseus/kol-102-1-demo"
        head = self.git("rev-parse", "HEAD")

        def move_branch(root, *args, **options):
            if args[:2] == ("worktree", "add"):
                self.git("update-ref", "refs/heads/" + branch, head)
                raise subprocess.CalledProcessError(1, args, stderr="injected add failure")
            return _git(root, *args, **options)

        with patch("worktree._git", side_effect=move_branch):
            with self.assertRaisesRegex(RuntimeError, "partial setup preserved"):
                self.prepare()
        self.assertEqual(self.git("rev-parse", branch), head)

    def test_recreated_worktree_cannot_inherit_ownership(self):
        work = self.prepare()
        self.git("worktree", "remove", str(work.path))
        self.git("worktree", "add", str(work.path), work.branch)
        with self.assertRaisesRegex(ValueError, "ownership record"):
            self.prepare()

    def test_independent_attempt_cannot_resume_under_a_colliding_slug(self):
        self.prepare()
        alternative = self.prepare(independent=True)
        self.assertEqual(alternative.attempt, 2)
        with self.assertRaisesRegex(ValueError, "ownership record does not match"):
            self.prepare(slug="demo-2")

    def test_installed_cli_prepares_and_verifies_the_selected_attempt(self):
        lock = json.loads((SOURCE / "upstream.lock").read_text())
        archive = SOURCE.parents[1] / "References" / f"superpowers-{lock['commit']}.tar.gz"
        bundle = self.root / "installed"
        subprocess.run([sys.executable, str(SOURCE / "build.py"), "--archive", str(archive),
                        "--output", str(bundle)], check=True)
        skill = bundle / "skills/using-git-worktrees/SKILL.md"
        self.assertIn("using-theseus/scripts/worktree.py", skill.read_text())
        cli = bundle / "skills/using-theseus/scripts/worktree.py"

        def run(**request):
            return subprocess.run(
                [sys.executable, str(cli)], text=True, capture_output=True,
                input=json.dumps(dict(root=str(self.root), ticket_id="A2-8/1", slug="demo",
                                      **request)), cwd=self.root.parent,
            )

        initial = run(snapshot_a=self.snapshot, inputs=["Plans/new.md"])
        self.assertEqual(initial.returncode, 0, initial.stdout + initial.stderr)
        alternative = run(independent=True)
        self.assertEqual(alternative.returncode, 0, alternative.stdout + alternative.stderr)
        result = json.loads(alternative.stdout)
        self.assertEqual(result["branch"], "theseus/a2-8-1-demo-2")
        verification = run(operation="verify", attempt=result["attempt"])
        self.assertEqual(verification.returncode, 0, verification.stdout + verification.stderr)
        self.assertEqual(json.loads(verification.stdout), result)
        missing = run(operation="verify", attempt=3)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("not owned", json.loads(missing.stdout)["error"])


if __name__ == "__main__":
    unittest.main()
