"""Exercise actual commands, final evidence, and scoped reporting in temporary worktrees."""

import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex import RoleExecutionError, RoleResult
from execution import run_execution
from planning import Check, Task, render_plan
from scope import capture
from verification import report_updates, run_verification, verified_receipt
from worktree import prepare_worktree, verify_worktree


BLOCK = "Sources/tool.md"
CHILD = "Sources/tool/child.py.md"
PLAN = "Plans/tool.md"
COMMAND = "grep -q 'value = 1' child.py"
COMMAND_CWD = "Sources/tool"
NOTE = """---
Template: Building block
---
## User requirements

Keep this wording.

## Specification
### Architecture and techniques

Use Python.

### Acceptance

Run the behavior check with `grep -q 'value = 1' child.py`.

## Working context
### Files

Existing files prose.

### Problems

Existing problems prose.
"""
TICKET = "\n#KOL-12🍏\nTicket text.\n#KOL-12🛑\n"


class VerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.git(self.vault, "init", "-q")
        self.git(self.vault, "config", "user.name", "Test")
        self.git(self.vault, "config", "user.email", "test@example.invalid")
        self.write(self.vault, BLOCK, NOTE)
        self.write(self.vault, CHILD, NOTE)
        self.write(self.vault, "Sources/tool/child.py", "value = 0\n")
        check = Check(COMMAND, COMMAND_CWD, "No dependencies", "Pass")
        task = Task("Set the value", ("Sources/tool/child.py",), "coder_light", "specified", (), (check,))
        self.write(self.vault, PLAN, render_plan(BLOCK, "first", "Run checks", (task,), {}, ()).decode() + TICKET)
        self.write(self.vault, ".gitignore", ".worktrees/\n")
        self.commit(self.vault, "basis")
        self.identity = {"ticket_id": "KOL-12", "slug": "tool"}
        selected = prepare_worktree(self.vault, **self.identity, snapshot_a=self.head(self.vault))
        self.worktree = selected.path
        execution = run_execution(self.vault, **self.identity, block=BLOCK, plan=PLAN,
                                  work="first", settings={}, runner=self.coder, review=lambda _: True)
        self.assertEqual(execution.stage, "complete")
        self.accepted = self.head(self.worktree)
        self.checks = {
            BLOCK: {"acceptance": [{"command": "test -f child.py", "cwd": "Sources/tool"}],
                    "tests": [], "mandatory": []},
            CHILD: {"acceptance": [{"command": COMMAND, "cwd": COMMAND_CWD}],
                    "tests": [], "mandatory": []},
        }
        self.reviews = []
        self.findings = []
        self.dispositions = []

    def git(self, path, *args):
        return subprocess.run(["git", "-C", str(path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def head(self, path):
        return self.git(path, "rev-parse", "HEAD")

    def commit(self, path, subject):
        self.git(path, "add", "-A")
        self.git(path, "commit", "-qm", subject)

    def write(self, root, file, contents):
        path = root / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)

    def coder(self, role, settings, *, cwd, context, allowed_paths, allowed_sections, checks, task):
        def check():
            return subprocess.run(COMMAND, shell=True, cwd=cwd / COMMAND_CWD, check=False).returncode

        red = {"command": COMMAND, "cwd": COMMAND_CWD, "exit": check(), "observed": "Value absent"}
        self.assertNotEqual(red["exit"], 0)
        self.write(cwd, "Sources/tool/child.py", "value = 1\n")
        green = {**red, "exit": check(), "observed": "Value set"}
        self.assertEqual(green["exit"], 0)
        self.git(cwd, "add", "--", *allowed_paths)
        self.git(cwd, "commit", "-qm", json.loads(context)["commit_subject"], "-m",
                 "Theseus-evidence: " + json.dumps({"red": red, "green": [green]}))
        return RoleResult("success", "Commit ready")

    def runner(self, role, settings, *, cwd, read_only, allowed_paths, allowed_sections, checks, task, context):
        self.assertEqual((role, cwd, read_only, allowed_paths, allowed_sections, checks),
                         ("reviewer", self.worktree, True, [], {}, []))
        brief = json.loads(context)
        self.reviews.append(brief)
        payload = {key: brief[key] for key in ("base", "head", "diff_sha256", "verification_sha256")}
        payload.update(findings=self.findings, dispositions=self.dispositions)
        return RoleResult("success", json.dumps(payload))

    def verify(self, runner=None, **options):
        request = {"accepted_head": self.accepted, "checks": self.checks, "settings": {},
                   "runner": runner or self.runner}
        return run_verification(self.vault, **self.identity, block=BLOCK, plan=PLAN, work="first",
                                **(request | options))

    def test_failed_check_and_resume_preserve_passed_commands_and_real_cwd(self):
        log = shlex.quote(str(self.root / "check-runs"))
        switch = shlex.quote(str(self.root / "permit"))
        self.checks[BLOCK]["acceptance"][0]["command"] += f" && pwd >> {log}"
        self.checks[BLOCK]["mandatory"].append({"command": f"test -f {switch}", "cwd": "."})
        blocked = self.verify()
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual([check["status"] for check in blocked["checks"]], ["passed", "failed", "pending"])
        self.assertFalse(self.reviews)
        (self.root / "permit").touch()

        def structured_runner(*args, response_schema, **kwargs):
            brief = json.loads(kwargs["context"])
            self.assertEqual(set(response_schema["required"]),
                             {"base", "head", "diff_sha256", "verification_sha256", "findings", "dispositions"})
            self.assertEqual(response_schema["properties"]["verification_sha256"],
                             {"type": "string", "enum": [brief["verification_sha256"]]})
            self.assertFalse(response_schema["additionalProperties"])
            return self.runner(*args, **kwargs)

        structured_runner.accepts_response_schema = True
        resumed = self.verify(structured_runner)
        self.assertEqual(resumed["status"], "verified", resumed)
        self.assertEqual((self.root / "check-runs").read_text().splitlines(), [str(self.worktree / "Sources/tool")])
        self.assertTrue(all(check["revision"] == self.accepted for check in resumed["checks"]))
        self.assertEqual(self.reviews[0]["checks"], resumed["checks"])
        evidence, = self.reviews[0]["task_evidence"]
        self.assertEqual(evidence["red"]["command"], COMMAND)
        self.assertEqual(evidence["red"]["exit"], 1)
        self.assertEqual(evidence["green"][0]["exit"], 0)
        self.assertIn("+value = 1", self.reviews[0]["diff"])
        self.assertEqual(resumed["review"]["verification_sha256"], self.reviews[0]["verification_sha256"])
        self.assertEqual(self.verify(), resumed)
        self.assertEqual(len(self.reviews), 1)
        updates = report_updates(capture(self.worktree), resumed, block=BLOCK, work="first", plan=PLAN)
        self.assertIn(b"Final verification verified", updates[PLAN].data)
        self.assertIn(b"No verification blockers", updates[BLOCK].data)

    def test_unfinished_plan_cannot_be_verified_even_at_its_exact_head(self):
        self.identity = {"ticket_id": "KOL-13", "slug": "pending"}
        selected = prepare_worktree(self.vault, **self.identity, snapshot_a=self.head(self.vault))
        self.worktree = selected.path
        self.accepted = self.head(self.worktree)
        blocked = self.verify()
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("unfinished tasks", blocked["problems"][0])
        self.assertFalse(self.reviews)
        self.assertEqual(self.head(self.worktree), self.accepted)

    def test_missing_command_and_affected_descendant_manifest_block(self):
        del self.checks[CHILD]
        self.assertIn(CHILD, self.verify()["problems"][0])
        self.checks[CHILD] = {"acceptance": [], "tests": [], "mandatory": []}
        self.assertIn("missing acceptance commands", self.verify()["problems"][0])
        self.checks[CHILD]["acceptance"].append({"command": "true", "cwd": "."})
        self.assertIn("missing required plan check", self.verify()["problems"][0])
        self.checks[CHILD]["acceptance"].append({"command": COMMAND, "cwd": COMMAND_CWD})
        self.checks[CHILD]["mandatory"].append({"command": "theseus-no-such-check-executable", "cwd": "."})
        unavailable = self.verify()
        self.assertEqual(unavailable["status"], "blocked")
        self.assertEqual(unavailable["checks"][-1]["exit"], 127)
        self.assertFalse(self.reviews)

    def test_stale_reviewer_binding_and_changed_revision_block(self):
        def stale_runner(*args, **kwargs):
            answer = self.runner(*args, **kwargs)
            payload = json.loads(answer.response)
            payload["verification_sha256"] = "stale"
            return RoleResult("success", json.dumps(payload))

        stale = self.verify(stale_runner)
        self.assertIn("actual mandatory check evidence", stale["problems"][0])
        self.assertEqual(self.verify()["status"], "verified")
        self.write(self.worktree, "Sources/tool/child.py", "value = 2\n")
        self.commit(self.worktree, "changed after verification")
        blocked = self.verify()
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("revision changed", blocked["problems"][0])

    def test_worktree_change_during_review_blocks(self):
        def mutating_runner(*args, **kwargs):
            self.write(self.worktree, "Sources/tool/child.py", "value = 2\n")
            return self.runner(*args, **kwargs)

        self.assertEqual(self.verify(mutating_runner)["status"], "blocked")
        self.assertEqual(self.verify()["status"], "blocked")

    def test_review_blocker_persists_and_scoped_updates_preserve_other_prose(self):
        self.findings = [{"id": "contract", "severity": "blocker", "detail": "contract at child.py:1"}]
        blocked = self.verify()
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(self.verify(), blocked)
        self.assertEqual(len(self.reviews), 1)
        baseline = capture(self.worktree)
        updates = report_updates(baseline, blocked, block=BLOCK, work="first", plan=PLAN)
        self.assertEqual(set(updates), {BLOCK, CHILD, PLAN})
        self.assertIn(b"*contract at child.py:1*", updates[CHILD].data)
        self.assertIn(b"\nExisting files prose.\n", updates[BLOCK].data)
        self.assertIn(b"\nKeep this wording.\n", updates[BLOCK].data)
        self.assertIn(b"Ticket text.", updates[PLAN].data)
        self.assertIn(b"Final verification blocked", updates[PLAN].data)
        self.assertEqual(capture(self.worktree), baseline)
        self.findings.clear()
        self.checks[BLOCK]["tests"].append({"command": "true", "cwd": "."})
        self.assertEqual(self.verify()["problems"], blocked["problems"])
        self.assertEqual(len(self.reviews), 1)

    def test_final_review_repair_rechecks_and_resumes_the_same_attempt(self):
        self.findings = [{"id": "contract", "severity": "blocker", "detail": "Missing explanation in child.py"}]
        self.assertEqual(self.verify()["status"], "blocked")
        self.write(self.worktree, "Sources/tool/child.py", "value = 1  # Default value\n")
        self.commit(self.worktree, "Explain the default value")
        revision = self.head(self.worktree)
        self.assertEqual(self.verify(accepted_head=revision)["problems"], ["unrecorded worktree HEAD"])

        def interrupt(*args, **kwargs):
            raise RoleExecutionError("simulated reviewer interruption")

        result = self.verify(interrupt, accepted_head=revision, repair_of=self.accepted)
        self.assertEqual(result["problems"], ["simulated reviewer interruption"])
        self.assertTrue(all(check["revision"] == revision for check in result["checks"]))
        self.findings.clear()
        result = self.verify(accepted_head=revision, repair_of=self.accepted)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("did not address every prior blocker", result["problems"][0])
        self.dispositions = [{"id": "contract", "status": "fixed", "detail": "Comment now explains the default"}]
        result = self.verify(accepted_head=revision, repair_of=self.accepted)
        self.assertEqual(result["status"], "verified", result)
        self.assertEqual(len(self.reviews), 3)
        self.assertEqual(self.reviews[-1]["previous_final_review"]["head"], self.accepted)
        self.assertIn("+value = 1  # Default value", self.reviews[-1]["diff"])
        self.assertNotEqual(self.reviews[0]["verification_sha256"], self.reviews[-1]["verification_sha256"])

        selected = verify_worktree(self.vault, **self.identity)
        verified_receipt(selected, block=BLOCK, plan=PLAN, work="first", accepted_head=revision)
        self.assertEqual(run_execution(self.vault, **self.identity, block=BLOCK, plan=PLAN,
                                       work="first", settings={}).stage, "complete")

    def test_failed_repair_check_allows_a_further_scoped_commit(self):
        self.findings = [{"id": "contract", "severity": "blocker", "detail": "Missing explanation in child.py"}]
        self.assertEqual(self.verify()["status"], "blocked")
        self.write(self.worktree, "Sources/tool/child.py", "value = 2  # Default value\n")
        self.commit(self.worktree, "Explain the default but change its value")
        failed = self.head(self.worktree)
        result = self.verify(accepted_head=failed, repair_of=self.accepted)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("exit 1", result["problems"][0])
        self.assertEqual(len(self.reviews), 1)

        self.write(self.worktree, "Sources/tool/child.py", "value = 1  # Default value\n")
        self.commit(self.worktree, "Restore the default value")
        self.findings.clear()
        self.dispositions = [{"id": "contract", "status": "fixed", "detail": "Comment now explains the default"}]
        result = self.verify(accepted_head=self.head(self.worktree), repair_of=failed)
        self.assertEqual(result["status"], "verified", result)
        self.assertEqual(self.reviews[-1]["previous_final_review"]["head"], self.accepted)

    def test_final_review_repair_rejects_unplanned_changes(self):
        self.findings = [{"id": "contract", "severity": "blocker", "detail": "Missing explanation in child.py"}]
        self.assertEqual(self.verify()["status"], "blocked")
        self.write(self.worktree, "Sources/tool/unplanned.py", "value = 1\n")
        self.commit(self.worktree, "Unplanned repair file")
        revision = self.head(self.worktree)
        result = self.verify(accepted_head=revision, repair_of=self.accepted)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("Sources/tool/unplanned.py: not writable", result["problems"][0])
        self.assertEqual(len(self.reviews), 1)

    def test_final_review_repair_requires_a_blocked_predecessor(self):
        self.assertEqual(self.verify()["status"], "verified")
        self.write(self.worktree, "Sources/tool/child.py", "value = 1  # Default value\n")
        self.commit(self.worktree, "Explain the default value")
        result = self.verify(accepted_head=self.head(self.worktree), repair_of=self.accepted)
        self.assertIn("recorded blocking final review", result["problems"][0])
        self.assertEqual(len(self.reviews), 1)


if __name__ == "__main__":
    unittest.main()
