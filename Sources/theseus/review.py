"""Review verified execution candidates and persist decisions in their Git worktree."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

if __package__:
    from .codex import RoleExecutionError, run_role, scope_path, unique_fields
    from .execution import _clean, _parent
    from .worktree import OBJECT_ID, _git, _line
else:
    from codex import RoleExecutionError, run_role, scope_path, unique_fields
    from execution import _clean, _parent
    from worktree import OBJECT_ID, _git, _line


REVIEW_LIMIT = 5
ESCALATE_AFTER = 2
FIELDS = {"base", "head", "diff_sha256", "findings", "dispositions"}
IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
REVIEW_TASK = (
    "Review the entire supplied Git diff against the approved requirements and task. "
    "Use the supplied base and head. Inspect their committed files when needed. "
    "Return response as a single JSON object with base, head, diff_sha256 copied from the brief, "
    "findings, and dispositions. A finding has id, severity (blocker or advisory), and detail "
    "with the affected requirement or risk, location, and evidence. Unmet requirements, failed "
    "mandatory checks, and confirmed substantial risks are blockers. Include requested changes to "
    "Implementation details as findings with the applicable severity. "
    "Return a disposition for every prior blocker, with id, status (open, fixed, or unsupported), "
    "and detail describing what you independently verified. Only call a blocker fixed on a changed "
    "diff that addresses it. Use unsupported only after independently checking the exact Git citation "
    "and reasoning in a supplied challenge. Tests passing on their own never dispose of a blocker. "
    "Put previously reported blockers in dispositions and newly discovered issues in findings. "
    "Use empty arrays where applicable. Return outcome success for a completed report even when it "
    "contains blockers. Return outcome error when you could not complete the review."
)


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _fields(value, names):
    return isinstance(value, dict) and set(value) == names


def _git_bytes(root, *args, check=True):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(["git", "--no-replace-objects", "-C", str(root), *args],
                          check=check, capture_output=True, env=env)


def _object_schema(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def _run_reviewer(runner, settings, *, cwd, task, brief):
    options = {}
    if runner is run_role or getattr(runner, "accepts_response_schema", False) is True:
        final = "verification_sha256" in brief
        pins = ("base", "head", "diff_sha256", "verification_sha256") if final else ("base", "head", "diff_sha256")
        fields = {field: {"type": "string", "enum": [brief[field]]} for field in pins}
        text = {"type": "string"}
        statuses = ["open", "fixed"] if final else ["open", "fixed", "unsupported"]
        fields["findings"] = {"type": "array", "items": _object_schema({
            "id": text, "severity": {"type": "string", "enum": ["blocker", "advisory"]}, "detail": text,
        })}
        fields["dispositions"] = {"type": "array", "items": _object_schema({
            "id": text, "status": {"type": "string", "enum": statuses}, "detail": text,
        })}
        options["response_schema"] = _object_schema(fields)
    return runner("reviewer", settings, cwd=cwd, allowed_paths=[], allowed_sections={}, checks=[],
                  read_only=True, task=task, context=json.dumps(brief, ensure_ascii=False), **options)


def _report(response, version, blockers, challenges, changed_diff):
    report = json.loads(response, object_pairs_hook=unique_fields)
    if not _fields(report, FIELDS):
        raise ValueError("invalid review report fields")
    if any(report[field] != version[field] for field in ("base", "head", "diff_sha256")):
        raise ValueError("stale review: response refers to another Git diff")
    if not isinstance(report["findings"], list) or not isinstance(report["dispositions"], list):
        raise ValueError("review findings and dispositions must be arrays")
    known = {item["id"] for item in blockers}
    findings = set(known)
    for item in report["findings"]:
        if (not _fields(item, {"id", "severity", "detail"}) or not isinstance(item["id"], str)
                or not IDENTITY.fullmatch(item["id"]) or item["id"] in findings
                or item["severity"] not in ("blocker", "advisory") or not _nonempty(item["detail"])):
            raise ValueError("invalid or duplicate review finding")
        findings.add(item["id"])
    for item in report["dispositions"]:
        if (not _fields(item, {"id", "status", "detail"}) or not isinstance(item["id"], str)
                or item["id"] not in known or item["status"] not in ("open", "fixed", "unsupported")
                or not _nonempty(item["detail"])):
            raise ValueError("invalid or duplicate disposition of a prior blocker")
        if item["status"] == "fixed" and not changed_diff:
            raise ValueError("review cannot confirm a fix on an unchanged diff")
        if item["status"] == "unsupported" and item["id"] not in challenges:
            raise ValueError("review cannot dismiss a blocker without a corroborated challenge")
        known.remove(item["id"])
    if known:
        raise ValueError("review did not address every prior blocker")
    return report


class ReviewController:
    """Use as the review callback to run_execution in the same verified worktree.

    The caller supplies approved requirements (text, or a function of the candidate),
    chooses and carries out fixes, and submits each replacement via execution so scope
    and mandatory checks are verified again. This controller never modifies Git history.
    """

    def __init__(self, root, settings, requirements, *, runner=run_role):
        self.root = Path(root).resolve(strict=True)
        if str(self.root) != _line(self.root, "rev-parse", "--show-toplevel"):
            raise ValueError("review must run at the worktree root")
        self.admin = Path(_line(self.root, "rev-parse", "--absolute-git-dir"))
        self.settings = settings
        self.requirements = requirements
        self.runner = runner

    @contextmanager
    def _journal(self, number):
        if type(number) is not int or number < 1:
            raise ValueError("expected a positive task number")
        path = self.admin / f"theseus-review-{number}.json"
        with os.fdopen(os.open(path.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
                               0o600), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError("invalid review journal path")
            state = (json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)
                     if path.exists() else {
                         "rounds": [], "approaches": [], "challenges": [],
                     })
            yield path, state

    def _save(self, path, state):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.admin, delete=False) as file:
            pending = Path(file.name)
            try:
                json.dump(state, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
                os.replace(pending, path)
            finally:
                pending.unlink(missing_ok=True)

    def _version(self, candidate):
        if any(not isinstance(value, str) or not re.fullmatch(OBJECT_ID, value)
               for value in (candidate.parent, candidate.commit)):
            raise ValueError("review requires full commit object IDs")
        _clean(self.root)
        if (_line(self.root, "rev-parse", "HEAD") != candidate.commit
                or _parent(self.root, candidate.commit) != candidate.parent):
            raise ValueError("stale review candidate: HEAD or its parent differs")
        flags = ("--no-ext-diff", "--no-textconv", "--no-renames", "--no-color",
                 "--ignore-submodules=none", "--src-prefix=a/", "--dst-prefix=b/")
        names = _git(self.root, "diff", *flags, "--name-only", "-z", candidate.parent,
                     candidate.commit, "--").stdout.split("\0")[:-1]
        if not names or sorted(names) != sorted(candidate.files):
            raise ValueError("review candidate paths differ from the Git diff")
        diff = _git_bytes(self.root, "diff", *flags, "--binary", "--full-index", candidate.parent,
                          candidate.commit, "--").stdout
        requirements = self.requirements(candidate) if callable(self.requirements) else self.requirements
        if not _nonempty(requirements):
            raise ValueError("review requires the approved task and requirements")
        version = {
            "base": candidate.parent, "head": candidate.commit,
            "diff_sha256": hashlib.sha256(diff).hexdigest(),
            "requirements_sha256": hashlib.sha256(requirements.encode()).hexdigest(),
        }
        return version, diff.decode("utf-8", errors="backslashreplace"), requirements

    def _status(self, state):
        rounds, approaches = state["rounds"], state["approaches"]
        attempts = len(rounds) - (approaches[-1]["after_round"] if approaches else 0)
        last = rounds[-1] if rounds else None
        blockers = last["blockers"] if last else []
        difficulties = sum(bool(item["blockers"]) for item in rounds)
        if blockers and attempts >= REVIEW_LIMIT:
            action = "analyze"
        elif blockers:
            action = "review_challenge" if state["challenges"] else "fix"
        elif last:
            action = "reviewed"
        else:
            action = "review"
        return {
            "action": action, "rounds": len(rounds), "approach_rounds": attempts,
            "coder_override": "coder_heavy" if blockers and difficulties >= ESCALATE_AFTER else None,
            "blockers": blockers, "advisories": last["advisories"] if last else [],
            "version": last["version"] if last else None,
        }

    def status(self, number):
        """Report historical findings and the next action for the last reviewed candidate."""
        with self._journal(number) as (_, state):
            return self._status(state)

    def begin_approach(self, number, *, analysis, approach):
        """Record the analysis and a different approach without clearing blockers."""
        with self._journal(number) as (path, state):
            if self._status(state)["action"] != "analyze":
                raise ValueError("a new approach is available after five unresolved review rounds")
            if not _nonempty(analysis) or not _nonempty(approach):
                raise ValueError("record what failed and justify a different next approach")
            if approach.strip() in {entry["approach"] for entry in state["approaches"]}:
                raise ValueError("the proposed approach was already tried")
            state["approaches"].append({"after_round": len(state["rounds"]),
                                        "analysis": analysis.strip(), "approach": approach.strip()})
            state["challenges"] = []
            self._save(path, state)

    def challenge(self, candidate, finding_id, *, rationale, side, path, line, quote):
        """Ask the independent reviewer to verify a cited counterexample in Git."""
        with self._journal(candidate.number) as (journal, state):
            status = self._status(state)
            version, _, _ = self._version(candidate)
            if status["action"] not in {"fix", "review_challenge"} or version != status["version"]:
                raise ValueError("a challenge requires a current blocked review")
            if finding_id not in {item["id"] for item in status["blockers"]} or not _nonempty(rationale):
                raise ValueError("a challenge requires an open blocker and a rationale")
            if (side not in ("base", "head") or type(line) is not int or line < 1
                    or not _nonempty(quote) or not isinstance(path, str)):
                raise ValueError("a challenge requires a base or head Git citation with lines and verbatim text")
            scope_path(path)
            contents = _git_bytes(self.root, "cat-file", "blob", f"{version[side]}:{path}", check=False)
            quoted = quote.encode().splitlines()
            if contents.returncode or contents.stdout.splitlines()[line - 1:line - 1 + len(quoted)] != quoted:
                raise ValueError("challenge citation does not match committed Git content")
            if any(entry["id"] == finding_id for entry in state["challenges"]):
                raise ValueError("a challenge for this blocker is already queued")
            state["challenges"].append({"id": finding_id, "rationale": rationale, "side": side,
                                        "path": path, "line": line, "quote": quote})
            self._save(journal, state)

    def __call__(self, candidate):
        """Return True only for a current reviewed diff with no remaining blockers."""
        with self._journal(candidate.number) as (path, state):
            version, diff, requirements = self._version(candidate)
            status = self._status(state)
            previous = status["version"]
            if previous is not None and version["base"] != previous["base"]:
                raise ValueError("replacement candidate must keep the original dispatch parent")
            if status["action"] == "analyze":
                return False
            current = previous == version
            changed_diff = previous is not None and version["diff_sha256"] != previous["diff_sha256"]
            challenges = state["challenges"] if current else []
            if current and not challenges:
                return not status["blockers"]
            if (status["blockers"] and not changed_diff and not challenges
                    and version["requirements_sha256"] == previous["requirements_sha256"]):
                return False
            if state["approaches"] and status["approach_rounds"] == 0 and not changed_diff:
                return False
            brief = {
                **version, "number": candidate.number, "files": candidate.files, "diff": diff,
                "requirements": requirements, "red": candidate.red, "green": candidate.green,
                "blockers": status["blockers"], "challenges": challenges,
                "round": status["rounds"] + 1,
                "approach": state["approaches"][-1] if state["approaches"] else None,
            }
            answer = _run_reviewer(self.runner, self.settings, cwd=self.root, task=REVIEW_TASK, brief=brief)
            if answer.outcome != "success":
                raise RoleExecutionError(answer.response)
            if self._version(candidate)[0] != version:
                raise ValueError("stale review: Git diff or requirements changed during review")
            report = _report(answer.response, version, status["blockers"],
                             {item["id"] for item in challenges}, changed_diff)
            open_ids = {item["id"]: item["detail"] for item in report["dispositions"]
                        if item["status"] == "open"}
            blockers = [{**item, "detail": open_ids[item["id"]]} for item in status["blockers"]
                        if item["id"] in open_ids]
            advisories = {item["id"]: item for item in status["advisories"]}
            for item in report["findings"]:
                if item["severity"] == "blocker":
                    blockers.append(item)
                    advisories.pop(item["id"], None)
                else:
                    advisories[item["id"]] = item
            state["rounds"].append({"version": version, "blockers": blockers,
                                    "advisories": list(advisories.values()),
                                    "report": report, "challenges": challenges})
            state["challenges"] = []
            self._save(path, state)
            return not blockers
