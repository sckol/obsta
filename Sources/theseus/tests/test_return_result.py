"""Exercise return and interruption recovery in disposable Git repositories."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_evaluation as comparison
import return_result
from codex import RoleResult
from execution import run_execution
from notes import merge_return_section
from planning import Check, Task, render_plan
from return_result import _entry, inspect_return, resume_return, run_return
from scope import Entry, capture
from verification import run_verification
from worktree import prepare_worktree


BLOCK = "Sources/tool.md"
CHILD = "Sources/tool/child.py.md"
SOURCE = "Sources/tool/child.py"
OBSOLETE = "Sources/tool/obsolete.py"
PLAN = "Plans/tool.md"
COMMAND = f"grep -q 'value = 1' {SOURCE} && test ! -e {OBSOLETE}"
THREAD = "#KOL-53🍏 Current discussion\n*⏳ Processing…*\n#KOL-53🛑\n"
NOTE = f"""## User requirements

Keep the contract.

## Specification
### Acceptance

The value must be set.
Run `{COMMAND}`.

### Architecture and techniques

The old description.

## Working context

Prior context.

""" + THREAD + """
## Other

Outside the task.
"""


def git(root, *args):
    return comparison.git(root, *args)


def write(root, path, value):
    comparison.write(root, path, value)


def installed_cli(test):
    source = Path(__file__).resolve().parents[1]
    lock = json.loads((source / "upstream.lock").read_text())
    archive = source.parents[1] / "References" / f"superpowers-{lock['commit']}.tar.gz"
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    cwd = Path(temporary.name)
    bundle = cwd / "installed bundle"
    built = subprocess.run([sys.executable, str(source / "build.py"), "--archive", str(archive),
                            "--output", str(bundle)], cwd=cwd, capture_output=True, text=True)
    test.assertEqual(built.returncode, 0, built.stdout + built.stderr)
    skill = bundle / "skills/finishing-a-development-branch/SKILL.md"
    test.assertIn("using-theseus/scripts/return_result.py", skill.read_text())
    cli = bundle / "skills/using-theseus/scripts/return_result.py"

    def run(**request):
        process = subprocess.run([sys.executable, str(cli)], cwd=cwd, capture_output=True, text=True,
                                 input=json.dumps(request, default=str))
        result = json.loads(process.stdout)
        test.assertEqual(process.returncode, int(result["status"] == "blocked"), process.stderr)
        return result

    return run


def writer(root, path, expected, replacement):
    """Deterministic cooperative writer for temporary vaults and injected races."""
    if capture(root).get(path) != expected:
        return False
    target = root / path
    if replacement is None:
        target.unlink()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(replacement.data)
        target.chmod(replacement.mode)
    return True


def reviewer(role, settings, *, context, **options):
    assert role == "reviewer"
    brief = json.loads(context)
    report = {key: brief[key] for key in ("base", "head", "diff_sha256", "verification_sha256")}
    return RoleResult("success", json.dumps(dict(report, findings=[], dispositions=[])))


class ReturnTests(unittest.TestCase):
    def arrange(self, work="first", *, verified=True, new_plan=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault = vault = Path(temporary.name).resolve()
        git(vault, "init", "-q")
        git(vault, "config", "user.name", "Return test")
        git(vault, "config", "user.email", "test@example.invalid")
        write(vault, ".gitignore", "/.worktrees/\n")
        write(vault, BLOCK, NOTE)
        write(vault, CHILD, NOTE.replace(THREAD, "Child context.\n"))
        write(vault, SOURCE, "value = 0\n")
        write(vault, OBSOLETE, "obsolete = True\n")
        write(vault, "Plans/archive.md", "Historical plan.\n")
        write(vault, "outside.txt", "Committed")
        command = COMMAND
        check = Check(command, ".", "None", "Pass")
        task = Task("Update the child and remove obsolete code", (SOURCE, OBSOLETE),
                    "coder_light", "specified", (), (check,))
        plan_data = render_plan(BLOCK, work, "Apply and verify the change", (task,), {}, ()).decode()
        if not new_plan:
            write(vault, PLAN, plan_data + THREAD)
        git(vault, "add", "-A")
        git(vault, "commit", "-qm", "Baseline")
        self.main = git(vault, "rev-parse", "HEAD")
        self.identity = dict(ticket_id="KOL-15", slug="tool")
        self.selected = prepare_worktree(vault, **self.identity, snapshot_a=self.main)
        root = self.selected.path

        def coder(role, settings, *, cwd, context, **options):
            write(cwd, SOURCE, "value = 1\n")
            (cwd / OBSOLETE).unlink()
            git(cwd, "add", "-A")
            red = {"command": command, "cwd": ".", "exit": 1, "observed": "Baseline value and obsolete file."}
            green = {**red, "exit": 0, "observed": "New value, obsolete file absent."}
            git(cwd, "commit", "-qm", json.loads(context)["commit_subject"],
                "-m", "Theseus-evidence: " + json.dumps({"red": red, "green": [green]}))
            return RoleResult("success", "Committed")

        if new_plan:
            write(root, PLAN, plan_data)
            write(root, SOURCE, "value = 1\n")
            (root / OBSOLETE).unlink()
            git(root, "add", "-A")
            git(root, "commit", "-qm", "Fixture: verified bug fix with in-worktree planning")
        else:
            result = run_execution(vault, **self.identity, block=BLOCK, plan=PLAN, work=work, settings={},
                                   runner=coder, review=lambda changes: True)
            self.assertEqual(result.stage, "complete", result)
        self.head = git(root, "rev-parse", "HEAD")
        self.options = dict(vault=vault, **self.identity, block=BLOCK, plan=PLAN, work=work, accepted_head=self.head)
        self.checks = {note: {"acceptance": [{"command": command, "cwd": "."}], "mandatory": [], "tests": []}
                       for note in (BLOCK, CHILD)}
        if verified:
            self.verify()
        write(vault, "outside.txt", "Staged edit")
        git(vault, "add", "outside.txt")
        write(vault, "outside.txt", "Unstaged edit")
        self.index = (vault / ".git/index").read_bytes()
        self.journal = vault / ".git" / f"theseus-return-{root.name}.json"

    def verify(self):
        result = run_verification(**self.options, checks=self.checks, settings={}, runner=reviewer)
        self.assertEqual(result["status"], "verified", result)

    def run_return(self, backend=writer, **options):
        return run_return(**(self.options | options), conditional_writer=backend)

    def assert_main(self):
        self.assertEqual(git(self.vault, "rev-parse", "HEAD"), self.main)
        self.assertEqual((self.vault / ".git/index").read_bytes(), self.index)
        self.assertEqual((self.vault / "outside.txt").read_text(), "Unstaged edit")

    def test_first_modification_and_bug_fix_use_exact_verified_delta(self):
        for work in ("first", "modification", "bug_fix"):
            with self.subTest(work=work):
                self.arrange(work)
                write(self.vault, BLOCK, NOTE.replace("Outside the task.", "Current human prose."))
                write(self.vault, SOURCE, "Concurrent in-scope source edit.")
                untouched_child = (self.vault / CHILD).read_bytes()
                plan = self.vault / PLAN
                plan.write_text(plan.read_text().replace("Current discussion", "Updated plan ticket"))
                result = self.run_return()
                self.assertEqual(result["status"], "returned", result)
                self.assertEqual(set(result["files"]), {BLOCK, CHILD, PLAN, SOURCE, OBSOLETE})
                self.assertIn("Current human prose.", (self.vault / BLOCK).read_text())
                self.assertIn("Keep the contract.", (self.vault / BLOCK).read_text())
                self.assertIn(THREAD, (self.vault / BLOCK).read_text())
                child = (self.vault / CHILD).read_bytes()
                self.assertIn(untouched_child.split(b"## Working context")[0], child)
                self.assertIn(b"No verification blockers", child)
                self.assertIn("Final verification verified", plan.read_text())
                self.assertEqual((self.vault / SOURCE).read_text(), "value = 1\n")
                self.assertFalse((self.vault / OBSOLETE).exists())
                self.assertIn("Updated plan ticket", plan.read_text())
                self.assertFalse(self.selected.path.exists())
                self.assertEqual(git(self.vault, "rev-parse", self.selected.branch), self.head)
                self.assertEqual(self.run_return(), result)
                self.assert_main()

    def test_missing_or_stale_receipt_cannot_return(self):
        self.arrange(verified=False)
        result = self.run_return()
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("verification receipt", result["problems"][0])
        self.assertFalse(self.journal.exists())
        self.verify()
        result = self.run_return(accepted_head=self.selected.base)
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("revision changed", result["problems"][0])
        self.assertTrue(self.selected.path.exists())
        self.assert_main()

    def test_missing_backend_leaves_prepared_journal_and_no_writes(self):
        self.arrange()
        original = capture(self.vault)
        result = self.run_return(None)
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("conditional writer unavailable", result["problems"][0])
        self.assertEqual(capture(self.vault), original)
        self.assertEqual(json.loads(self.journal.read_text())["stage"], "prepared")
        self.assertEqual(self.run_return()["status"], "returned")
        self.assert_main()

    def test_binary_git_failure_still_yields_json(self):
        self.arrange()
        error = subprocess.CalledProcessError(1, ["git", "cat-file"], stderr=b"fatal: absent \xff blob\n")
        with patch("return_result._changes_to_return", side_effect=error):
            result = self.run_return()
        self.assertEqual(json.loads(json.dumps(result)), {"status": "blocked", "problems": ["fatal: absent � blob"]})

    def test_installed_cli_discovers_and_continues_only_the_saved_operation(self):
        self.arrange()
        cli = installed_cli(self)
        selectors = dict(vault=self.vault, ticket_id=self.identity["ticket_id"])
        self.assertEqual(cli(mode="inspect", **selectors), {"status": "missing"})
        self.assertIn("no saved return journal", cli(mode="resume", **selectors)["problems"][0])
        self.assertFalse(self.journal.exists())

        self.assertEqual(self.run_return(None)["status"], "blocked")
        saved = self.journal.read_bytes()
        found = cli(mode="inspect", **selectors)
        self.assertEqual((found["status"], found["stage"]), ("found", "prepared"))
        self.assertEqual(found["context"]["accepted_head"], self.head)
        self.assertEqual(found["context"]["action"], "return_new")
        self.assertEqual(cli(mode="resume", action="discard_new", **selectors)["status"], "blocked")
        self.assertEqual(cli(mode="resume", accepted_head=self.selected.base, **selectors)["status"], "blocked")
        self.assertEqual(self.journal.read_bytes(), saved)

        plan = self.vault / PLAN
        original = plan.read_bytes()
        edited = original.replace(b"## Tasks\n", b"## Tasks\n\nHuman task edit.\n", 1)
        self.assertNotEqual(edited, original)
        plan.write_bytes(edited)
        live = capture(self.vault)
        blocked = cli(mode="start", **self.options)
        self.assertIn(f"{PLAN}: Tasks changed", blocked["problems"][0])
        self.assertEqual(cli(mode="resume", **selectors), blocked)
        self.assertEqual(capture(self.vault), live)
        self.assertTrue(self.selected.path.exists())

        plan.write_bytes(original)
        result = cli(mode="resume", **selectors)
        self.assertEqual((result["status"], result["accepted_head"]), ("returned", self.head))
        self.assertEqual(cli(mode="resume", **selectors), result)
        self.assertIn("Final verification verified", plan.read_text())
        self.assertEqual((self.vault / SOURCE).read_bytes(), b"value = 1\n")
        self.assertFalse((self.vault / OBSOLETE).exists())
        self.assertFalse(self.selected.path.exists())
        self.assert_main()

    def test_installed_writer_blocks_edit_after_syncing_replacement(self):
        self.arrange()
        plan = self.vault / PLAN
        edited = plan.read_text().replace("Current discussion", "Concurrent plan ticket")
        prepare = return_result._prepare_replacement

        def edit_after_prepare(file, replacement):
            prepare(file, replacement)
            plan.write_text(edited)

        with patch("return_result._prepare_replacement", side_effect=edit_after_prepare):
            result = self.run_return(return_result._installed_write)
        self.assertIn(f"{PLAN}: conditional write conflict", result["problems"][0])
        self.assertEqual(plan.read_text(), edited)
        self.assertFalse(list((self.vault / ".git").glob(".theseus-return-*.tmp")))
        self.assertTrue(self.selected.path.exists())
        result = self.run_return(return_result._installed_write)
        self.assertEqual(result["status"], "returned", result)
        self.assertIn("Concurrent plan ticket", plan.read_text())
        self.assert_main()

    def test_installed_writer_recovers_directory_sync_failure(self):
        self.arrange()
        plan = self.vault / PLAN
        parent = plan.parent.stat()
        fsync = os.fsync

        def fail_directory_sync(descriptor):
            if os.path.samestat(os.fstat(descriptor), parent):
                raise OSError("simulated return directory sync failure")
            fsync(descriptor)

        with patch("return_result.os.fsync", side_effect=fail_directory_sync):
            for operation in (
                lambda: self.run_return(return_result._installed_write),
                lambda: resume_return(self.vault, **self.identity, conditional_writer=return_result._installed_write),
            ):
                result = operation()
                self.assertIn("simulated return directory sync failure", result["problems"][0])
                self.assertEqual(json.loads(self.journal.read_text())["active"], PLAN)
                self.assertTrue(self.selected.path.exists())
        self.assertIn("Final verification verified", plan.read_text())
        published = plan.stat()
        result = installed_cli(self)(mode="resume", vault=self.vault, **self.identity)
        self.assertEqual(result["status"], "returned", result)
        self.assertTrue(os.path.samestat(published, plan.stat()))
        self.assert_main()

    def test_installed_writer_cannot_clobber_creation_after_final_absence_check(self):
        self.arrange()
        path = "Sources/tool/nested/new.py"
        target = self.vault / path
        read_live = return_result._live_file

        def create_after_check(directory, name):
            actual = read_live(directory, name)
            self.assertIsNone(actual)
            target.write_bytes(b"New human file")
            return actual

        replacement = Entry("file", b"Verified candidate", 0o755)
        with patch("return_result._live_file", side_effect=create_after_check):
            self.assertFalse(return_result._installed_write(self.vault, path, None, replacement))
        self.assertEqual(target.read_bytes(), b"New human file")
        target.unlink()
        self.assertTrue(return_result._installed_write(self.vault, path, None, replacement))
        self.assertEqual(target.read_bytes(), replacement.data)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), replacement.mode)
        self.assertFalse(list((self.vault / ".git").glob(".theseus-return-*.tmp")))
        self.assert_main()

    def test_death_after_staging_keeps_scratch_outside_vault_and_can_resume(self):
        self.arrange()
        baseline = capture(self.vault)
        code = """
import os
import sys

sys.path.insert(0, sys.argv[1])
import return_result

prepare = return_result._prepare_replacement
def interrupt(file, replacement):
    prepare(file, replacement)
    os._exit(86)

return_result._prepare_replacement = interrupt
sys.exit(return_result.main())
"""
        scripts = str(Path(return_result.__file__).resolve().parent)
        process = subprocess.run([sys.executable, "-c", code, scripts], capture_output=True, text=True,
                                 input=json.dumps(self.options, default=str))
        self.assertEqual(process.returncode, 86, process.stderr)
        self.assertEqual(capture(self.vault), baseline)
        self.assertEqual(json.loads(self.journal.read_text())["active"], PLAN)
        self.assertEqual(len(list((self.vault / ".git").glob(".theseus-return-*.tmp"))), 1)
        result = installed_cli(self)(mode="resume", vault=self.vault, **self.identity)
        self.assertEqual(result["status"], "returned", result)
        self.assertFalse(self.selected.path.exists())
        self.assert_main()

    def test_installed_writer_guards_deletion_against_mode_and_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = "Sources/module.py"
            target = root / path
            external = root / "external.py"
            write(root, path, "baseline")
            write(root, external.name, "baseline")
            expected = Entry("file", b"baseline", 0o644)
            target.chmod(0o600)
            self.assertFalse(return_result._installed_write(root, path, expected, None))
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            target.unlink()
            os.link(external, target)
            with self.assertRaisesRegex(ValueError, "unsafe live file"):
                return_result._installed_write(root, path, expected, None)
            self.assertTrue(os.path.samefile(external, target))
            target.unlink()
            target.symlink_to(external)
            with self.assertRaises(ValueError):
                return_result._installed_write(root, path, expected, None)
            self.assertTrue(target.is_symlink())
            self.assertEqual(external.read_bytes(), b"baseline")

    def test_resume_requires_identity_and_rejects_foreign_provenance(self):
        self.arrange()
        self.assertEqual(self.run_return(backend=None)["status"], "blocked")
        selectors = dict(vault=self.vault, **self.identity)
        state = json.loads(self.journal.read_text())
        other = self.journal.with_name(self.journal.stem + "-2.json")
        second = state | {"context": state["context"] | {"attempt": 2}, "branch": state["branch"] + "-2"}
        other.write_text(json.dumps(second))
        self.assertIn("specify slug and attempt", inspect_return(**selectors)["problems"][0])
        self.assertEqual(resume_return(**selectors, attempt=2, conditional_writer=writer)["status"], "blocked")
        self.assertTrue(self.selected.path.exists())

        self.journal.write_text(json.dumps(state | {"vault": str(self.vault.parent)}))
        foreign = resume_return(**selectors, attempt=1, conditional_writer=writer)
        self.assertIn("vault provenance", foreign["problems"][0])
        self.assertTrue(self.selected.path.exists())

        stale = state | {"context": state["context"] | {"accepted_head": self.selected.base}}
        self.journal.write_text(json.dumps(stale))
        changed = resume_return(**selectors, attempt=1, conditional_writer=writer)
        self.assertIn("revision changed", changed["problems"][0])
        self.assertTrue(self.selected.path.exists())

        self.journal.write_text(json.dumps(state))
        self.assertEqual(resume_return(**selectors, attempt=1, conditional_writer=writer)["status"], "returned")
        self.assert_main()

    def test_installed_cli_finishes_after_interruption_following_worktree_removal(self):
        self.arrange()
        with patch("return_result._finish", side_effect=OSError("simulated interruption after removal")):
            result = self.run_return()
        self.assertIn("simulated interruption", result["problems"][0])
        self.assertFalse(self.selected.path.exists())
        self.assertEqual(json.loads(self.journal.read_text())["stage"], "saved")
        cli = installed_cli(self)
        write(self.vault, OBSOLETE, "Recreated during interruption.")
        blocked = cli(mode="resume", vault=self.vault, **self.identity)
        self.assertIn(f"{OBSOLETE}: saved result changed", blocked["problems"][0])
        self.assertEqual((self.vault / OBSOLETE).read_text(), "Recreated during interruption.")
        (self.vault / OBSOLETE).unlink()
        finish = cli(mode="resume", vault=self.vault, **self.identity)
        self.assertEqual((finish["status"], finish["accepted_head"]), ("returned", self.head))
        self.assertEqual(cli(mode="resume", vault=self.vault, **self.identity), finish)
        self.assert_main()

    def test_installed_cli_finishes_when_candidate_delta_is_already_present(self):
        self.arrange()
        self.assertEqual(self.run_return(backend=None)["status"], "blocked")
        for operation in json.loads(self.journal.read_text())["operations"]:
            self.assertTrue(writer(self.vault, operation["path"], _entry(operation["expected"]),
                                   _entry(operation["replacement"])))
        self.journal.unlink()
        live = capture(self.vault)
        result = installed_cli(self)(mode="start", **self.options)
        self.assertEqual(result["status"], "returned", result)
        self.assertEqual(capture(self.vault), live)
        self.assertFalse(self.selected.path.exists())
        self.assert_main()

    def test_interruption_after_write_resumes_and_rebases_pending_note(self):
        self.arrange()
        attempts = []
        def interrupted(root, path, expected, replacement):
            attempts.append(path)
            self.assertTrue(writer(root, path, expected, replacement))
            raise OSError("injected after write before journal")
        result = self.run_return(interrupted)
        self.assertIn("injected", result["problems"][0])
        self.assertEqual(json.loads(self.journal.read_text())["active"], PLAN)
        self.assertTrue(self.selected.path.exists())
        live = NOTE.replace("Outside the task.", "Latest boundary text.").replace("Processing…", "New status")
        write(self.vault, BLOCK, live)
        def logging(root, path, expected, replacement):
            attempts.append(path)
            return writer(root, path, expected, replacement)
        result = resume_return(self.vault, **self.identity, conditional_writer=logging)
        self.assertEqual(result["status"], "returned", result)
        self.assertEqual(attempts, [PLAN, BLOCK, CHILD, SOURCE, OBSOLETE])
        self.assertIn("Latest boundary text.", (self.vault / BLOCK).read_text())
        self.assertIn("*⏳ New status*", (self.vault / BLOCK).read_text())
        self.assert_main()

    def test_race_between_comparison_and_write_never_clobbers_note(self):
        self.arrange()
        plan = self.vault / PLAN
        edited = plan.read_text().replace("Current discussion", "Concurrent plan ticket")
        def edit_first(root, path, expected, replacement):
            self.assertEqual(path, PLAN)
            plan.write_text(edited)
            return writer(root, path, expected, replacement)
        result = self.run_return(edit_first)
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn(f"{PLAN}: conditional write conflict", result["problems"][0])
        self.assertEqual(plan.read_text(), edited)
        self.assertTrue(self.selected.path.exists())
        self.assertEqual(self.run_return()["status"], "returned")
        self.assertIn("Concurrent plan ticket", plan.read_text())
        self.assert_main()

    def test_concurrent_change_to_staged_source_between_writes_blocks(self):
        self.arrange()
        def edit_after_first(root, path, expected, replacement):
            success = writer(root, path, expected, replacement)
            if path == PLAN:
                write(root, SOURCE, "concurrent source")
            return success
        result = self.run_return(edit_after_first)
        self.assertIn(f"{SOURCE}: live file changed", result["problems"][0])
        self.assertEqual((self.vault / SOURCE).read_text(), "concurrent source")
        self.assertTrue(self.selected.path.exists())
        self.assertIn(f"{SOURCE}: live file changed", self.run_return()["problems"][0])
        self.assert_main()

    def test_no_op_paths_are_checked_before_cleanup(self):
        self.arrange()
        write(self.vault, SOURCE, "value = 1\n")
        def edit_after_prepare(root, path, expected, replacement):
            success = writer(root, path, expected, replacement)
            if path == PLAN:
                write(root, SOURCE, "concurrent source")
            return success
        result = self.run_return(edit_after_prepare)
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn(f"{SOURCE}: previously applied value changed", result["problems"][0])
        self.assertTrue(self.selected.path.exists())
        self.assert_main()

    def test_already_applied_note_changed_blocks_retry(self):
        self.arrange()
        def cleanup_interrupted(root, path, expected, replacement):
            if path == BLOCK:
                raise OSError("pause before second write")
            return writer(root, path, expected, replacement)
        self.assertEqual(self.run_return(cleanup_interrupted)["status"], "blocked")
        plan = self.vault / PLAN
        plan.write_text(plan.read_text() + "\nLatest discussion.\n")
        result = self.run_return()
        self.assertIn(f"{PLAN}: previously applied value changed", result["problems"][0])
        self.assertTrue(self.selected.path.exists())
        self.assert_main()

    def test_ignored_uncommitted_worktree_file_refuses_cleanup(self):
        self.arrange()
        write(self.selected.path, ".worktrees/uncommitted", "unsaved")
        result = self.run_return()
        self.assertIn("worktree contains untracked data", result["problems"][0])
        self.assertFalse(self.journal.exists())
        self.assertTrue(self.selected.path.exists())
        self.assert_main()

    def test_bug_fix_partial_specification_merges_only_changed_lines(self):
        before = NOTE.encode()
        candidate = before.replace(b"The value must be set.", b"The value must equal one.")
        live = before.replace(b"The old description.", b"Latest architecture.")
        result = merge_return_section(before, candidate, live, ("Specification",), partial=True)
        self.assertIn(b"The value must equal one.", result)
        self.assertIn(b"Latest architecture.", result)
        conflict = before.replace(b"The value must be set.", b"Human acceptance.")
        with self.assertRaisesRegex(ValueError, "changed part of Specification"):
            merge_return_section(before, candidate, conflict, ("Specification",), partial=True)

    def test_bug_fix_can_return_the_new_current_plan(self):
        self.arrange("bug_fix", verified=False, new_plan=True)
        self.assertFalse((self.vault / PLAN).exists())
        with patch("return_result.verified_receipt", return_value=("fixture-receipt", {})):
            result = self.run_return()
        self.assertEqual(result["status"], "returned", result)
        self.assertIn(PLAN, result["files"])
        self.assertIn("Apply and verify the change", (self.vault / PLAN).read_text())
        self.assertEqual((self.vault / "Plans/archive.md").read_text(), "Historical plan.\n")
        self.assert_main()

    def test_bug_fix_delta_returns_only_changed_part_of_specification(self):
        self.arrange("bug_fix")
        note = self.selected.path / BLOCK
        note.write_text(note.read_text().replace("The value must be set.", "The value must equal one."))
        git(self.selected.path, "add", BLOCK)
        git(self.selected.path, "commit", "-qm", "Fixture: scoped accepted Specification edit")
        self.options["accepted_head"] = git(self.selected.path, "rev-parse", "HEAD")
        write(self.vault, BLOCK, NOTE.replace("The old description.", "Latest architecture."))
        # Execution history has no bug-fix planning handoff yet. Isolate the return policy
        # for a verified revision with an authorized Specification edit.
        with patch("return_result.verified_receipt", return_value=("fixture-receipt", {})):
            result = self.run_return()
        self.assertEqual(result["status"], "returned", result)
        self.assertIn("The value must equal one.", (self.vault / BLOCK).read_text())
        self.assertIn("Latest architecture.", (self.vault / BLOCK).read_text())
        self.assert_main()


class RefactoringReturnTests(unittest.TestCase):
    def arrange(self, *, old_plan="Plans/archive.md", choice="new", verify=True):
        fixture = comparison.EvaluationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.vault = fixture.vault
        write(self.vault, old_plan, "Historical plan.\n")
        write(self.vault, "Sources/engine/child.py.md", "## User requirements\n\nPreserve this.\n")
        write(self.vault, "Sources/engine/child.py", "CHILD = True\n")
        git(self.vault, "add", "-A")
        git(self.vault, "commit", "-qm", "Historical inputs")
        fixture.main = git(self.vault, "rev-parse", "HEAD")
        fixture.index = (self.vault / ".git/index").read_bytes()
        self.selected = None
        basis = fixture.prepare()
        result, _ = fixture.evaluate(basis)
        self.report = result["report_id"]
        fixture.evaluation.select(reply=f"{choice} {self.report}")
        self.fixture = fixture
        self.selected = fixture.worktree
        self.options = dict(vault=self.vault, ticket_id="KOL-102/1", slug="engine", block=comparison.BLOCK,
                            plan=comparison.PLAN, work="refactoring", accepted_head=fixture.new)
        if verify:
            checks = {comparison.BLOCK: {
                "acceptance": [{"command": f"python3 {comparison.TEST}", "cwd": "."}],
                "tests": [], "mandatory": [],
            }}
            verified = run_verification(**self.options, checks=checks, settings={}, runner=reviewer)
            self.assertEqual(verified["status"], "verified", verified)

    def finish(self, action="return_new", backend=writer, **options):
        return run_return(**self.options, action=action, conditional_writer=backend,
                          report_id=options.get("report_id", self.report))

    def assert_main(self):
        self.assertEqual(git(self.vault, "rev-parse", "HEAD"), self.fixture.main)
        self.assertEqual((self.vault / ".git/index").read_bytes(), self.fixture.index)

    def test_refactoring_returns_new_plan_preserves_archives_and_descendants(self):
        self.arrange()
        result = installed_cli(self)(mode="start", report_id=self.report, **self.options)
        self.assertEqual(result["status"], "returned", result)
        self.assertEqual((self.vault / "Plans/archive.md").read_text(), "Historical plan.\n")
        self.assertEqual((self.vault / "Sources/engine/child.py").read_text(), "CHILD = True\n")
        self.assertIn("Preserve this.", (self.vault / "Sources/engine/child.py.md").read_text())
        block = (self.vault / comparison.BLOCK).read_text()
        self.assertIn("Simple transformation.", block)
        self.assertIn(comparison.OPEN_TICKET, block)
        self.assertIn("Negative inputs produce their magnitude.", block)
        self.assertTrue((self.vault / comparison.PLAN).is_file())
        self.assertFalse(self.selected.path.exists())
        self.assert_main()

    def test_refactoring_historical_plan_collision_blocks(self):
        self.arrange(old_plan=comparison.PLAN)
        result = self.finish()
        self.assertIn("historical plan path collision", result["problems"][0])
        self.assertEqual((self.vault / comparison.PLAN).read_text(), "Historical plan.\n")
        self.assertTrue(self.selected.path.exists())
        self.assert_main()

    def test_refactoring_revalidates_choice_after_no_op_and_failed_first_write(self):
        self.arrange()
        self.assertEqual(self.finish(backend=None)["status"], "blocked")
        journal = self.vault / ".git" / f"theseus-return-{self.selected.path.name}.json"
        prepared = json.loads(journal.read_text())
        note = next(item for item in prepared["operations"] if item["path"] == comparison.BLOCK)
        self.assertTrue(writer(self.vault, note["path"], _entry(note["expected"]), _entry(note["replacement"])))
        journal.unlink()
        failed = self.finish(backend=lambda *args: False)
        self.assertIn("conditional write conflict", failed["problems"][0])
        state = json.loads(journal.read_text())
        self.assertEqual(state["active"], comparison.PLAN)
        self.assertTrue(next(item for item in state["operations"] if item["path"] == comparison.BLOCK)["applied"])

        write(self.vault, comparison.CODE, "Concurrent code change.")
        result = self.finish()
        self.assertIn("stale evaluation", result["problems"][0])
        self.assertEqual((self.vault / comparison.CODE).read_text(), "Concurrent code change.")
        self.assertFalse((self.vault / comparison.PLAN).exists())
        self.assertTrue(self.selected.path.exists())
        self.assert_main()

    def test_refactoring_exact_keep_old_choice_removes_only_clean_worktree(self):
        self.arrange(choice="old", verify=False)
        prior = capture(self.vault)
        blocked = self.finish("discard_new", backend=None, report_id="0" * 64)
        self.assertEqual(blocked["status"], "blocked")
        (self.selected.path / "local.txt").write_text("Unsaved work")
        self.assertEqual(self.finish("discard_new", backend=None)["status"], "blocked")
        (self.selected.path / "local.txt").unlink()
        result = installed_cli(self)(mode="start", action="discard_new", report_id=self.report, **self.options)
        self.assertEqual(result["status"], "discarded", result)
        self.assertEqual(capture(self.vault), prior)
        self.assertFalse(self.selected.path.exists())
        self.assertEqual(git(self.vault, "rev-parse", self.selected.branch), self.fixture.new)
        self.assert_main()


if __name__ == "__main__":
    unittest.main()
