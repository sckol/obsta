"""Review checked candidates using scripted reviewers and disposable Git repos."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex import RoleResult
from execution import Candidate
from review import ReviewController


BLOCKER = {"id": "value", "severity": "blocker", "detail": "Result violates the requirement at code.py:1"}
ADVISORY = {"id": "naming", "severity": "advisory", "detail": "Consider a more descriptive name"}


def disposition(status):
    return {"id": BLOCKER["id"], "status": status, "detail": "Verified code.py:1 against the requirement"}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.name", "Theseus test")
        self.git("config", "user.email", "test@example.invalid")
        self.code = self.root / "code.py"
        self.code.write_text("VALUE = 0\n")
        self.git("add", "code.py")
        self.git("commit", "-qm", "dispatch")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.candidate = self.replace_candidate(1, first=True)
        self.settings = {"theseus.reviewer": "scripted/medium"}
        self.requirements = "Return the required value"
        self.calls = []
        self.answers = []

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, check=True,
                              text=True).stdout

    def replace_candidate(self, value, *, first=False):
        self.code.write_text(f"VALUE = {value}\n")
        self.git("add", "code.py")
        if first:
            self.git("commit", "-qm", "candidate")
        else:
            self.git("commit", "--amend", "--no-edit", "-q")
        return Candidate(1, self.git("rev-parse", "HEAD").strip(), self.base, ("code.py",), {},
                         ({"command": "test", "exit": 0},))

    def controller(self):
        return ReviewController(self.root, self.settings, lambda _: self.requirements, runner=self.runner)

    def runner(self, role, settings, *, cwd, allowed_paths, allowed_sections, checks, read_only, task, context):
        self.assertEqual((role, settings, cwd), ("reviewer", self.settings, self.root))
        self.assertEqual((allowed_paths, allowed_sections, checks, read_only), ([], {}, [], True))
        brief = json.loads(context)
        self.calls.append(brief)
        self.assertIn("dispositions", task)
        response = {field: brief[field] for field in ("base", "head", "diff_sha256")}
        payload = self.answers.pop(0)
        response.update(payload(brief) if callable(payload) else payload)
        return RoleResult("success", json.dumps(response))

    def test_reviewer_gets_actual_diff_and_checks_then_confirms_a_fix(self):
        def structured_runner(*args, response_schema, **kwargs):
            brief = json.loads(kwargs["context"])
            self.assertEqual(set(response_schema["required"]),
                             {"base", "head", "diff_sha256", "findings", "dispositions"})
            self.assertFalse(response_schema["additionalProperties"])
            for field in ("base", "head", "diff_sha256"):
                self.assertEqual(response_schema["properties"][field],
                                 {"type": "string", "enum": [brief[field]]})
            return self.runner(*args, **kwargs)

        structured_runner.accepts_response_schema = True
        review = ReviewController(self.root, self.settings, self.requirements, runner=structured_runner)
        self.answers = [dict(findings=[BLOCKER, ADVISORY], dispositions=[]),
                        dict(findings=[], dispositions=[disposition("fixed")])]
        self.assertFalse(review(self.candidate))
        self.assertFalse(self.controller()(self.candidate))
        self.assertEqual(len(self.calls), 1)
        first = self.calls[0]
        self.assertEqual(first["base"], self.base)
        self.assertEqual(first["head"], self.candidate.commit)
        self.assertEqual(first["requirements"], self.requirements)
        self.assertEqual(first["green"], list(self.candidate.green))
        diff = self.git("diff", "--binary", "--full-index", self.base, self.candidate.commit)
        self.assertEqual(first["diff"], diff)
        self.assertEqual(first["diff_sha256"], hashlib.sha256(diff.encode()).hexdigest())
        self.assertEqual(review.status(1)["action"], "fix")
        self.assertEqual(review.status(1)["blockers"], [BLOCKER])

        fixed = self.replace_candidate(2)
        self.assertTrue(self.controller()(fixed))
        self.assertEqual(self.calls[1]["blockers"], [BLOCKER])
        self.assertTrue(review(fixed))
        self.assertEqual(review.status(1)["advisories"], [ADVISORY])
        self.assertEqual(review.status(1)["rounds"], 2)
        self.assertEqual(review.status(1)["action"], "reviewed")
        self.assertFalse(self.git("status", "--porcelain"))

    def test_false_finding_requires_a_git_corroborated_challenge_and_rereview(self):
        review = self.controller()
        self.answers = [dict(findings=[BLOCKER], dispositions=[]),
                        dict(findings=[], dispositions=[disposition("unsupported")])]
        self.assertFalse(review(self.candidate))
        with self.assertRaisesRegex(ValueError, "citation does not match"):
            review.challenge(self.candidate, "value", rationale="Declared line", side="head", path="code.py",
                             line=1, quote="VALUE = 2")
        review.challenge(self.candidate, "value", rationale="The actual value meets the plan", side="head",
                         path="code.py", line=1, quote="VALUE = 1")
        self.assertEqual(review.status(1)["action"], "review_challenge")
        self.assertTrue(self.controller()(self.candidate))
        self.assertEqual(self.calls[0]["head"], self.calls[1]["head"])
        challenge, = self.calls[1]["challenges"]
        self.assertEqual(challenge["quote"], "VALUE = 1")
        self.assertEqual(challenge["id"], "value")

    def test_do_not_accept_silent_or_unsubstantiated_blocker_dispositions(self):
        review = self.controller()
        self.answers = [dict(findings=[BLOCKER], dispositions=[]),
                        dict(findings=[], dispositions=[]),
                        dict(findings=[], dispositions=[disposition("unsupported")])]
        self.assertFalse(review(self.candidate))
        fixed = self.replace_candidate(2)
        with self.assertRaisesRegex(ValueError, "every prior blocker"):
            review(fixed)
        with self.assertRaisesRegex(ValueError, "corroborated challenge"):
            review(fixed)
        self.assertEqual(review.status(1)["blockers"], [BLOCKER])
        self.assertEqual(review.status(1)["rounds"], 1)

    def test_stale_candidate_and_stale_response_cannot_approve_new_head(self):
        review = self.controller()
        self.answers = [dict(findings=[], dispositions=[])]
        self.assertTrue(review(self.candidate))
        fixed = self.replace_candidate(2)
        self.assertEqual(review.status(1)["version"]["head"], self.candidate.commit)
        with self.assertRaisesRegex(ValueError, "stale review candidate"):
            review(self.candidate)
        self.answers = [dict(findings=[], dispositions=[], head=self.candidate.commit)]
        with self.assertRaisesRegex(ValueError, "stale review"):
            review(fixed)
        self.assertEqual(review.status(1)["rounds"], 1)
        self.answers = [dict(findings=[], dispositions=[])]
        self.assertTrue(review(fixed))

    def test_approval_depends_on_current_requirements_and_clean_worktree(self):
        review = self.controller()
        self.answers = [dict(findings=[], dispositions=[]), dict(findings=[BLOCKER], dispositions=[])]
        self.assertTrue(review(self.candidate))
        self.requirements = "Return a different value"
        self.assertFalse(review(self.candidate))
        self.assertNotEqual(self.calls[0]["requirements_sha256"], self.calls[1]["requirements_sha256"])
        self.code.write_text("dirty")
        with self.assertRaisesRegex(ValueError, "uncommitted"):
            review(self.candidate)

    def test_resume_escalates_and_requires_new_approach_with_blocker_even_when_green(self):
        self.answers = [dict(findings=[BLOCKER], dispositions=[])]
        self.answers.extend(dict(findings=[], dispositions=[disposition("open")]) for _ in range(4))
        self.assertFalse(self.controller()(self.candidate))
        self.assertIsNone(self.controller().status(1)["coder_override"])
        for value in range(2, 6):
            current = self.replace_candidate(value)
            self.assertFalse(self.controller()(current))
            status = self.controller().status(1)
            self.assertEqual(status["coder_override"], "coder_heavy")
            self.assertEqual(status["rounds"], value)
            self.assertEqual(self.calls[-1]["green"][0]["exit"], 0)
            self.assertEqual(status["blockers"][0]["id"], "value")
        review = self.controller()
        self.assertEqual(review.status(1)["action"], "analyze")
        self.assertFalse(review(current))
        with self.assertRaisesRegex(ValueError, "justify"):
            review.begin_approach(1, analysis="", approach="Retry")
        review.begin_approach(1, analysis="Constants failed the input contract", approach="Read the input")
        self.assertEqual(self.controller().status(1)["approach_rounds"], 0)
        self.assertEqual(self.controller().status(1)["coder_override"], "coder_heavy")
        self.assertFalse(review(current))
        next_candidate = self.replace_candidate(6)
        self.answers = [dict(findings=[], dispositions=[disposition("fixed")])]
        self.assertTrue(self.controller()(next_candidate))
        self.assertEqual(self.calls[-1]["approach"]["approach"], "Read the input")
        self.assertEqual(self.controller().status(1)["rounds"], 6)

    def test_execution_accepts_only_after_successful_review_callback(self):
        from test_execution import ExecutionTests

        fixture = ExecutionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.worktree.path
        self.answers = [dict(findings=[BLOCKER], dispositions=[]),
                        dict(findings=[], dispositions=[disposition("fixed")]),
                        dict(findings=[], dispositions=[])]
        review = self.controller()
        result = fixture.run_execution(fixture.coder, review=review)
        self.assertEqual(result.stage, "await_review")
        self.assertEqual(fixture.calls, [(1, "coder_light")])
        code = self.root / fixture.files[0]
        code.write_text("VALUE = int('1')\n")
        fixture.git(self.root, "add", "--", fixture.files[0])
        fixture.git(self.root, "commit", "--amend", "--no-edit", "-q")
        self.assertEqual(fixture.run_execution(fixture.coder, review=self.controller()).stage, "complete")
        self.assertEqual(fixture.calls, [(1, "coder_light"), (2, "coder")])
        self.assertEqual([call["number"] for call in self.calls], [1, 1, 2])
        self.assertEqual(self.calls[0]["base"], self.calls[1]["base"])
        self.assertNotEqual(self.calls[0]["head"], self.calls[1]["head"])
        self.assertEqual(fixture.git(fixture.main, "rev-parse", "HEAD").strip(), fixture.main_head)
