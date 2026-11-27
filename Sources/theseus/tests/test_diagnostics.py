import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diagnostics import Diagnostics
from worktree import prepare_worktree


BLOCK = "Sources/demo.md"
CODE = "Sources/demo/calculator.py"
TEST = "Sources/demo/tests/regression.py"
EXPERIMENT = ".obsta-experiments/KOL-102/1"
SYMPTOM = "adding two to one yields zero"
QUESTION = "Which input and invocation can reproduce the wrong sum?"
ARCHITECTURE = "Should calculator.py separate delta validation from summation?"
FAILURE = "wrong sum"


def python(*arguments):
    return {"argv": [sys.executable, *arguments]}


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.write(BLOCK, "## User requirements\nAdd numbers.\n")
        self.write(CODE, "def add(first, delta):\n    return first - delta + 1\n")
        self.write("other.txt", "human text")

    def write(self, path, text, root=None):
        file = (root or self.root) / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")

    def diagnose(self, **overrides):
        self.write(f"{EXPERIMENT}/reproduce.py", self.regression_source())
        args = {
            "symptom": SYMPTOM, "cause": "add subtracts delta instead of adding it",
            "reproduce": python(str(self.root / EXPERIMENT / "reproduce.py")),
            "failure_exit": 1, "failure_marker": FAILURE,
            "probe": python("-c",
                            "import sys; sys.path.insert(0, 'Sources/demo'); from calculator import add; "
                            "assert add(1, 2) < add(1, 1); print('delta is subtracted')"),
            "probe_marker": "delta is subtracted", "question": QUESTION,
            "architecture_question": ARCHITECTURE,
        }
        return self.case().run("diagnose", **(args | overrides))

    def case(self, **overrides):
        args = {"root": self.root, "ticket_id": "KOL-102/1", "problem": "wrong-sum",
                "block": BLOCK, "regression": TEST, "files": [CODE, TEST]}
        return Diagnostics(**(args | overrides))

    def regression_source(self):
        return ("import sys\nfrom pathlib import Path\n"
                "sys.path.insert(0, str(Path.cwd() / 'Sources/demo'))\n"
                "from calculator import add\n"
                "assert add(1, 2) == 3, 'wrong sum'\n")

    def journal(self):
        return json.loads((self.root / EXPERIMENT / "wrong-sum.json").read_text())

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd or self.root, text=True).strip()

    def snapshot(self):
        self.git("init", "-q")
        self.git("config", "user.name", "Diagnostics tests")
        self.git("config", "user.email", "diagnostics@example.test")
        self.git("add", ".")
        self.git("commit", "-qm", "Snapshot A")
        return self.git("rev-parse", "HEAD")

    def test_local_checks_red_before_edit_and_green_after(self):
        self.assertEqual(self.diagnose()["status"], "confirmed")
        self.write(TEST, self.regression_source())
        ready = self.case().run("begin", command=python(TEST), mandatory=[python(TEST)])
        self.assertEqual(ready["status"], "ready_to_fix")
        self.assertEqual(ready["files"], [CODE])
        self.write(CODE, "def add(first, delta):\n    return first + delta\n")
        self.assertEqual(self.case().run("finish")["status"], "verified")
        events = self.journal()["events"]
        self.assertEqual([event.get("exit") for event in [
            events[0]["red"], events[0]["probe"], events[1]["regression_before_fix"],
            events[2]["regression_after_fix"], events[3]["mandatory_check"],
        ]], [1, 0, 1, 0, 0])
        self.assertFalse((self.root / ".worktrees").exists())
        self.assertEqual((self.root / TEST).read_text(), self.regression_source())

    def test_requires_specific_reproduction_and_causal_probe(self):
        for overrides in ({"reproduce": python("-c", "raise ImportError('fixture unavailable')")},
                          {"probe": python("-c", "print('inconclusive')")}):
            with self.subTest(overrides=overrides):
                result = self.diagnose(**overrides)
                self.assertEqual(result["status"], "needs_evidence")
                self.assertEqual(result["question"], QUESTION)
                with self.assertRaisesRegex(ValueError, "confirm the cause"):
                    self.case().run("begin", command=python(TEST))
        self.assertEqual(self.journal()["failed_fixes"], 0)

    def test_dependent_work_hands_confirmed_bug_fix_to_verified_worktree(self):
        head = self.snapshot()
        index = (self.root / ".git/index").read_bytes()
        self.assertEqual(self.diagnose()["status"], "confirmed")
        plan = self.case().run("handoff", slug="calculator", snapshot_a=head, inputs=[],
                               execute_after_plan=True)
        self.assertEqual(plan["decision"]["stage"], "plan")
        self.assertEqual(plan["decision"]["work"], "bug_fix")
        self.assertTrue(plan["decision"]["execute_after_plan"])
        worktree = Path(plan["worktree"]["path"])
        self.assertEqual(self.git("branch", "--show-current", cwd=worktree),
                         "theseus/kol-102-1-calculator")
        self.assertEqual(self.case().run("handoff", slug="calculator", snapshot_a=head,
                                         inputs=[])["worktree"], plan["worktree"])
        self.assertFalse((worktree / EXPERIMENT).exists())
        self.write(TEST, self.regression_source(), root=worktree)
        self.assertEqual(self.case().run("begin", command=python(TEST))["root"], str(worktree))
        self.write(CODE, "def add(first, delta):\n    return first + delta\n", root=worktree)
        self.assertEqual(self.case().run("finish")["status"], "verified")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual((self.root / ".git/index").read_bytes(), index)
        self.assertFalse((self.root / TEST).exists())

    def test_blocks_handoff_when_source_drifted_in_main_or_prepared_worktree(self):
        snapshot = self.snapshot()
        self.diagnose()
        changed = "def add(first, delta):\n    return first - delta + 7\n"
        self.write(CODE, changed)
        args = dict(slug="calculator", snapshot_a=snapshot, inputs=[])
        self.assertEqual(self.case().run("handoff", **args)["status"], "rediagnose")
        self.assertFalse((self.root / ".worktrees").exists())
        self.diagnose()
        worktree = prepare_worktree(self.root, ticket_id="KOL-102/1", slug="calculator",
                                    snapshot_a=snapshot, inputs=[BLOCK, CODE, TEST])
        self.write(CODE, changed.replace("+ 7", "+ 8"), root=worktree.path)
        self.assertEqual(self.case().run("handoff", **args)["status"], "rediagnose")
        self.assertEqual(self.diagnose()["status"], "confirmed")
        self.assertEqual(self.case().run("handoff", **args)["status"], "plan")

    def test_failed_fixes_require_rediagnosis_and_escalate_on_third_retry(self):
        self.write(TEST, self.regression_source())
        for failure in range(1, 4):
            self.assertEqual(self.diagnose()["status"], "confirmed")
            self.case().run("begin", command=python(TEST))
            self.write(CODE, f"def add(first, delta):\n    return first - delta + {failure + 4}\n")
            result = self.case().run("finish")
            self.assertEqual(result["failed_fixes"], failure)
            if failure < 3:
                self.assertEqual(result["status"], "rediagnose")
                with self.assertRaisesRegex(ValueError, "confirm the cause"):
                    self.case().run("begin", command=python(TEST))
        self.assertEqual(result["status"], "architecture")
        self.assertEqual(result["question"], ARCHITECTURE)
        self.assertEqual(self.case().run("begin", command=python(TEST)), result)
        self.assertEqual(self.diagnose(), result)
        self.assertEqual(self.journal()["failed_fixes"], 3)

        with self.assertRaisesRegex(ValueError, "requires the user's architecture decision"):
            self.case().run("continue", authorized=False, approach="Separate validation and addition",
                            decision="Yes, split the two responsibilities.")
        resumed = self.case().run("continue", authorized=True, approach="Separate validation and addition",
                                  decision="Yes, split the two responsibilities.")
        self.assertEqual(resumed["status"], "rediagnose")
        self.assertEqual(resumed["failed_fixes"], 3)
        self.assertEqual(self.diagnose()["status"], "confirmed")
        self.case().run("begin", command=python(TEST))
        self.write(CODE, "def add(first, delta):\n    validated = delta\n    return first - validated + 9\n")
        retry = self.case().run("finish")
        self.assertEqual(retry["status"], "rediagnose")
        self.assertEqual(retry["failed_fixes"], 4)
        self.assertEqual(self.journal()["failures_before_approach"], 3)

    def test_inline_regression_stays_inside_single_file_block(self):
        note = "Sources/inline.py.md"
        source = "Sources/inline.py"
        bug = "def add(first, delta):\n    return first - delta\n"
        inline = "\nif __name__ == '__main__':\n    assert add(1, 2) == 3, 'wrong sum'\n"
        self.write(note, "## User requirements\nAdd numbers.\n")
        self.write(source, bug)
        case = self.case(problem="inline-sum", block=note, files=[source],
                         regression=source, regression_code=inline)
        result = case.run(
            "diagnose", symptom=SYMPTOM, cause="subtraction instead of addition",
            reproduce=python("-c",
                             "import sys; sys.path.insert(0, 'Sources'); from inline import add; "
                             "assert add(1, 2) == 3, 'wrong sum'"),
            failure_exit=1, failure_marker=FAILURE,
            probe=python("-c",
                         "import sys; sys.path.insert(0, 'Sources'); from inline import add; "
                         "assert add(1, 2) < add(1, 1); print('delta is subtracted')"),
            probe_marker="delta is subtracted", question=QUESTION, architecture_question=ARCHITECTURE,
        )
        self.assertEqual(result["status"], "confirmed")
        self.write(source, bug + inline)
        self.assertEqual(case.run("begin", command=python(source))["files"], [source])
        fixed = bug.replace("first - delta", "first + delta")
        self.write(source, fixed)
        with self.assertRaisesRegex(ValueError, "exact inline regression"):
            case.run("finish")
        self.write(source, fixed + inline)
        self.assertEqual(case.run("finish")["status"], "verified")

    def test_blocked_mandatory_check_preserves_attempt_for_verification(self):
        self.diagnose()
        self.write(TEST, self.regression_source())
        self.case().run("begin", command=python(TEST), mandatory=[python(
            "-c", "import sys; sys.path.insert(0, 'Sources/demo'); from calculator import add; "
            "assert add(3, 0) == 3, 'zero must be neutral'"
        )])
        self.write(CODE, "def add(first, delta):\n    return 0 if delta == 0 else first + delta\n")
        blocked = self.case().run("finish")
        self.assertEqual(blocked["status"], "verification_blocked")
        self.assertIn("zero must be neutral", blocked["evidence"]["output"])
        self.assertEqual(blocked["failed_fixes"], 0)
        self.write(CODE, "def add(first, delta):\n    return first + delta\n")
        self.assertEqual(self.case().run("finish")["status"], "verified")

    def test_refuses_wrong_paths_and_unchanged_or_rewritten_regression(self):
        with self.assertRaisesRegex(ValueError, "not writable"):
            self.case(files=[CODE, "other.txt"], regression="other.txt").run("finish")
        self.diagnose()
        self.write(TEST, self.regression_source())
        with self.assertRaisesRegex(ValueError, "must name the permanent test"):
            self.case().run("begin", command=python("-c", "exit(1)"))
        self.case().run("begin", command=python(TEST))
        with self.assertRaisesRegex(ValueError, "no implementation change"):
            self.case().run("finish")
        self.write("other.txt", "outside the task")
        with self.assertRaisesRegex(ValueError, "outside its exact implementation paths"):
            self.case().run("finish")
        self.write("other.txt", "human text")
        self.write(TEST, "pass\n")
        with self.assertRaisesRegex(ValueError, "regression changed"):
            self.case().run("finish")


if __name__ == "__main__":
    unittest.main()
