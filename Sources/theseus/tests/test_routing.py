import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

from routing import LocalChange, Plan, route


BLOCK = "Sources/example.md"
IMPLEMENTATION = "Sources/example.py"
PLAN = "Plans/my plan.md"
OTHER = "Notes/another.md"


class RoutingTest(unittest.TestCase):
    def decide(self, path=BLOCK, **arguments):
        arguments.setdefault("block_candidates", (BLOCK,))
        return route(path, **arguments)

    def test_activation_and_default_paths(self):
        cases = (
            ("ordinary ticket", OTHER, {}, "ordinary"),
            ("outside authorized Sources write", OTHER,
             {"write_targets": (IMPLEMENTATION,), "work": "modification"}, "brainstorm"),
            ("outside authorized Plans write", OTHER,
             {"write_targets": (PLAN,), "work": "first", "requirements_ready": True}, "plan"),
            ("similarly named folder", "Sources notes/foo.md", {}, "ordinary"),
            ("source discussion", BLOCK, {"action": "discuss"}, "brainstorm"),
            ("plan discussion", PLAN, {"action": "discuss"}, "brainstorm"),
            ("unspecified work in Sources", BLOCK, {}, "clarify"),
        )
        for name, path, inputs, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(self.decide(path, **inputs).stage, expected)

    def test_plan_status_and_explicit_transition(self):
        for status, action, expected in (
            ("draft", "execute", "clarify"),
            ("ready", "execute", "execute"),
            ("ready", "auto", "await_execution"),
            ("ready", "continue", "clarify"),
            ("in_progress", "execute", "clarify"),
            ("in_progress", "continue", "resume"),
            ("blocked", "continue", "resume"),
            ("interrupted", "continue", "resume"),
            ("done", "continue", "clarify"),
            ("rejected", "execute", "clarify"),
        ):
            with self.subTest(status=status, action=action):
                result = self.decide(PLAN, action=action, plan=Plan(PLAN, BLOCK, "first", status))
                self.assertEqual(result.stage, expected)
        self.assertEqual(self.decide(PLAN, action="execute").stage, "clarify")
        self.assertEqual(self.decide(work="first", action="execute", requirements_ready=True).stage,
                         "clarify")
        default = self.decide(work="first", requirements_ready=True)
        explicit = self.decide(work="first", action="plan", requirements_ready=True,
                               execute_after_plan=True)
        self.assertEqual(default.stage, "plan")
        self.assertFalse(default.execute_after_plan)
        self.assertEqual(explicit.stage, "plan")
        self.assertTrue(explicit.execute_after_plan)
        self.assertEqual(self.decide(work="first", action="plan", execute_after_plan=True).stage,
                         "clarify")

    def test_work_types_local_modification_and_bug(self):
        localized = LocalChange((IMPLEMENTATION,), "python3 -m unittest test_example", True, True)
        settings = dict(requirements_ready=True, local=localized, write_targets=(IMPLEMENTATION,))
        self.assertEqual(self.decide(work="first", **settings).stage, "plan")
        self.assertEqual(self.decide(work="modification", **settings).stage, "local")
        self.assertEqual(self.decide(work="modification", action="plan", **settings).stage, "plan")
        self.assertEqual(self.decide(work="modification", requirements_ready=True).stage, "plan")
        self.assertEqual(self.decide(work="modification", requirements_ready=True,
                                     local=localized).stage, "plan")
        self.assertEqual(self.decide(work="modification", requirements_ready=True,
                                     local=localized, write_targets=(IMPLEMENTATION, PLAN)).stage,
                         "plan")
        self.assertEqual(self.decide(work="bug_fix", **settings).stage, "diagnose")
        self.assertEqual(self.decide(work="bug_fix", diagnosis_confirmed=True, **settings).stage,
                         "local")
        self.assertEqual(self.decide(work="bug_fix", diagnosis_confirmed=True,
                                     requirements_ready=True).stage, "plan")

    def test_refactoring_requires_confirmation_and_prepared_workspace(self):
        self.assertEqual(self.decide(work="refactoring").stage, "clarify")
        self.assertEqual(self.decide(work="refactoring", action="discuss").stage, "brainstorm")
        self.assertEqual(self.decide(work="refactoring", refactor_confirmed=True).stage, "brainstorm")
        self.assertEqual(self.decide(work="refactoring", refactor_confirmed=True,
                                     requirements_ready=True).stage, "prepare_refactoring")
        self.assertEqual(self.decide(work="refactoring", refactor_confirmed=True,
                                     requirements_ready=True, refactor_workspace_ready=True).stage,
                         "plan")
        plan = Plan(PLAN, BLOCK, "refactoring", "ready")
        self.assertEqual(self.decide(PLAN, action="execute", plan=plan,
                                     refactor_confirmed=True).stage, "clarify")
        self.assertEqual(self.decide(PLAN, action="execute", plan=plan, refactor_confirmed=True,
                                     refactor_workspace_ready=True).stage, "execute")

    def test_ambiguity_does_not_create_authorization(self):
        another = "Sources/other.md"
        candidates = (BLOCK, another)
        self.assertEqual(self.decide(work="first", block_candidates=()).stage, "clarify")
        self.assertEqual(self.decide(work="first", block_candidates=candidates).stage, "clarify")
        self.assertEqual(self.decide(work="first", block_candidates=candidates,
                                     selected_block=BLOCK).block, BLOCK)
        self.assertEqual(self.decide(work="first", selected_block=another).stage, "clarify")
        self.assertEqual(self.decide(PLAN, work="modification", action="execute",
                                     plan=Plan(PLAN, BLOCK, "first", "ready")).stage, "clarify")
        self.assertEqual(self.decide(PLAN, action="execute",
                                     plan=Plan("Plans/other.md", BLOCK, "first", "ready")).stage,
                         "clarify")
        self.assertEqual(self.decide(PLAN, action="execute",
                                     plan=Plan(PLAN, another, "first", "ready")).stage, "clarify")
        self.assertEqual(self.decide(OTHER, work="first", action="execute",
                                     plan=Plan(PLAN, BLOCK, "first", "ready")).stage, "ordinary")
        self.assertEqual(self.decide(OTHER, action="execute", write_targets=(PLAN,),
                                     plan=Plan(PLAN, BLOCK, "first", "ready")).stage, "execute")
        self.assertEqual(self.decide(OTHER, action="execute", write_targets=(IMPLEMENTATION,),
                                     plan=Plan(PLAN, BLOCK, "first", "ready")).stage, "clarify")

    def test_noncanonical_paths_are_rejected(self):
        for path in ("/Sources/x.md", "../Sources/x.md", "Notes/../Sources/x.md", "Plans//x.md",
                     "./Sources/x.md", "Sources\\x.md", "sources/x.md", "Sources/x.md\n", " C.md"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.decide(path)
        with self.assertRaises(ValueError):
            self.decide(write_targets="Sources/example.py")
        with self.assertRaises(ValueError):
            self.decide(work="first", requirements_ready="false")
        with self.assertRaises(ValueError):
            self.decide(action="continue", plan=Plan(PLAN, BLOCK, "first", "uncertain"))

    def test_actual_patches_and_installed_router(self):
        lock = json.loads((SOURCE / "upstream.lock").read_text(encoding="utf-8"))
        archive = SOURCE.parents[1] / "References" / ("superpowers-" + lock["commit"] + ".tar.gz")
        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp).resolve() / "installed"
            subprocess.run([sys.executable, str(SOURCE / "build.py"), "--archive",
                            str(archive), "--output", str(installed)], cwd=tmp, check=True)
            second = installed.with_name("strict-umask")
            subprocess.run([sys.executable, str(SOURCE / "build.py"), "--archive",
                            str(archive), "--output", str(second)], cwd=tmp, check=True,
                           umask=0o077)
            manifest = (installed / "manifest.json").read_bytes()
            self.assertEqual(manifest, (second / "manifest.json").read_bytes())
            routing_patches = json.loads(manifest)["patches"][:3]
            self.assertEqual([item["path"] for item in routing_patches],
                             ["001-using-theseus.patch", "002-brainstorming.patch",
                              "003-writing-plans.patch"])
            scripts = installed / "skills/using-theseus/scripts/routing.py"
            self.assertEqual(scripts.read_bytes(), (SOURCE / "routing.py").read_bytes())
            for name in ("using-theseus", "brainstorming", "writing-plans"):
                skill = installed / "skills" / name / "SKILL.md"
                text = skill.read_text(encoding="utf-8")
                self.assertIn(f"name: {name}\n", text)
                links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)
                self.assertTrue(links, str(skill))
                for link in links:
                    dest = (skill.parent / link).resolve()
                    self.assertTrue(dest.is_relative_to(installed) and dest.is_file(), link)
            invocation = [sys.executable, str(scripts)]
            output = subprocess.run(invocation, cwd=tmp, text=True, capture_output=True, check=True,
                                    input=json.dumps({"ticket_path": OTHER}))
            self.assertEqual(json.loads(output.stdout)["stage"], "ordinary")
            bad = subprocess.run(invocation, cwd=tmp, text=True, capture_output=True,
                                 input='{"ticket_path":"Sources/example.md","refactor_confirmed":"false"}')
            self.assertNotEqual(bad.returncode, 0)
            self.assertEqual(bad.stdout, "")
            duplicate = subprocess.run(invocation, cwd=tmp, text=True, capture_output=True,
                                       input='{"ticket_path":"a.md","ticket_path":"b.md"}')
            self.assertNotEqual(duplicate.returncode, 0)


if __name__ == "__main__":
    unittest.main()
