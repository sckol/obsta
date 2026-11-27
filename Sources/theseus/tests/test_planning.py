import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex import RoleResult
from planning import run_planning
from routing import LocalChange, Plan, route


BLOCK = "Sources/example.md"
PLAN = "Plans/example.md"
PATH = "Sources/example/main.py"
NOTE = (
    "---\r\nTemplate: Building block\r\n---\r\n"
    "## User requirements\r\nHuman requirement.\r\n"
    "## Specification\r\n### Architecture and techniques\r\n*Old design.*\r\n"
    "### Acceptance\r\n*Existing check.*\r\n"
    "### Actions\r\n- [ ] Implement\r\n"
    "## Working context\r\nHuman notes.\r\n"
).encode()
SPEC = ("Specification", "Architecture and techniques")
ACCEPTANCE = ("Specification", "Acceptance")
CHECK = {"command": "python3 -m unittest", "cwd": ".", "setup": "No setup", "expected": "All tests pass"}


class PlanningTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "Sources").mkdir()
        (self.root / BLOCK).write_bytes(NOTE)
        self.calls = []
        self.proposal = {
            "goal": "Run the example.",
            "specification": [{"note": BLOCK, "section": list(SPEC), "text": "Use an example CLI."}],
            "tasks": [{"description": "Implement the CLI", "files": [PATH], "coder": "coder_light",
                       "complexity": "specified", "depends_on": [], "checks": [CHECK]}],
            "external_dependencies": [],
        }

    def planner(self, role, settings, **kwargs):
        self.calls.append((role, kwargs))
        return RoleResult("success", json.dumps(self.proposal))

    def run_stage(self, work="first", allowed=True, *, runner=None, **route_args):
        decision = route(BLOCK, block_candidates=[BLOCK], work=work, requirements_ready=allowed,
                         **route_args)
        selected = [SPEC]
        if work == "first":
            selected.append(ACCEPTANCE)
            self.proposal["specification"].append({
                "note": BLOCK, "section": list(ACCEPTANCE),
                "text": "From the vault, run python3 -m unittest. All tests pass.",
            })
        try:
            return run_planning(self.root, decision, ticket="Implement the example.", plan_path=PLAN,
                                sections={BLOCK: selected}, files=[PATH], runner=runner or self.planner)
        finally:
            if work == "first":
                self.proposal["specification"].pop()

    def test_first_plan_stays_in_main_vault_until_explicit_execution(self):
        result = self.run_stage()
        self.assertEqual(result.stage, "await_execution")
        self.assertEqual(self.calls[0][0], "planner")
        self.assertEqual(self.calls[0][1]["allowed_paths"], [])
        self.assertIn("Status:\n", (self.root / PLAN).read_text())
        self.assertIn("### Plan status\n\n*Ready*", (self.root / PLAN).read_text())
        self.assertIn(PATH, (self.root / PLAN).read_text())
        self.assertIn(CHECK["expected"], (self.root / PLAN).read_text())
        self.assertFalse((self.root / ".worktrees").exists())
        self.assertIn(b"*Use an example CLI.*\r\n", (self.root / BLOCK).read_bytes())
        self.assertIn(b"run python3 -m unittest", (self.root / BLOCK).read_bytes())

    def test_explicit_execution_handoff_has_validated_tasks(self):
        result = self.run_stage(execute_after_plan=True)
        self.assertEqual(result.stage, "execute")
        self.assertEqual(result.tasks[0].files, (PATH,))
        self.assertIn("*Ready*", (self.root / PLAN).read_text())

    def test_discussion_records_question_in_ticket_without_writing_plan(self):
        decision = route(BLOCK, block_candidates=[BLOCK], work="first")
        result = run_planning(self.root, decision, ticket="Build an example.",
                              question="Which command should the CLI expose?", runner=self.planner)
        self.assertEqual(result.stage, "await_decision")
        self.assertIn("Which command should the CLI expose?", result.response)
        self.assertEqual(self.calls, [])
        self.assertEqual((self.root / BLOCK).read_bytes(), NOTE)
        self.assertFalse((self.root / PLAN).exists())

    def test_partial_modification_preserves_other_section_and_user_status(self):
        self.run_stage(work="modification")
        updated = (self.root / BLOCK).read_bytes()
        self.assertEqual(updated, NOTE.replace(b"*Old design.*", b"*Use an example CLI.*"))
        plan = self.root / PLAN
        plan.write_bytes(plan.read_bytes().replace(b"Status:\n", b"Status: In progress\n"))
        decision = route(PLAN, plan=Plan(PLAN, BLOCK, "modification", "ready"), action="execute")
        result = run_planning(self.root, decision, ticket="Execute this plan.",
                              files=[PATH], runner=self.planner)
        self.assertEqual(result.stage, "execute")
        self.assertEqual(result.tasks[0].files, (PATH,))
        self.assertIn(b"Status: In progress\n", plan.read_bytes())
        self.assertEqual(len(self.calls), 1)

    def test_local_changes_are_reported_in_ticket_without_creating_plan(self):
        decision = route(BLOCK, block_candidates=[BLOCK], work="modification", requirements_ready=True,
                         write_targets=[PATH], local=LocalChange([PATH], "python3 -m unittest", True, True))
        result = run_planning(self.root, decision, ticket="Rename the item.", files=[PATH],
                              local_verification="python3 -m unittest", runner=self.planner)
        self.assertEqual(result.stage, "local")
        self.assertIn(PATH, result.response)
        self.assertIn("python3 -m unittest", result.response)
        self.assertFalse((self.root / PLAN).exists())

    def test_missing_first_acceptance_and_root_specification_update_are_rejected(self):
        decision = route(BLOCK, block_candidates=[BLOCK], work="first", requirements_ready=True)
        for selected, message in (([SPEC], "requires Architecture and techniques and Acceptance"),
                                  ([("Specification",)], "precise Specification subsections")):
            with self.subTest(selection=selected), self.assertRaisesRegex(ValueError, message):
                run_planning(self.root, decision, ticket="Implement example", plan_path=PLAN,
                             sections={BLOCK: selected}, files=[PATH], runner=self.planner)
        self.assertFalse((self.root / PLAN).exists())
        self.assertEqual((self.root / BLOCK).read_bytes(), NOTE)

    def test_later_execute_rechecks_acceptance_and_leaves_the_plan_untouched(self):
        self.run_stage()
        plan = (self.root / PLAN).read_bytes()
        note = self.root / BLOCK
        note.write_bytes(note.read_bytes().replace(
            b"*From the vault, run python3 -m unittest. All tests pass.*\r\n", b""))
        decision = route(PLAN, plan=Plan(PLAN, BLOCK, "first", "ready"), action="execute")
        with self.assertRaisesRegex(ValueError, "Acceptance must be populated"):
            run_planning(self.root, decision, ticket="Execute this plan", files=[PATH])
        self.assertEqual((self.root / PLAN).read_bytes(), plan)

    def test_roles_dependencies_and_shell_metacharacters_round_trip(self):
        def structured_planner(*args, response_schema, **kwargs):
            self.assertEqual(set(response_schema["required"]),
                             {"goal", "specification", "tasks", "external_dependencies"})
            self.assertFalse(response_schema["additionalProperties"])
            task_schema = response_schema["properties"]["tasks"]["items"]
            self.assertFalse(task_schema["additionalProperties"])
            self.assertEqual(task_schema["properties"]["files"]["items"], {"type": "string", "enum": [PATH]})
            check_schema = task_schema["properties"]["checks"]["items"]
            self.assertEqual(set(check_schema["required"]), set(CHECK))
            return self.planner(*args, **kwargs)

        structured_planner.accepts_response_schema = True
        command = 'printf "%s" "a&b" | grep "a&b"'
        self.proposal["tasks"].append({
            "description": "Coordinate the interface", "files": [PATH], "coder": "coder",
            "complexity": "contract", "depends_on": [1],
            "checks": [dict(CHECK, command=command, setup="Setup · expect detail")],
        })
        result = self.run_stage(runner=structured_planner, execute_after_plan=True)
        self.assertEqual(result.tasks[1].depends_on, (1,))
        self.assertEqual(result.tasks[1].checks[0].command, command)
        self.assertIn("<code>printf &quot;%s&quot; &quot;a&amp;b&quot; &#124; grep", (self.root / PLAN).read_text())

    def test_planner_question_returns_without_writing(self):
        decision = route(BLOCK, block_candidates=[BLOCK], work="first", requirements_ready=True)
        result = run_planning(
            self.root, decision, ticket="Implement example", plan_path=PLAN, files=[PATH],
            sections={BLOCK: [SPEC, ACCEPTANCE]},
            runner=lambda *args, **kwargs: RoleResult("error", "Which interface is authoritative?"),
        )
        self.assertEqual(result.stage, "await_decision")
        self.assertEqual(result.response, "*Which interface is authoritative?*")
        self.assertFalse((self.root / PLAN).exists())
        self.assertEqual((self.root / BLOCK).read_bytes(), NOTE)

    def test_invalid_path_checks_dependencies_role_and_sections_do_not_write(self):
        task = self.proposal["tasks"][0]
        for key, invalid in (
            ("files", ["Sources/example/../other.py"]), ("files", ["Sources/example/extra.py"]),
            ("checks", []), ("depends_on", [1]), ("coder", "coder_heavy"),
            ("complexity", "contract"),
        ):
            with self.subTest(key=key, invalid=invalid):
                old = task[key]
                task[key] = invalid
                with self.assertRaises(ValueError):
                    self.run_stage()
                task[key] = old
        self.proposal["specification"][0]["section"] = ["Specification", "Acceptance"]
        with self.assertRaises(ValueError):
            self.run_stage(work="modification")
        self.assertEqual((self.root / BLOCK).read_bytes(), NOTE)
        self.assertFalse((self.root / PLAN).exists())


if __name__ == "__main__":
    unittest.main()
