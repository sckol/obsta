"""Compare actual Git revisions with scripted evaluator roles and no model calls."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex import RoleResult
from evaluation import Evaluation, _recommend
from execution import run_execution
from planning import ACCEPTANCE, ARCHITECTURE
from refactoring import run_refactoring_planning
from routing import Decision


BLOCK = "Sources/engine.md"
PLAN = "Plans/refactor.md"
CODE = "Sources/engine/engine.py"
TEST = "Sources/engine/test_engine.py"
OPEN_TICKET = '#KOL-102/1🍏 Refactor engine.\n*⏳ Processing…*\n#KOL-102/1🛑'
OLD = "def transform(n):\n    cached = {}\n    cached[n] = abs(n)\n    return cached[n]\n"
NEW = "def transform(n):\n    return abs(n)\n"
COMMON = (
    "from pathlib import Path\n"
    "import sys\n"
    "from engine import transform\n"
    "assert not Path('check-write').exists()\n"
    "Path('check-write').write_text('local only')\n"
    "value = int(sys.stdin.read())\n"
    "print(transform(value))\n"
    "raise SystemExit(0 if transform(value) == abs(value) else 7)\n"
)
TYPICAL = (
    "from pathlib import Path\n"
    "text = Path('engine.py').read_text()\n"
    "assert not Path('check-write').exists()\n"
    "print('Edit the transform rule in engine.py; cache edits:', text.count('cached'))\n"
)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def write(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name).resolve()
        git(self.vault, "init", "-q")
        git(self.vault, "config", "user.name", "Evaluation test")
        git(self.vault, "config", "user.email", "test@example.invalid")
        write(self.vault, ".gitignore", "/.worktrees/\n/.obsta-experiments/\n")
        write(self.vault, BLOCK, "## User requirements\n\nNegative inputs produce their magnitude.\n\n"
              f"{OPEN_TICKET}\n\n"
              "## Specification\n\n### Acceptance\nTest both signs.\n\n## Working context\n\nOld context.\n")
        write(self.vault, CODE, OLD)
        write(self.vault, TEST, "from engine import transform\nassert transform(-5) == 5\nassert transform(4) == 4\n")
        git(self.vault, "add", ".")
        git(self.vault, "commit", "-qm", "Baseline")
        self.main = git(self.vault, "rev-parse", "HEAD")
        self.index = (self.vault / ".git/index").read_bytes()
        self.calls = []
        self.approval = "confirmed"
        self.benefit = True

    def planner(self, role, settings, **kwargs):
        self.assertEqual(role, "planner")
        return RoleResult("success", json.dumps({
            "goal": "Simplify engine.",
            "specification": [{"note": BLOCK, "section": ARCHITECTURE, "text": "Simple transformation."},
                              {"note": BLOCK, "section": ACCEPTANCE, "text": "Positive inputs work."}],
            "tasks": [{
                "description": "Reimplement engine", "files": [CODE, TEST], "coder": "coder",
                "complexity": "design", "depends_on": [],
                "checks": [{"command": f"python3 {TEST}", "cwd": ".", "setup": "None", "expected": "Pass"}],
            }],
            "external_dependencies": [],
        }))

    def evaluator(self, role, settings, *, cwd, task, context, **options):
        self.assertEqual(role, "evaluator")
        self.assertTrue(options["read_only"])
        self.assertEqual(options["allowed_paths"], [])
        self.assertEqual(options["allowed_sections"], {})
        self.assertEqual(settings, {"theseus.evaluator": "fake/low"})
        self.assertEqual(sorted(path.name for path in cwd.iterdir()), sorted([self.old, self.new]))
        self.assertEqual((cwd / self.old / CODE).read_text(encoding="utf-8"), OLD)
        self.assertFalse((cwd / self.new / "Sources/engine/check-write").exists())
        brief = json.loads(context)
        self.calls.append(brief)
        if "dispute" in brief:
            self.assertEqual(set(brief), {"dispute", "obligation", "check", "original", "repeated"})
            self.assertEqual(brief["original"], brief["repeated"])
            return RoleResult("success", json.dumps({
                "verdict": self.approval, "evidence": "The repeated probe returns the negative sign from new.",
            }))
        self.assertEqual(set(brief), {"goal", "obligations", "checks", "evidence", "volumes"})
        return RoleResult("success", json.dumps({
            "typical_change": {self.old: "Edit transform and cache handoff in engine.py; run test_engine.py.",
                               self.new: "Edit only transform in engine.py; run test_engine.py."},
            "benefit": ({"revision": self.new, "check": "edit-rule",
                         "explanation": "The cache edit is unnecessary; one expression owns the rule."}
                        if self.benefit else None),
            "concerns": [],
            "dispute": None,
        }))

    def prepare(self, *, code=NEW, missing=False, acceptance_missing=False, acceptance_failure=False):
        self.worktree = run_refactoring_planning(
            self.vault, Decision("prepare_refactoring", BLOCK, "refactoring"), requested=True,
            confirmed=True, ticket_id="KOL-102/1", slug="engine", ticket="Refactor the engine.",
            plan_path=PLAN, snapshot_a=self.main, files=[CODE, TEST], settings={}, runner=self.planner,
        ).worktree
        self.old = self.worktree.base
        def coder(role, settings, *, cwd, context, checks, **options):
            write(cwd, CODE, code)
            write(cwd, TEST, "from engine import transform\nassert transform(4) == 4\n")
            git(cwd, "add", CODE, TEST)
            red = {"command": checks[0], "cwd": ".", "exit": 1, "observed": "test entry does not exist"}
            green = {"command": checks[0], "cwd": ".", "exit": 0, "observed": "positive test passes"}
            git(cwd, "commit", "-qm", json.loads(context)["commit_subject"],
                "-m", "Theseus-evidence: " + json.dumps({"red": red, "green": [green]}))
            return RoleResult("success", "Implemented")

        executed = run_execution(self.vault, ticket_id="KOL-102/1", slug="engine", block=BLOCK, plan=PLAN,
                                 work="refactoring", settings={}, runner=coder, review=lambda candidate: True)
        self.assertEqual(executed.stage, "complete")
        self.new = git(self.worktree.path, "rev-parse", "HEAD")
        self.case = dict(vault=self.vault, ticket_id="KOL-102/1", slug="engine",
                         block=BLOCK, plan=PLAN, settings={"theseus.evaluator": "fake/low"})
        self.evaluation = Evaluation(**self.case, runner=self.evaluator)
        source = BLOCK + "#User requirements"
        obligations = [{"id": "negative-sign", "source": source, "behavior": "transform(-17) returns 17"}]
        checks = [
            {"id": "common-sign", "kind": "common", "obligation": "negative-sign",
             "argv": [sys.executable, "-c", COMMON], "cwd": "Sources/engine", "stdin": "-17\n",
             "failure_exits": [7]},
            {"id": "version-acceptance", "kind": "acceptance", "argv": [sys.executable, TEST]},
            {"id": "edit-rule", "kind": "typical_change", "argv": [sys.executable, "-c", TYPICAL],
             "cwd": "Sources/engine"},
        ]
        if acceptance_failure:
            checks.append({"id": "extended-acceptance", "kind": "acceptance", "for_revision": self.new,
                           "argv": [sys.executable, "-c", "raise SystemExit(3)"], "failure_exits": [3]})
        if acceptance_missing:
            checks.append({"id": "missing-acceptance", "kind": "acceptance", "for_revision": self.new,
                           "argv": ["no-such-acceptance-command"]})
        if missing:
            obligations.append({"id": "compatibility", "source": "Child contract", "behavior": "ABI remains stable"})
            checks.append({"id": "missing-harness", "kind": "common", "obligation": "compatibility",
                           "argv": ["no-such-evaluation-test-command"]})
        return self.evaluation.prepare(accepted_head=self.new, goal="Reduce maintenance locations.",
                                       obligations=obligations, checks=checks, code=[CODE], tests=[TEST])

    def evaluate(self, basis, **options):
        self.assertFalse(self.calls)
        result = self.evaluation.evaluate(basis_id=basis["basis_id"], **options)
        self.assertEqual(result["status"], "await_choice")
        self.assertTrue(result["response"].startswith("*"))
        path = self.vault / result["report_path"]
        self.assertTrue(path.is_relative_to(self.vault / ".obsta-experiments/KOL-102/1"))
        self.assertEqual(git(self.vault, "rev-parse", "HEAD"), self.main)
        self.assertEqual((self.vault / ".git/index").read_bytes(), self.index)
        self.assertEqual((self.vault / CODE).read_text(), OLD)
        self.assertEqual(git(self.worktree.path, "rev-parse", "HEAD"), self.new)
        self.assertFalse(git(self.worktree.path, "status", "--porcelain"))
        return result, json.loads(path.read_text())

    def test_green_new_acceptance_can_hide_a_failed_common_obligation(self):
        result, detail = self.evaluate(self.prepare(code="def transform(n):\n    return n\n"))
        self.assertEqual(result["recommendation"], "old")
        self.assertIn("new common-sign fail", result["response"])
        self.assertEqual(detail["evidence"]["version-acceptance"][self.new]["status"], "pass")
        self.assertEqual(detail["evidence"]["common-sign"][self.new]["stdout"], "-17\n")
        self.assertEqual(detail["evidence"]["common-sign"][self.old]["stdout"], "17\n")
        self.assertEqual(detail["basis"]["checks"]["common-sign"]["stdin"], "-17\n")
        self.assertEqual(detail["basis"]["obligations"]["negative-sign"]["source"], BLOCK + "#User requirements")
        self.assertEqual(detail["pins"]["old"], self.old)
        self.assertEqual(detail["pins"]["new"], self.new)

    def test_smaller_equivalent_implementation_wins_with_both_label_orders(self):
        result, detail = self.evaluate(self.prepare())
        self.assertEqual(result["recommendation"], "new")
        self.assertEqual(detail["volumes"][self.old]["code"]["lines"], 4)
        self.assertEqual(detail["volumes"][self.new]["code"]["lines"], 2)
        self.assertEqual(detail["volumes"][self.old]["tests"]["lines"], 3)
        self.assertEqual(detail["volumes"][self.new]["tests"]["lines"], 2)
        for revisions in ([self.old, self.new], [self.new, self.old]):
            with self.subTest(revisions=revisions):
                revision, _reason = _recommend(revisions, detail["basis"]["checks"], detail["evidence"],
                                               detail["assessment"], None)
                self.assertEqual(revision, self.new)

    def test_unavailable_mandatory_check_blocks_recommendation_and_is_preserved(self):
        result, detail = self.evaluate(self.prepare(missing=True))
        self.assertIsNone(result["recommendation"])
        self.assertIn("old missing-harness unavailable", result["response"])
        self.assertIn("new missing-harness unavailable", result["response"])
        for item in detail["evidence"]["missing-harness"].values():
            self.assertEqual(item["status"], "unavailable")
            self.assertIn("no-such-evaluation-test-command", item["error"])

    def test_unavailable_applicable_acceptance_is_reported_and_blocks_recommendation(self):
        result, detail = self.evaluate(self.prepare(acceptance_missing=True))
        self.assertIsNone(result["recommendation"])
        self.assertIn("new missing-acceptance unavailable", result["response"])
        self.assertEqual(detail["basis"]["checks"]["missing-acceptance"]["for_revision"], self.new)

    def test_deciding_dispute_is_reproduced_and_reviewed_independently(self):
        basis = self.prepare(code="def transform(n):\n    return n\n")
        dispute = {"check": "common-sign", "revision": self.new, "explanation": "The negative result is disputed."}
        result, detail = self.evaluate(basis, dispute=dispute)
        self.assertEqual(result["recommendation"], "old")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(detail["confirmation"]["verdict"], "confirmed")
        self.assertEqual(detail["confirmation"]["repeated"][self.new]["status"], "fail")
        self.approval = "rejected"
        updated = self.evaluation.evaluate(basis_id=basis["basis_id"], dispute=dispute)
        self.assertIsNone(updated["recommendation"])
        self.assertNotEqual(updated["report_id"], result["report_id"])

    def test_applicable_acceptance_failure_can_be_the_deciding_dispute(self):
        basis = self.prepare(acceptance_failure=True)
        dispute = {"check": "extended-acceptance", "revision": self.new, "explanation": "Exit 3 is disputed."}
        result, detail = self.evaluate(basis, dispute=dispute)
        self.assertEqual(result["recommendation"], "old")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(detail["confirmation"]["verdict"], "confirmed")
        self.assertEqual(detail["confirmation"]["repeated"][self.new]["exit"], 3)

    def _selection(self, label, action, wrong_action):
        result, _detail = self.evaluate(self.prepare())
        report_id = result["report_id"]
        with self.assertRaisesRegex(ValueError, "missing or invalid evaluation artifact"):
            self.evaluation.validate_choice(report_id=report_id, action=action)
        with self.assertRaisesRegex(ValueError, "exact"):
            self.evaluation.select(reply=label)
        reply = f"{label} {report_id}"
        note = self.vault / BLOCK
        text = note.read_text().replace("*⏳ Processing…*", f"*◇ Both versions compared.*\n{reply}")
        note.write_text(text)
        choice = self.evaluation.select(reply=reply)
        self.assertEqual(choice["action"], action)
        self.assertEqual(choice["revision"], self.old if label == "old" else self.new)
        reopened = Evaluation(**self.case)
        self.assertEqual(reopened.validate_choice(report_id=report_id, action=action)["reply"], reply)
        with self.assertRaisesRegex(ValueError, "does not match"):
            reopened.validate_choice(report_id=report_id, action=wrong_action)
        other = "old" if label == "new" else "new"
        with self.assertRaisesRegex(ValueError, "different user choice"):
            reopened.select(reply=f"{other} {report_id}")
        self.assertTrue(self.worktree.path.exists())
        return report_id

    def test_old_choice_authorizes_only_discard(self):
        self._selection("old", "discard_new", "return_new")

    def test_new_choice_authorizes_only_return_and_rechecks_actual_head(self):
        report_id = self._selection("new", "return_new", "discard_new")
        git(self.worktree.path, "commit", "--allow-empty", "-qm", "Candidate changed")
        with self.assertRaisesRegex(ValueError, "stale evaluation"):
            self.evaluation.validate_choice(report_id=report_id, action="return_new")
        with self.assertRaisesRegex(ValueError, "stale evaluation"):
            self.evaluation.select(reply=f"new {report_id}")

    def test_live_old_implementation_and_scope_are_rechecked(self):
        report_id = self._selection("old", "discard_new", "return_new")
        with self.assertRaisesRegex(ValueError, "selected refactoring scope"):
            Evaluation(**dict(self.case, block="Sources/another.md")).validate_choice(
                report_id=report_id, action="discard_new")
        for identity in (dict(self.case, attempt=2), dict(self.case, ticket_id="KOL-102/2")):
            with self.assertRaisesRegex(ValueError, "not owned"):
                Evaluation(**identity)
        note = self.vault / BLOCK
        note.write_text(note.read_text().replace("produce their magnitude", "return unchanged"))
        with self.assertRaisesRegex(ValueError, "User requirements differ"):
            self.evaluation.validate_choice(report_id=report_id, action="discard_new")
        note.write_text(note.read_text().replace("return unchanged", "produce their magnitude"))
        write(self.vault, CODE, OLD + "# live edit\n")
        with self.assertRaisesRegex(ValueError, "stale evaluation"):
            self.evaluation.validate_choice(report_id=report_id, action="discard_new")


if __name__ == "__main__":
    unittest.main()
