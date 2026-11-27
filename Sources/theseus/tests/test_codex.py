import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from Sources.theseus.codex import RoleExecutionError, run_role


SOURCE = Path(__file__).resolve().parents[1]
ARCHIVE = SOURCE.parents[1] / "References" / (
    "superpowers-" + json.loads((SOURCE / "upstream.lock").read_text())["commit"] + ".tar.gz"
)
SETTINGS = {
    "theseus.orchestrator": "orchestrator-model/medium",
    "theseus.planner": "planner-model/high",
    "theseus.coder_light": "light-model/low",
    "theseus.coder": "coder-model/medium",
    "theseus.coder_heavy": "heavy-model/xhigh",
    "theseus.reviewer": "reviewer-model/high",
    "theseus.evaluator": "evaluator-model/high",
}

FAKE_CODEX = """\
#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
from pathlib import Path

args = sys.argv[1:]
log = Path(os.environ["FAKE_CODEX_LOG"])
schema = Path(args[args.index("--output-schema") + 1])
output = Path(args[args.index("--output-last-message") + 1])
log.write_text(json.dumps({
    "argv": args, "cwd": os.getcwd(), "prompt": sys.stdin.read(),
    "schema": json.loads(schema.read_text()),
}), encoding="utf-8")
mode = os.environ["FAKE_CODEX_MODE"]
if mode == "exit":
    output.write_text(os.environ["FAKE_CODEX_RESULT"], encoding="utf-8")
    print("model unavailable", file=sys.stderr)
    sys.exit(4)
if mode == "invalid-utf8":
    output.write_bytes(bytes([255]))
elif mode != "missing":
    output.write_text(os.environ["FAKE_CODEX_RESULT"], encoding="utf-8")
if mode in ("hang", "hang-after-exit"):
    descendant = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    log.with_suffix(".pids").write_text(f"{os.getpid()} {descendant.pid}", encoding="utf-8")
    if mode == "hang":
        time.sleep(30)
"""


class CodexTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cwd = self.root / "vault worktree"
        self.cwd.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.executable = self.bin / "codex"
        self.executable.write_text(FAKE_CODEX, encoding="utf-8")
        self.executable.chmod(0o755)
        self.log = self.root / "invocation.json"
        self.env = {
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "FAKE_CODEX_LOG": str(self.log),
            "FAKE_CODEX_MODE": "success",
            "FAKE_CODEX_RESULT": '{"outcome":"success","response":"Выполнено"}',
        }
        self.request = {
            "role": "coder",
            "settings": SETTINGS,
            "cwd": str(self.cwd),
            "task": "Implement the selected task",
            "context": "Только сведения об этой задаче",
            "allowed_paths": ["Sources/block.py"],
            "allowed_sections": {},
            "checks": ["python3 Sources/block.py --self-test"],
            "timeout": 15,
        }

    def run_role(self, **options):
        with patch.dict(os.environ, self.env):
            return run_role(**(self.request | options))

    def invocation(self):
        return json.loads(self.log.read_text(encoding="utf-8"))

    def invocation_brief(self, invocation):
        return json.loads(invocation["prompt"].split("\n\n", 1)[1])

    def assert_processes_stopped(self):
        pids = self.log.with_suffix(".pids")
        self.assertTrue(pids.is_file(), "fake Codex did not start its descendant")
        process_ids = pids.read_text().split()
        self.assertEqual(len(process_ids), 2)
        for raw_pid in process_ids:
            pid = int(raw_pid)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                process = subprocess.run(
                    ["ps", "-p", raw_pid, "-o", "stat="], capture_output=True, text=True,
                )
                if not process.stdout.strip() or process.stdout.strip().startswith("Z"):
                    break
                time.sleep(0.02)
            else:
                os.kill(pid, signal.SIGKILL)
                self.fail(f"role process {pid} survived cleanup")

    def test_roles_receive_selected_model_effort_scope_and_fresh_context(self):
        prior_context = None
        for role_setting, value in SETTINGS.items():
            role = role_setting.removeprefix("theseus.")
            model, effort = value.split("/")
            with self.subTest(role=role):
                context = f"Context specific to [{role}]"
                paths = ["Sources/block.py"] if role.startswith("coder") else []
                sections = {"Sources/block.md": ["Specification"]} if role == "planner" else {}
                result = self.run_role(
                    role=role, context=context, allowed_paths=paths, allowed_sections=sections,
                )
                self.assertEqual((result.outcome, result.response), ("success", "Выполнено"))
                call = self.invocation()
                args = call["argv"]
                self.assertEqual(args[0], "exec")
                self.assertEqual(args[-1], "-")
                self.assertIn("--ephemeral", args)
                self.assertIn("--skip-git-repo-check", args)
                self.assertNotIn("resume", args)
                self.assertNotIn("fork", args)
                self.assertEqual(args[args.index("-C") + 1], str(self.cwd))
                self.assertEqual(args[args.index("-m") + 1], model)
                self.assertIn(f'model_reasoning_effort="{effort}"', args)
                self.assertIn('approval_policy="never"', args)
                self.assertEqual(call["cwd"], str(self.cwd))
                brief = self.invocation_brief(call)
                self.assertEqual(brief, {
                    "role": role,
                    "task": self.request["task"],
                    "context": context,
                    "allowed_paths": paths,
                    "allowed_sections": sections,
                    "checks": self.request["checks"],
                })
                if prior_context is not None:
                    self.assertNotIn(prior_context, call["prompt"])
                self.assertNotIn("theseus.", call["prompt"])
                self.assertEqual(call["schema"]["properties"]["outcome"]["enum"], ["success", "error"])
                prior_context = context

    def test_role_error_remains_error(self):
        self.env["FAKE_CODEX_RESULT"] = '{"outcome":"error","response":"Blocked on requirement."}'
        result = self.run_role()
        self.assertEqual((result.outcome, result.response), ("error", "Blocked on requirement."))

    def test_structured_result_preserves_the_role_result_string_boundary(self):
        response_schema = {
            "type": "object", "properties": {"head": {"type": "string", "enum": ["abcdef"]}},
            "required": ["head"], "additionalProperties": False,
        }
        options = {"role": "reviewer", "read_only": True, "allowed_paths": [],
                   "response_schema": response_schema}
        self.env["FAKE_CODEX_RESULT"] = '{"outcome":"success","response":{"head":"abcdef"}}'
        answer = self.run_role(**options)
        self.assertEqual(answer.outcome, "success")
        self.assertEqual(json.loads(answer.response), {"head": "abcdef"})
        invocation = self.invocation()
        self.assertEqual(invocation["schema"]["properties"]["response"],
                         {"anyOf": [response_schema, {"type": "string"}]})
        self.assertIn("put the report object directly in response", invocation["prompt"])

        self.env["FAKE_CODEX_RESULT"] = '{"outcome":"error","response":"No result"}'
        answer = self.run_role(**options)
        self.assertEqual((answer.outcome, answer.response), ("error", "No result"))
        self.env["FAKE_CODEX_RESULT"] = '{"outcome":"success","response":"encoded by hand"}'
        with self.assertRaisesRegex(RoleExecutionError, "invalid final result"):
            self.run_role(**options)

    def test_argument_validation_precedes_launch(self):
        for options in (
            {"role": "unknown"}, {"settings": {}},
            {"settings": {"theseus.coder": "model/unknown"}},
            {"settings": {"theseus.coder": 'model"noise/medium'}},
            {"cwd": "relative/path"}, {"cwd": str(self.root / "unavailable")},
            {"cwd": str(self.executable)}, {"allowed_paths": ["../outside"]},
            {"allowed_sections": {"/absolute.md": ["Working context"]}},
            {"allowed_sections": {"block.md": "Working context"}},
            {"checks": "echo wrong-type"}, {"timeout": float("inf")},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_role(**options)
        self.assertFalse(self.log.exists())

    def test_unavailable_executable_or_failed_exit(self):
        original_path = self.env["PATH"]
        self.env["PATH"] = str(self.root / "empty-path")
        with self.assertRaisesRegex(RoleExecutionError, "could not launch"):
            self.run_role()
        self.env["PATH"] = original_path
        self.env["FAKE_CODEX_MODE"] = "exit"
        with self.assertRaisesRegex(RoleExecutionError, "exit 4.*model unavailable"):
            self.run_role()

    def test_rejects_missing_and_invalid_results(self):
        for mode, output in (
            ("missing", ""), ("invalid-utf8", ""), ("success", "plain text"),
            ("success", '{"outcome":"done","response":"Looks good"}'),
            ("success", '{"outcome":"success","response":true}'),
            ("success", '{"outcome":"success","response":"   "}'),
            ("success", '{"outcome":"success"}'),
            ("success", '{"outcome":"success","response":"OK","extra":1}'),
            ("success", '{"outcome":"error","outcome":"success","response":"Duplicate"}'),
            ("success", '[{"outcome":"success","response":"OK"}]'),
        ):
            with self.subTest(mode=mode, output=output):
                self.env.update(FAKE_CODEX_MODE=mode, FAKE_CODEX_RESULT=output)
                with self.assertRaisesRegex(RoleExecutionError, "invalid final result"):
                    self.run_role()

    def test_timeout_kills_child_and_descendant_even_with_valid_result(self):
        self.env["FAKE_CODEX_MODE"] = "hang"
        with self.assertRaisesRegex(RoleExecutionError, "timed out"):
            self.run_role(timeout=8)
        self.assert_processes_stopped()

    def test_completion_cleans_up_lingering_descendant(self):
        self.env["FAKE_CODEX_MODE"] = "hang-after-exit"
        self.assertEqual(self.run_role().outcome, "success")
        self.assert_processes_stopped()

    def test_interruptions_kill_child_and_descendant_even_with_valid_result(self):
        self.env["FAKE_CODEX_MODE"] = "hang"
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum):
                pids = self.log.with_suffix(".pids")
                pids.unlink(missing_ok=True)
                with subprocess.Popen(
                    [sys.executable, str(SOURCE / "codex.py")],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env=os.environ | self.env,
                ) as adapter:
                    try:
                        adapter.stdin.write(json.dumps(self.request | {"timeout": 15}))
                        adapter.stdin.close()
                        adapter.stdin = None
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline and not pids.is_file():
                            time.sleep(0.02)
                        self.assertTrue(pids.exists())
                        adapter.send_signal(signum)
                        stdout, stderr = adapter.communicate(timeout=5)
                        self.assertEqual(adapter.returncode, 2, stderr)
                        result = json.loads(stdout)
                        self.assertEqual(result["outcome"], "error")
                        self.assertIn("interrupted", result["response"])
                        self.assert_processes_stopped()
                    finally:
                        if adapter.poll() is None:
                            adapter.terminate()

    def test_installed_adapter_runs_from_separate_vault(self):
        bundle = self.root / "theseus"
        subprocess.run(
            [sys.executable, str(SOURCE / "build.py"), "--archive", str(ARCHIVE),
             "--output", str(bundle)], check=True, cwd=self.root,
        )
        entry = bundle / "skills" / "using-theseus" / "scripts" / "codex.py"
        result = subprocess.run(
            [sys.executable, str(entry)], input=json.dumps(self.request), capture_output=True,
            text=True, cwd=self.cwd, env=os.environ | self.env, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["outcome"], "success")
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn(entry.relative_to(bundle).as_posix(), manifest["files"])
        for relative in (
            "subagent-driven-development/implementer-prompt.md",
            "subagent-driven-development/task-reviewer-prompt.md",
            "subagent-driven-development/re-review-prompt.md",
            "requesting-code-review/code-reviewer.md",
        ):
            template = bundle / "skills" / relative
            self.assertIn("codex-tools.md", template.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
