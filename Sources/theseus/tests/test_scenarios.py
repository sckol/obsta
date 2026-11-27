"""A2–A7 on disposable fixture vaults, using interpreted intents and scripted roles.

Codex lifecycle, watcher snapshots and a real conditional vault writer are
outside this suite. Git setup and tests never operate on the source vault.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex import RoleExecutionError, RoleResult, run_role
from diagnostics import Diagnostics
from evaluation import Evaluation
from execution import run_execution
from notes import read_section
from planning import ACCEPTANCE, ARCHITECTURE, run_planning
from refactoring import run_refactoring_planning
from return_result import run_return
from review import ReviewController
from routing import Decision, LocalChange, Plan, route
from scope import authorize, block_scope, capture
from verification import run_verification
from worktree import prepare_worktree

from test_codex import FAKE_CODEX, SETTINGS
from test_fixtures import FIXTURE


FILE = "Sources/file-block.py"
FILE_NOTE = FILE + ".md"
FOLDER = "Sources/folder-block"
FOLDER_NOTE = FOLDER + ".md"
CHILD = FOLDER + "/child.py"
CHILD_NOTE = CHILD + ".md"
PARENT = FOLDER + "/__init__.py"
API = FOLDER + "/api.py"
REGRESSION = FOLDER + "/test_regression.py"
CURRENT = "Plans/current-plan.md"
ARCHIVE = "Plans/archived-plan.md"
PLAN = "Plans/scenario.md"
JOURNAL = ".obsta-experiments/KOL-51/wrong-result.json"
PYTHON = shlex.quote(sys.executable)
UNCHANGED = "def run(value):\n    return value\n"
DESCRIBE = "\ndef describe(value):\n    return str(value)\n"
DELEGATE = (
    "from pathlib import Path\nfrom runpy import run_path\n\n"
    "def run(value):\n    return run_path(str(Path(__file__).with_name('child.py')))['run'](value)\n"
)


def git(root, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(root), "-c", f"core.hooksPath={os.devnull}", *args],
        capture_output=True, check=True, text=True, env=env,
    ).stdout.strip()


def write(root, path, contents):
    target = root / path
    if contents is None:
        target.unlink()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")


def check_code(path, value="42", marker="wrong result"):
    return (
        f"from runpy import run_path; run = run_path({path!r})['run']; "
        f"assert run(42) == {value}, {marker!r}"
    )


def command(code):
    return f"{PYTHON} -c {shlex.quote(code)}"


def shell(root, entry):
    return subprocess.run(
        entry["command"], cwd=root / entry["cwd"], env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        shell=True, capture_output=True, text=True, timeout=10,
    )


def task(files, check, *, description="Perform the selected implementation change", depends=(), role="coder_light"):
    return {
        "description": description, "files": list(files), "coder": role,
        "complexity": "specified" if role == "coder_light" else "design", "depends_on": list(depends),
        "checks": [{"command": check, "cwd": ".", "setup": "Python 3", "expected": "Exit 0"}],
    }


def manifest(check, *, mandatory=(), tests=()):
    def entries(commands):
        return [{"command": item, "cwd": "."} for item in commands]

    return {"acceptance": entries([check]), "mandatory": entries(mandatory), "tests": entries(tests)}


def changed(before, after):
    return {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="theseus-scenarios-")
        self.addCleanup(temporary.cleanup)
        self.area = Path(temporary.name).resolve()
        self.vault = self.area / "vault"
        shutil.copytree(FIXTURE, self.vault)
        write(self.vault, ".gitignore", "/.worktrees/\n/.obsta-experiments/\n")
        write(self.vault, "Discussion.md", "# Ordinary note\n\nKeep this human text.\n")
        git(self.vault, "init", "-q")
        git(self.vault, "config", "user.name", "Theseus acceptance test")
        git(self.vault, "config", "user.email", "scenario@example.invalid")
        self.identity = {"ticket_id": "KOL-51", "slug": "scenarios"}
        self.calls = []
        self.writes = []
        self.snapshot()

    def snapshot(self):
        git(self.vault, "add", "-A")
        git(self.vault, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "Fixture snapshot A")
        self.snapshot_a = git(self.vault, "rev-parse", "HEAD")
        self.index = (self.vault / ".git/index").read_bytes()

    def assert_main_git(self):
        self.assertEqual(git(self.vault, "rev-parse", "HEAD"), self.snapshot_a)
        self.assertEqual((self.vault / ".git/index").read_bytes(), self.index)

    def assert_note_preserved(self, before, after, note, *, specification=True):
        sections = ["User requirements", "Actions"]
        if specification:
            sections.append("Specification")
        for section in sections:
            self.assertEqual(read_section(before[note].data, section, root_level=2),
                             read_section(after[note].data, section, root_level=2), section)

    def prepare(self, inputs):
        return prepare_worktree(self.vault, **self.identity, snapshot_a=self.snapshot_a, inputs=inputs)

    def roles(self, answers):
        def runner(role, settings, *, cwd, context, task, allowed_paths, allowed_sections, checks, read_only=False):
            self.assertEqual(settings, SETTINGS)
            brief = json.loads(context)
            self.calls.append((role, cwd, list(allowed_paths), allowed_sections, read_only, brief))
            self.assertTrue(task)
            if role in {"planner", "reviewer", "evaluator"}:
                self.assertTrue(read_only)
                self.assertFalse(allowed_paths or allowed_sections or checks)
            if role.startswith("coder"):
                self.assertFalse(read_only)
                self.assertEqual(checks, [item["command"] for item in brief["checks"]])
            return answers[role](cwd, brief, allowed_paths)
        return runner

    def plan(self, decision, tasks, *, root=None, plan=PLAN, updates=None, before_proposal=None):
        root = root or self.vault
        note = decision.block
        if updates is None:
            updates = {ARCHITECTURE: "The entry point is a Python function.",
                       ACCEPTANCE: f"Run {tasks[0]['checks'][0]['command']}."}
        payload = {
            "goal": "Implement the behavior approved in the ticket.",
            "specification": [{"note": note, "section": section, "text": text} for section, text in updates.items()],
            "tasks": tasks, "external_dependencies": [],
        }
        files = sorted({path for item in tasks for path in item["files"]})

        def proposal(cwd, brief, _paths):
            self.assertEqual(cwd, root)
            self.assertEqual(brief["approved_implementation_files"], files)
            self.assertEqual(brief["approved_specification_sections"], {note: [list(key) for key in updates]})
            self.assertEqual(brief["block"], note)
            if before_proposal is not None:
                before_proposal()
            return RoleResult("success", json.dumps(payload))

        return run_planning(root, decision, ticket="Approved fixture requirements.", plan_path=plan,
                            files=files, sections={note: list(updates)}, settings=SETTINGS,
                            runner=self.roles({"planner": proposal}))

    def code(self, modifications, *, before_red=None, after_green=None, replace=False, foreign=False):
        def scripted_coder(cwd, brief, allowed):
            self.assertEqual(set(allowed), set(modifications))
            if before_red is not None:
                before_red(cwd)
            check = brief["checks"][0]
            red = shell(cwd, check)
            self.assertNotEqual(red.returncode, 0, red.stdout + red.stderr)
            for path, contents in modifications.items():
                write(cwd, path, contents)
            if after_green is not None:
                after_green(cwd)
            green = []
            for item in brief["checks"]:
                run = shell(cwd, item)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                green.append({"command": item["command"], "cwd": item["cwd"], "exit": run.returncode,
                              "observed": run.stdout.strip() or "Successful exit"})
            paths = list(allowed)
            if foreign:
                write(cwd, "Discussion.md", "Unauthorised replacement.\n")
                paths.append("Discussion.md")
            git(cwd, "add", "-A", "--", *paths)
            evidence = {
                "red": {"command": check["command"], "cwd": check["cwd"], "exit": red.returncode,
                        "observed": (red.stdout + red.stderr).strip()},
                "green": green,
            }
            git(cwd, "-c", "commit.gpgsign=false", "commit", *(["--amend"] if replace else []), "-qm",
                brief["commit_subject"], "-m", "Theseus-evidence: " + json.dumps(evidence))
            return RoleResult("success", "Checked candidate committed.")
        return scripted_coder

    def review(self, _cwd, brief, _paths, *, findings=(), dispositions=()):
        self.assertTrue(brief["diff"])
        self.assertRegex(brief["diff_sha256"], r"^[a-f0-9]{64}$")
        version = {key: brief[key] for key in ("base", "head", "diff_sha256")}
        if "verification_sha256" in brief:
            self.assertTrue(brief["checks"])
            self.assertTrue(all(item["status"] == "passed" for item in brief["checks"]))
            version["verification_sha256"] = brief["verification_sha256"]
        return RoleResult("success", json.dumps(dict(version, findings=list(findings), dispositions=list(dispositions))))

    def options(self, note, work, plan=PLAN):
        return dict(vault=self.vault, **self.identity, block=note, plan=plan, work=work)

    def execute(self, note, work, coder, *, worktree, plan=PLAN, reviewer=None, **options):
        answers = {role: coder for role in ("coder", "coder_light", "coder_heavy")}
        controller = ReviewController(worktree.path, SETTINGS, "Meet the approved acceptance and preserve contracts.",
                                      runner=self.roles({"reviewer": reviewer or self.review}))
        return run_execution(**self.options(note, work, plan), settings=SETTINGS, runner=self.roles(answers),
                             review=controller, **options)

    def verify(self, note, work, selected, checks, *, plan=PLAN, status="verified"):
        options = dict(self.options(note, work, plan), accepted_head=git(selected.path, "rev-parse", "HEAD"))
        report = run_verification(**options, checks=checks, settings=SETTINGS,
                                  runner=self.roles({"reviewer": self.review}))
        self.assertEqual(report["status"], status, report)
        return options, report

    def cas(self, root, path, expected, replacement):
        self.assertEqual(root, self.vault)
        self.assertEqual(capture(root).get(path), expected)
        self.writes.append(path)
        if replacement is None:
            (root / path).unlink()
        else:
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes(replacement.data)
            (root / path).chmod(replacement.mode)
        return True

    def returned(self, options, selected, paths):
        result = run_return(**options, conditional_writer=self.cas)
        self.assertEqual(result["status"], "returned", result)
        self.assertEqual(set(result["files"]), set(paths))
        self.assertFalse(selected.path.exists())
        self.assertEqual(git(self.vault, "rev-parse", selected.branch), options["accepted_head"])
        self.assert_main_git()
        return result

    def test_a2_routes_and_fresh_role_process_arguments_in_uninstalled_vault(self):
        before = capture(self.vault)
        self.assertFalse((self.vault / "Sources/theseus").exists())
        self.assertFalse((self.vault / ".obsidian/plugins").exists())
        plan = Plan(CURRENT, FILE_NOTE, "modification", "ready")
        cases = [
            (FILE_NOTE, {"action": "discuss", "block_candidates": [FILE_NOTE]}, "brainstorm"),
            (CURRENT, {"action": "execute", "plan": plan}, "execute"),
            ("Discussion.md", {"write_targets": [FILE], "block_candidates": [FILE_NOTE],
                               "action": "discuss"}, "brainstorm"),
            ("Discussion.md", {"write_targets": [PLAN], "block_candidates": [FILE_NOTE],
                               "work": "first", "requirements_ready": True}, "plan"),
            ("Discussion.md", {}, "ordinary"),
        ]
        for note, arguments, stage in cases:
            with self.subTest(note=note, stage=stage):
                self.assertEqual(route(note, **arguments).stage, stage)
        bin_path = self.area / "bin"
        write(self.area, "bin/codex", FAKE_CODEX)
        (bin_path / "codex").chmod(0o755)
        selected = self.prepare([FILE_NOTE])
        logs = []
        for role_setting, value in SETTINGS.items():
            role = role_setting.removeprefix("theseus.")
            model, effort = value.split("/")
            paths = [FILE] if role.startswith("coder") else []
            log = self.area / f"{role}.json"
            directory = self.vault if role == "planner" else selected.path
            env = {
                "PATH": str(bin_path) + os.pathsep + os.environ["PATH"],
                "FAKE_CODEX_LOG": str(log), "FAKE_CODEX_MODE": "success",
                "FAKE_CODEX_RESULT": '{"outcome":"success","response":"Scripted role finished."}',
            }
            with patch.dict(os.environ, env):
                result = run_role(role, SETTINGS, cwd=directory, task="Perform fixture stage.",
                                  context=f"Scope of {role}", allowed_paths=paths, allowed_sections={},
                                  checks=[], read_only=not paths)
            self.assertEqual(result.outcome, "success")
            invocation = json.loads(log.read_text(encoding="utf-8"))
            argv = invocation["argv"]
            self.assertEqual(argv[0], "exec")
            self.assertIn("--ephemeral", argv)
            self.assertEqual(argv[argv.index("-m") + 1], model)
            self.assertIn(f'model_reasoning_effort="{effort}"', argv)
            self.assertEqual(argv[argv.index("-C") + 1], str(directory))
            self.assertEqual(invocation["cwd"], str(directory))
            self.assertEqual("--sandbox" in argv, not paths)
            brief = json.loads(invocation["prompt"].split("\n\n", 1)[1])
            self.assertEqual((brief["role"], brief["context"], brief["allowed_paths"], brief["allowed_sections"]),
                             (role, f"Scope of {role}", paths, {}))
            logs.append(log)
        self.assertEqual(len(logs), len(SETTINGS))
        self.assertEqual(capture(self.vault), before)
        self.assert_main_git()

    def new_block_scenario(self, note, path, code, *, direct):
        (self.vault / (FILE if note == FILE_NOTE else PARENT)).unlink()
        self.snapshot()
        initial = capture(self.vault)
        discussion = route(note, block_candidates=[note], action="discuss")
        response = run_planning(self.vault, discussion, ticket="Discuss requirements.",
                                discussion="The entry point and observable behavior are agreed.")
        self.assertEqual(response.stage, "discussed")
        self.assertIn("observable behavior", response.response)
        self.assertEqual(capture(self.vault), initial)
        check = command(check_code(path))
        decision = route(note, block_candidates=[note], work="first", requirements_ready=True,
                         execute_after_plan=direct)
        planned = self.plan(decision, [task([path], check)])
        self.assertEqual(planned.stage, "execute" if direct else "await_execution")
        after_plan = capture(self.vault)
        self.assertEqual(changed(initial, after_plan), {note, PLAN})
        self.assert_note_preserved(initial, after_plan, note, specification=False)
        self.assertNotIn(path, after_plan)
        ready = Plan(PLAN, note, "first", "ready")
        if not direct:
            self.assertEqual(route(PLAN, plan=ready).stage, "await_execution")
            execution = route(PLAN, plan=ready, action="execute")
            self.assertEqual(run_planning(self.vault, execution, ticket="Execute confirmed plan.",
                                          files=[path]).stage, "execute")
        write(self.vault, "Discussion.md", "An unrelated edit made after snapshot A.\n")
        selected = self.prepare([note, PLAN])
        self.assertEqual(selected.snapshot_a, self.snapshot_a)
        work = capture(selected.path)
        self.assertEqual(work[note], after_plan[note])
        self.assertEqual(work[PLAN], after_plan[PLAN])
        self.assertEqual(work["Discussion.md"], initial["Discussion.md"])
        self.assertEqual(block_scope(work, note).kind, "file" if note == FILE_NOTE else "folder")
        before_return = capture(self.vault)
        self.assertEqual(self.execute(note, "first", self.code({path: code}), worktree=selected).stage, "complete")
        self.assertEqual(capture(self.vault), before_return)
        options, report = self.verify(note, "first", selected, {note: manifest(check)})
        self.assertEqual(report["version"]["head"], options["accepted_head"])
        self.returned(options, selected, {note, PLAN, path})
        final = capture(self.vault)
        self.assertEqual(changed(before_return, final), {note, PLAN, path})
        self.assert_note_preserved(before_return, final, note)
        self.assertIn(b"*Done", final[PLAN].data)
        self.assertEqual(final[path].data.decode(), code)
        self.assertEqual([call[0] for call in self.calls], ["planner", "coder_light", "reviewer", "reviewer"])
        self.assertEqual(self.calls[0][1], self.vault)
        self.assertEqual(self.calls[1][1:5], (selected.path, [path], {}, False))
        self.assertNotEqual(self.calls[2][-1]["head"], self.calls[3][-1]["head"])

    def test_a3_new_file_block_waits_for_execution_request(self):
        self.new_block_scenario(FILE_NOTE, FILE, UNCHANGED, direct=False)

    def test_a3_new_folder_block_executes_with_explicit_authorization(self):
        self.new_block_scenario(FOLDER_NOTE, API, DELEGATE, direct=True)

    def test_a3_local_modification_keeps_existing_requirement_and_uses_no_worktree(self):
        before = capture(self.vault)
        check = command(check_code(FILE))
        intent = LocalChange([FILE], check, independent=True, single_block=True)
        decision = route(FILE_NOTE, block_candidates=[FILE_NOTE], work="modification", requirements_ready=True,
                         write_targets=[FILE], local=intent)
        self.assertEqual(decision.stage, "local")
        result = run_planning(self.vault, decision, ticket="Cover the unchanged behavior.", files=[FILE],
                              local_verification=check)
        self.assertEqual(result.stage, "local")
        self.assertIn(check, result.response)
        write(self.vault, FILE, UNCHANGED.replace("    return value", "    result = value\n    return result"))
        after = capture(self.vault)
        delta = authorize(before, after, FILE_NOTE, role="obstagent", stage="local", work="modification", files=[FILE])
        self.assertEqual([item.path for item in delta], [FILE])
        self.assertEqual(shell(self.vault, {"command": check, "cwd": "."}).returncode, 0)
        self.assertEqual(changed(before, after), {FILE})
        self.assertFalse((self.vault / ".worktrees").exists())
        self.assertEqual(self.calls, [])
        self.assert_main_git()

    def modification(self):
        describe_check = f"assert run_path({CHILD!r})['describe'](42) == '42'"
        check = command(f"{check_code(CHILD)}; {describe_check}")
        second = command(f"{check_code(API)}; {describe_check}")
        intent = route(FOLDER_NOTE, block_candidates=[FOLDER_NOTE], work="modification", requirements_ready=True)
        self.assertEqual(intent.stage, "plan")
        before = capture(self.vault)
        planned = self.plan(intent, [task([CHILD], check),
                                     task([API, PARENT], second, depends=[1], role="coder")],
                            updates={ACCEPTANCE: "The parent delegates to the child and preserves the result. The child can format a value."})
        self.assertEqual(planned.stage, "await_execution")
        self.assertEqual(planned.tasks[1].depends_on, (1,))
        after = capture(self.vault)
        self.assertEqual(changed(before, after), {FOLDER_NOTE, PLAN})
        self.assert_note_preserved(before, after, FOLDER_NOTE, specification=False)
        self.assertEqual(read_section(before[FOLDER_NOTE].data, ARCHITECTURE, root_level=2),
                         read_section(after[FOLDER_NOTE].data, ARCHITECTURE, root_level=2))
        ready = route(PLAN, plan=Plan(PLAN, FOLDER_NOTE, "modification", "ready"), action="execute")
        self.assertEqual(run_planning(self.vault, ready, ticket="Execute modification.",
                                      files=[API, CHILD, PARENT]).stage, "execute")
        selected = self.prepare([FOLDER_NOTE, PLAN])
        edits = {
            1: self.code({CHILD: UNCHANGED + DESCRIBE}),
            2: self.code({API: DELEGATE, PARENT: None}),
        }

        def coder(cwd, brief, paths):
            return edits[brief["number"]](cwd, brief, paths)

        checks = {
            FOLDER_NOTE: manifest(second, mandatory=[command(check_code(FILE))]),
            CHILD_NOTE: manifest(check),
        }
        return selected, coder, checks

    def test_a3_a7_planned_modification_interrupted_return_recovers_exact_authorized_changes(self):
        selected, coder, checks = self.modification()
        self.assertEqual(self.execute(FOLDER_NOTE, "modification", coder, worktree=selected).stage, "complete")
        _, incomplete = self.verify(FOLDER_NOTE, "modification", selected, {FOLDER_NOTE: checks[FOLDER_NOTE]},
                                    status="blocked")
        self.assertIn(f"missing verification manifest for {CHILD_NOTE}", incomplete["problems"])
        options, report = self.verify(FOLDER_NOTE, "modification", selected, checks)
        self.assertEqual({item["block"] for item in report["checks"]}, {FOLDER_NOTE, CHILD_NOTE})
        note_path = self.vault / FOLDER_NOTE
        note_path.write_text(note_path.read_text().replace("Update parent block", "Current human task wording"))
        write(self.vault, CHILD, "# Concurrent file work inside the return scope.\n" + UNCHANGED)
        write(self.vault, "Discussion.md", "Staged unrelated human text.\n")
        git(self.vault, "add", "--", "Discussion.md")
        self.index = (self.vault / ".git/index").read_bytes()
        write(self.vault, "Discussion.md", "Unstaged unrelated human text.\n")
        before = capture(self.vault)
        stop_at = PLAN

        def interrupted(root, path, expected, replacement):
            written = self.cas(root, path, expected, replacement)
            if path == stop_at:
                raise OSError("Interrupted after applying a note before journal acknowledgement")
            return written

        result = run_return(**options, conditional_writer=interrupted)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("Interrupted after applying", result["problems"][0])
        journal = self.vault / ".git" / f"theseus-return-{selected.path.name}.json"
        partial = json.loads(journal.read_text())
        self.assertEqual((partial["stage"], partial["active"]), ("applying", stop_at))
        self.assertEqual(partial["base"], selected.base)
        self.assertEqual(partial["context"]["accepted_head"], options["accepted_head"])
        self.assertTrue(selected.path.exists())
        self.assertEqual(self.writes, [PLAN])
        self.assertEqual(changed(before, capture(self.vault)), set(self.writes))
        self.assert_main_git()
        note_path.write_text(note_path.read_text().replace("## Actions\n", "## Actions\nRecent human notes.\n"))
        before = dict(before, **{FOLDER_NOTE: capture(self.vault)[FOLDER_NOTE]})
        result = self.returned(options, selected, {FOLDER_NOTE, CHILD_NOTE, PLAN, API, CHILD, PARENT})
        final = capture(self.vault)
        self.assertEqual(changed(before, final), {FOLDER_NOTE, CHILD_NOTE, PLAN, API, CHILD, PARENT})
        self.assertEqual(self.writes.count(stop_at), 1)
        self.assertEqual(final[CHILD].data.decode(), UNCHANGED + DESCRIBE)
        self.assertEqual(final[API].data.decode(), DELEGATE)
        self.assertNotIn(PARENT, final)
        for note in (FOLDER_NOTE, CHILD_NOTE):
            self.assert_note_preserved(before, final, note)
        self.assertEqual(final[ARCHIVE], before[ARCHIVE])
        self.assertEqual(final[CURRENT], before[CURRENT])
        self.assertEqual(final["Discussion.md"], before["Discussion.md"])
        self.assertEqual(json.loads(journal.read_text())["stage"], "finished")
        self.assertEqual(run_return(**options, conditional_writer=self.cas), result)
        self.assertEqual(len(self.writes), len(result["files"]))
        self.assertEqual([call[0] for call in self.calls],
                         ["planner", "coder_light", "reviewer", "coder", "reviewer", "reviewer"])

    def test_a7_return_blocks_ambiguous_note_and_external_link_before_writing(self):
        selected, coder, checks = self.modification()
        self.assertEqual(self.execute(FOLDER_NOTE, "modification", coder, worktree=selected).stage, "complete")
        options, _ = self.verify(FOLDER_NOTE, "modification", selected, checks)
        note = self.vault / FOLDER_NOTE
        original = note.read_text()
        note.write_text(original + "\n## Working context\nA second heading makes the update ambiguous.\n")
        blocked = capture(self.vault)
        result = run_return(**options, conditional_writer=self.cas)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("ambiguous", result["problems"][0])
        self.assertEqual(capture(self.vault), blocked)
        note.write_text(original)
        external = self.area / "outside.py"
        external.write_text("Keep external file.\n")
        (self.vault / API).symlink_to(external)
        blocked = capture(self.vault)
        result = run_return(**options, conditional_writer=self.cas)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("symlink", result["problems"][0])
        self.assertEqual(capture(self.vault), blocked)
        self.assertEqual(external.read_text(), "Keep external file.\n")
        self.assertEqual(self.writes, [])
        self.assertTrue(selected.path.exists())
        self.assert_main_git()

    def diagnosis(self, source, *, reproduce=None):
        def python(*argv):
            return {"argv": [sys.executable, *argv]}

        probe = (
            f"from runpy import run_path; run = run_path({source!r})['run']; "
            "assert run(42) != 42; assert run(42) - 42 == run(10) - 10; print('constant offset')"
        )
        return {
            "symptom": "run returns the wrong value", "cause": "run adds a constant offset",
            "reproduce": python("-c", reproduce or check_code(source)), "failure_exit": 1,
            "failure_marker": "wrong result", "probe": python("-c", probe), "probe_marker": "constant offset",
            "question": "Which input reproduces the wrong result?",
            "architecture_question": "Can the offset be removed from the public entry point?",
        }

    def interrupt_plan_publication(self, root, decision, tasks, updates):
        admin = Path(git(root, "rev-parse", "--absolute-git-dir"))
        lock = admin / "index.lock"
        try:
            with self.assertRaises(subprocess.CalledProcessError) as error:
                self.plan(decision, tasks, root=root, updates=updates, before_proposal=lock.touch)
            self.assertIn("index.lock", error.exception.stderr)
        finally:
            lock.unlink(missing_ok=True)
        return json.loads((admin / "theseus-bug-fix-plan.json").read_text())

    def test_a4_local_bug_fix_records_missing_evidence_red_green_and_escalation(self):
        write(self.vault, FILE, UNCHANGED.replace("return value", "return value - 1"))
        self.snapshot()
        before = capture(self.vault)
        inline = '\nif __name__ == "__main__":\n    assert run(42) == 42, "wrong result"\n'
        check = {"argv": [sys.executable, FILE]}
        local = LocalChange([FILE], f"{PYTHON} {FILE}", independent=True, single_block=True)
        arguments = dict(block_candidates=[FILE_NOTE], work="bug_fix", write_targets=[FILE],
                         requirements_ready=True, local=local)

        def case(problem="local-offset"):
            return Diagnostics(self.vault, ticket_id="KOL-41", problem=problem, block=FILE_NOTE,
                               files=[FILE], regression=FILE, regression_code=inline)

        self.assertEqual(route(FILE_NOTE, **arguments).stage, "diagnose")
        missing = case().run("diagnose", **self.diagnosis(FILE, reproduce="print('no reproduction')"))
        self.assertEqual(missing["status"], "needs_evidence")
        self.assertIn("Which input", missing["question"])
        with self.assertRaisesRegex(ValueError, "confirm the cause"):
            case().run("begin", command=check)
        self.assertEqual(case().run("diagnose", **self.diagnosis(FILE))["status"], "confirmed")
        self.assertEqual(route(FILE_NOTE, **arguments, diagnosis_confirmed=True).stage, "local")
        bad = before[FILE].data.decode()
        write(self.vault, FILE, bad + inline)
        self.assertEqual(case().run("begin", command=check, mandatory=[check])["status"], "ready_to_fix")
        write(self.vault, FILE, UNCHANGED + inline)
        self.assertEqual(case().run("finish")["status"], "verified")
        journal = self.vault / ".obsta-experiments/KOL-41/local-offset.json"
        events = json.loads(journal.read_text())["events"]
        self.assertEqual([events[i][field]["exit"] for i, field in enumerate(
            ("red", "red", "regression_before_fix", "regression_after_fix", "mandatory_check"))],
            [0, 1, 1, 0, 0])
        self.assertEqual(changed(before, capture(self.vault)) - {
            ".obsta-experiments", ".obsta-experiments/KOL-41", ".obsta-experiments/KOL-41/local-offset.json"
        }, {FILE})
        self.assertEqual((self.vault / FILE).read_text(), UNCHANGED + inline)

        write(self.vault, FILE, bad + inline)
        for attempt in range(1, 4):
            self.assertEqual(case("persistent-offset").run("diagnose", **self.diagnosis(FILE))["status"],
                             "confirmed")
            self.assertEqual(case("persistent-offset").run("begin", command=check)["status"], "ready_to_fix")
            write(self.vault, FILE, UNCHANGED.replace("return value", f"return value - {attempt + 1}") + inline)
            state = case("persistent-offset").run("finish")
            self.assertEqual(state["failed_fixes"], attempt)
        self.assertEqual(state["status"], "architecture")
        self.assertIn("offset", state["question"])
        self.assertEqual(case("persistent-offset").run("diagnose", **self.diagnosis(FILE)), state)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.vault / ".worktrees").exists())
        self.assert_main_git()

    def test_a4_planned_bug_fix_runs_from_clean_worktree_plan_and_returns_partial_specification(self):
        write(self.vault, CHILD, UNCHANGED.replace("return value", "return value - 1"))
        self.snapshot()
        base = capture(self.vault)
        regression = {"argv": [sys.executable, REGRESSION]}
        check = f"{PYTHON} {REGRESSION}"
        parent_check = command(check_code(API))
        case = Diagnostics(self.vault, ticket_id=self.identity["ticket_id"], problem="wrong-result", block=FOLDER_NOTE,
                           files=[API, CHILD, REGRESSION], regression=REGRESSION)
        self.assertEqual(case.run("diagnose", **self.diagnosis(CHILD))["status"], "confirmed")
        outcome = case.run("handoff", slug=self.identity["slug"], snapshot_a=self.snapshot_a,
                           inputs=[], execute_after_plan=True)
        self.assertEqual(outcome["status"], "plan")
        root = Path(outcome["worktree"]["path"])
        selected = self.prepare([])
        self.assertEqual(selected.path, root)
        self.assertFalse((root / ".obsta-experiments").exists())
        before = capture(self.vault)
        self.assertNotIn(PLAN, before)
        decision = Decision(**outcome["decision"])
        tasks = [task([CHILD, REGRESSION], check), task([API], parent_check, depends=[1])]
        updates = {ACCEPTANCE: "The parent must return the input unchanged."}
        setup = self.interrupt_plan_publication(root, decision, tasks, updates)
        self.assertEqual(git(root, "rev-parse", "HEAD"), selected.base)
        self.assertNotIn(PLAN, capture(root))
        self.assertEqual((setup["base"], setup["block"], setup["plan"]), (selected.base, FOLDER_NOTE, PLAN))
        planned = self.plan(decision, tasks, root=root, updates=updates)
        self.assertEqual(planned.stage, "execute")
        self.assertEqual(git(root, "rev-parse", "HEAD"), setup["commit"])
        self.assertEqual(self.plan(decision, tasks, root=root, updates=updates), planned)
        self.assertEqual([call[0] for call in self.calls], ["planner"])
        self.assertEqual(capture(self.vault), before)
        self.assertEqual(git(root, "status", "--porcelain"), "")
        self.assertEqual(git(root, "rev-parse", "HEAD^"), selected.base)
        self.assertEqual(set(git(root, "diff", "--name-only", selected.base, "HEAD").splitlines()),
                         {FOLDER_NOTE, PLAN})

        def add_regression(cwd):
            write(cwd, REGRESSION, check_code(CHILD) + "\n")
            self.assertEqual(case.run("begin", command=regression, mandatory=[regression])["status"],
                             "ready_to_fix")

        def finish_diagnosis(_cwd):
            self.assertEqual(case.run("finish")["status"], "verified")

        edits = {
            1: self.code({CHILD: UNCHANGED, REGRESSION: check_code(CHILD) + "\n"},
                         before_red=add_regression, after_green=finish_diagnosis),
            2: self.code({API: DELEGATE}),
        }

        def coder(cwd, brief, paths):
            return edits[brief["number"]](cwd, brief, paths)

        self.assertEqual(self.execute(FOLDER_NOTE, "bug_fix", coder, worktree=selected).stage, "complete")
        evidence = json.loads((self.vault / JOURNAL).read_text())["events"]
        self.assertEqual([evidence[i][field]["exit"] for i, field in enumerate(
            ("red", "regression_before_fix", "regression_after_fix", "mandatory_check"))], [1, 1, 0, 0])
        note = self.vault / FOLDER_NOTE
        note.write_text(note.read_text().replace("Expose a Python function", "Document the public Python function"))
        before_return = capture(self.vault)
        options, _ = self.verify(FOLDER_NOTE, "bug_fix", selected,
                                 {FOLDER_NOTE: manifest(parent_check, tests=[check]), CHILD_NOTE: manifest(check)})
        self.returned(options, selected, {FOLDER_NOTE, CHILD_NOTE, PLAN, API, CHILD, REGRESSION})
        final = capture(self.vault)
        self.assertEqual(changed(before_return, final), {FOLDER_NOTE, CHILD_NOTE, PLAN, API, CHILD, REGRESSION})
        self.assert_note_preserved(base, final, FOLDER_NOTE, specification=False)
        self.assert_note_preserved(base, final, CHILD_NOTE)
        self.assertIn(b"Document the public Python function", read_section(final[FOLDER_NOTE].data, ARCHITECTURE,
                                                                           root_level=2))
        self.assertIn(b"return the input unchanged", read_section(final[FOLDER_NOTE].data, ACCEPTANCE, root_level=2))
        self.assertEqual(final[REGRESSION].data.decode(), check_code(CHILD) + "\n")
        self.assertEqual([call[0] for call in self.calls],
                         ["planner", "coder_light", "reviewer", "coder_light", "reviewer", "reviewer"])
        self.assertEqual(self.calls[0][1], root)

    def test_a4_planned_bug_fix_rejects_replacement_setup_with_the_same_subject(self):
        selected = self.prepare([FOLDER_NOTE])
        root = selected.path
        check = command(check_code(API))
        intent = route(FOLDER_NOTE, block_candidates=[FOLDER_NOTE], work="bug_fix", requirements_ready=True,
                       diagnosis_confirmed=True)
        setup = self.interrupt_plan_publication(root, intent, [task([API], check)], {})
        planned = git(root, "show", f"{setup['commit']}:{PLAN}")
        write(root, PLAN, planned.replace("Perform the selected implementation change", "Replace the task") + "\n")
        git(root, "add", "--", PLAN)
        subject = git(root, "show", "-s", "--format=%s", setup["commit"])
        git(root, "-c", "commit.gpgsign=false", "commit", "-qm", subject)
        self.assertEqual(git(root, "rev-parse", "HEAD^"), selected.base)
        with self.assertRaisesRegex(ValueError, "outside the current worktree history"):
            self.execute(FOLDER_NOTE, "bug_fix", self.code({API: DELEGATE}), worktree=selected)
        self.assertEqual([call[0] for call in self.calls], ["planner"])
        self.assert_main_git()

    def refactoring_scenario(self, choice):
        old = DELEGATE.replace("    return run_path", "    child_path = Path(__file__).with_name('child.py')\n"
                               "    return run_path").replace("str(Path(__file__).with_name('child.py'))",
                                                             "str(child_path)")
        write(self.vault, PARENT, old)
        self.snapshot()
        before = capture(self.vault)
        args = dict(block_candidates=[FOLDER_NOTE], work="refactoring", requirements_ready=True)
        self.assertEqual(route(FOLDER_NOTE, **args).stage, "clarify")
        self.assertEqual(capture(self.vault), before)
        self.assertFalse((self.vault / ".worktrees").exists())
        accepted = route(FOLDER_NOTE, **args, refactor_confirmed=True, execute_after_plan=True)
        self.assertEqual(accepted.stage, "prepare_refactoring")
        check = command(check_code(PARENT))
        response = {
            "goal": "Simplify the adapter while preserving the child.",
            "specification": [
                {"note": FOLDER_NOTE, "section": ARCHITECTURE, "text": "The adapter calls the existing child."},
                {"note": FOLDER_NOTE, "section": ACCEPTANCE, "text": "Verify the adapter on a positive input."},
            ],
            "tasks": [task([PARENT], check)],
            "external_dependencies": [],
        }

        def planner(cwd, brief, _paths):
            state = capture(cwd)
            self.assertNotIn(PARENT, state)
            self.assertNotIn(CURRENT, state)
            self.assertNotIn(ARCHIVE, state)
            self.assertEqual(state[CHILD], before[CHILD])
            self.assertEqual(state[CHILD_NOTE], before[CHILD_NOTE])
            self.assertEqual(read_section(state[FOLDER_NOTE].data, "Specification", root_level=2).strip(), b"")
            self.assertEqual(read_section(state[FOLDER_NOTE].data, "Working context", root_level=2).strip(), b"")
            self.assert_note_preserved(before, state, FOLDER_NOTE, specification=False)
            self.assertEqual(brief["approved_implementation_files"], [PARENT])
            self.assertEqual(capture(self.vault), before)
            return RoleResult("success", json.dumps(response))

        prepared = run_refactoring_planning(
            self.vault, accepted, requested=True, confirmed=True, **self.identity,
            ticket="Confirmed cleanup and authorized execution.", snapshot_a=self.snapshot_a,
            plan_path=PLAN, files=[PARENT], settings=SETTINGS, runner=self.roles({"planner": planner}),
        )
        selected = prepared.worktree
        self.assertEqual(prepared.planning.stage, "execute")
        self.assertEqual(capture(self.vault), before)
        self.assertEqual(git(selected.path, "rev-parse", "HEAD"), prepared.execution_base)
        self.assertEqual(git(selected.path, "rev-parse", "HEAD^"), prepared.cleanup)
        candidate = DELEGATE if choice == "new" else DELEGATE.replace("['run'](value)", "['run'](abs(value))")
        self.assertEqual(self.execute(FOLDER_NOTE, "refactoring", self.code({PARENT: candidate}),
                                      worktree=selected).stage, "complete")
        options, _ = self.verify(FOLDER_NOTE, "refactoring", selected, {FOLDER_NOTE: manifest(check)})
        head = options["accepted_head"]

        def evaluator(cwd, brief, _paths):
            self.assertEqual({item.name for item in cwd.iterdir()}, {selected.base, head})
            self.assertEqual((cwd / selected.base / PARENT).read_text(), old)
            self.assertEqual((cwd / head / PARENT).read_text(), candidate)
            self.assertEqual((cwd / head / CHILD).read_bytes(), before[CHILD].data)
            return RoleResult("success", json.dumps({
                "typical_change": {selected.base: "Update the intermediate binding and call.",
                                   head: "Update the call directly."},
                "benefit": {"revision": head, "check": "maintain", "explanation": "The binding is removed."},
                "concerns": [], "dispute": None,
            }))

        evaluation = Evaluation(self.vault, **self.identity, block=FOLDER_NOTE, plan=PLAN, settings=SETTINGS,
                                runner=self.roles({"evaluator": evaluator}))
        negative = check_code(PARENT, "-42").replace("run(42)", "run(-42)")
        checks = [
            {"id": "identity", "kind": "common", "obligation": "delegate",
             "argv": [sys.executable, "-c", negative]},
            {"id": "acceptance", "kind": "acceptance", "argv": [sys.executable, "-c", check_code(PARENT)]},
            {"id": "maintain", "kind": "typical_change", "argv": [
                sys.executable, "-c", f"from pathlib import Path; print(len(Path({PARENT!r}).read_text().splitlines()))"
            ]},
        ]
        obligations = [{"id": "delegate", "source": FOLDER_NOTE + "#User requirements",
                        "behavior": "The delegate returns negative values unchanged."}]

        def evaluate(commands):
            basis = evaluation.prepare(accepted_head=head, goal="Simplify Python delegation.",
                                       obligations=obligations, checks=commands, code=[PARENT], tests=[])
            return evaluation.evaluate(basis_id=basis["basis_id"])

        if choice == "old":
            missing = evaluate([*checks, {"id": "missing", "kind": "common", "obligation": "delegate",
                                          "argv": ["scenario-no-such-runtime"]}])
            self.assertIn("missing unavailable", missing["response"])
            detail = json.loads((self.vault / missing["report_path"]).read_text())
            self.assertEqual(detail["evidence"]["missing"][head]["status"], "unavailable")
        result = evaluate(checks)
        self.assertEqual(result["status"], "await_choice")
        self.assertEqual(result["recommendation"], choice)
        report_path = self.vault / result["report_path"]
        detail = json.loads(report_path.read_text())
        self.assertEqual(detail["evidence"]["acceptance"][head]["status"], "pass")
        self.assertEqual(detail["evidence"]["identity"][head]["status"], "pass" if choice == "new" else "fail")
        self.assertLess(detail["volumes"][head]["code"]["lines"], detail["volumes"][selected.base]["code"]["lines"])
        self.assertEqual(detail["pins"]["old"], selected.base)
        self.assertEqual(detail["pins"]["new"], head)
        report_id = result["report_id"]
        action = "return_new" if choice == "new" else "discard_new"
        while_main = capture(self.vault)
        self.assertEqual({name for name in changed(before, while_main) if not name.startswith(".obsta-experiments")},
                         set())
        blocked = run_return(**options, action=action, report_id=report_id, conditional_writer=self.cas)
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("evaluation artifact", blocked["problems"][0])
        self.assertEqual(capture(self.vault), while_main)
        self.assertEqual(self.writes, [])
        self.assertTrue(selected.path.exists())
        choice_note = self.vault / FOLDER_NOTE
        reply = f"{choice} {report_id}"
        choice_note.write_text(choice_note.read_text().replace("*⏳ Обрабатывается…*",
                                                               f"*⏳ Обрабатывается…*\n{reply}\n"))
        self.assertEqual(evaluation.select(reply=reply)["action"], action)
        live = capture(self.vault)
        if choice == "new":
            self.returned(dict(options, action=action, report_id=report_id), selected, {FOLDER_NOTE, PARENT, PLAN})
            self.assertEqual((self.vault / PARENT).read_text(), candidate)
        else:
            discarded = run_return(**options, action=action, report_id=report_id, conditional_writer=self.cas)
            self.assertEqual(discarded["status"], "discarded", discarded)
            self.assertEqual(discarded["files"], [])
            self.assertFalse(selected.path.exists())
            self.assertEqual(capture(self.vault), live)
            self.assertEqual((self.vault / PARENT).read_text(), old)
        final = capture(self.vault)
        self.assert_note_preserved(live, final, FOLDER_NOTE, specification=choice == "old")
        for path in (CHILD, CHILD_NOTE, CURRENT, ARCHIVE):
            self.assertEqual(final[path], before[path])
        self.assertTrue(report_path.is_file())
        self.assert_main_git()
        self.assertEqual([call[0] for call in self.calls], ["planner", "coder_light", "reviewer", "reviewer"]
                         + ["evaluator"] * (1 if choice == "new" else 2))

    def test_a5_refactoring_detects_hidden_loss_and_honors_choice_to_keep_old(self):
        self.refactoring_scenario("old")

    def test_a5_refactoring_publishes_recommended_new_version_only_after_choice(self):
        self.refactoring_scenario("new")

    def review_case(self):
        decision = route(FOLDER_NOTE, block_candidates=[FOLDER_NOTE], work="modification",
                         requirements_ready=True, execute_after_plan=True)
        check = command(check_code(API))
        self.plan(decision, [task([API], check)], updates={})
        return self.prepare([FOLDER_NOTE, PLAN]), check

    def test_a6_interrupted_role_and_blocking_review_keep_progress_and_resume_in_same_worktree(self):
        selected, check = self.review_case()
        before = capture(self.vault)
        plan_file = selected.path / PLAN
        candidate_code = DELEGATE.replace("['run'](value)", "['run'](abs(value))")
        first = self.code({API: candidate_code})

        def interrupt(cwd, brief, paths):
            first(cwd, brief, paths)
            raise RoleExecutionError("Interrupted after committing the candidate")

        if_abs = {"id": "sign-loss", "severity": "blocker", "detail": "The public delegate strips the input sign."}

        def reviewer(cwd, brief, paths):
            if brief["round"] == 1:
                self.assertIn("abs(value)", brief["diff"])
                return self.review(cwd, brief, paths, findings=[if_abs])
            self.assertEqual(brief["blockers"], [if_abs])
            self.assertNotIn("abs(value)", brief["diff"])
            disposition = {"id": "sign-loss", "status": "fixed", "detail": "The original input is passed to the child."}
            return self.review(cwd, brief, paths, dispositions=[disposition])

        with self.assertRaisesRegex(RoleExecutionError, "Interrupted"):
            self.execute(FOLDER_NOTE, "modification", interrupt, worktree=selected, reviewer=reviewer)
        held = git(selected.path, "rev-parse", "HEAD")
        self.assertIn("*Running", plan_file.read_text())
        self.assertNotIn("*Done", plan_file.read_text())
        self.assertEqual(capture(self.vault), before)
        self.assertEqual(self.prepare([FOLDER_NOTE, PLAN]), selected)
        self.assertEqual(git(selected.path, "rev-parse", "HEAD"), held)
        independent = prepare_worktree(self.vault, **self.identity, independent=True)
        self.assertNotEqual((independent.path, independent.branch), (selected.path, selected.branch))
        self.assertEqual(independent.attempt, 2)
        self.assertEqual(independent.base, selected.base)
        self.assertNotIn(API, capture(independent.path))
        self.assertIn("*Pending*", (independent.path / PLAN).read_text())
        self.assertEqual(route(PLAN, plan=Plan(PLAN, FOLDER_NOTE, "modification", "interrupted"),
                               action="continue").stage, "resume")

        def no_redispatch(*_):
            self.fail("A recorded candidate must be reviewed without redispatch.")

        for _ in range(2):
            blocked = self.execute(FOLDER_NOTE, "modification", no_redispatch, worktree=selected, reviewer=reviewer)
            self.assertEqual((blocked.stage, blocked.candidate.commit), ("await_review", held))
        controller = ReviewController(selected.path, SETTINGS, "Meet the approved acceptance and preserve contracts.")
        self.assertEqual(controller.status(1)["rounds"], 1)
        self.assertEqual(controller.status(1)["blockers"], [if_abs])
        self.assertEqual(git(selected.path, "rev-parse", "HEAD"), held)
        self.assertNotIn("*Done", plan_file.read_text())

        # The caller-approved replacement changes only the rejected candidate commit.
        remediation = self.code({API: DELEGATE}, replace=True, before_red=lambda cwd: write(cwd, API, None))
        coder_brief = self.calls[1][-1]
        remediation_runner = self.roles({"coder_heavy": remediation})
        remediation_runner("coder_heavy", SETTINGS, cwd=selected.path, context=json.dumps(coder_brief),
                           task="Fix the confirmed sign regression.", allowed_paths=[API],
                           allowed_sections={}, checks=[check])
        fixed = self.execute(FOLDER_NOTE, "modification", no_redispatch, worktree=selected, reviewer=reviewer)
        self.assertEqual(fixed.stage, "complete")
        self.assertEqual(controller.status(1)["rounds"], 2)
        self.assertEqual(controller.status(1)["blockers"], [])
        self.assertIn("*Done", plan_file.read_text())

        good_checks = {FOLDER_NOTE: manifest(check)}
        stale = run_verification(**self.options(FOLDER_NOTE, "modification"), accepted_head=held,
                                 checks=good_checks, settings=SETTINGS, runner=self.roles({"reviewer": self.review}))
        self.assertEqual(stale["status"], "blocked")
        self.assertIn("revision changed", stale["problems"][0])
        for bad_check, expected in [("false", "exit 1"), ("scenario-no-such-verification-command", "exit 127")]:
            options, report = self.verify(FOLDER_NOTE, "modification", selected,
                                          {FOLDER_NOTE: manifest(check, mandatory=[bad_check])}, status="blocked")
            self.assertIn(expected, report["problems"][0])
            self.assertEqual(report["checks"][-1]["revision"], options["accepted_head"])
            blocked = run_return(**options, conditional_writer=self.cas)
            self.assertEqual(blocked["status"], "blocked")
            self.assertTrue(selected.path.exists())
            self.assertEqual(capture(self.vault), before)
        options, report = self.verify(FOLDER_NOTE, "modification", selected, good_checks)
        reviewer_brief = self.calls[-1][-1]
        self.assertEqual(reviewer_brief["head"], options["accepted_head"])
        self.assertEqual(reviewer_brief["diff_sha256"], report["version"]["diff_sha256"])
        self.assertNotIn("abs(value)", reviewer_brief["diff"])
        self.returned(options, selected, {FOLDER_NOTE, PLAN, API})
        self.assertEqual([call[0] for call in self.calls],
                         ["planner", "coder_light", "reviewer", "coder_heavy", "reviewer", "reviewer"])
        self.assertTrue(independent.path.exists())

    def test_a6_scope_violation_stays_unaccepted_even_on_retry(self):
        selected, check = self.review_case()
        before = capture(self.vault)
        poison = self.code({API: DELEGATE}, foreign=True)
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "Discussion.md: not writable"):
                self.execute(FOLDER_NOTE, "modification", poison, worktree=selected)
            self.assertIn("*Running", (selected.path / PLAN).read_text())
            self.assertNotIn("*Done", (selected.path / PLAN).read_text())
        options, report = self.verify(FOLDER_NOTE, "modification", selected, {FOLDER_NOTE: manifest(check)},
                                      status="blocked")
        self.assertIn("unfinished", report["problems"][0])
        self.assertEqual(run_return(**options, conditional_writer=self.cas)["status"], "blocked")
        self.assertEqual(capture(self.vault), before)
        self.assertEqual(self.writes, [])
        self.assertEqual([call[0] for call in self.calls], ["planner", "coder_light"])
        self.assert_main_git()


if __name__ == "__main__":
    unittest.main()
