"""Record a confirmed Bug fix diagnosis and its regression checks across retries."""

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

if __package__:
    from .routing import Decision, canonical_path, canonical_paths, require_boolean, unique_fields
    from .scope import _check_path, _implementation, _inside, authorize, block_scope, capture
    from .worktree import TICKET_ID, _checked_path, prepare_worktree, verify_worktree
else:
    from routing import Decision, canonical_path, canonical_paths, require_boolean, unique_fields
    from scope import _check_path, _implementation, _inside, authorize, block_scope, capture
    from worktree import TICKET_ID, _checked_path, prepare_worktree, verify_worktree


LIMIT = 3


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected nonempty diagnostic text")
    return value.strip()


def _command(command):
    argv = command["argv"]
    if (not isinstance(argv, list) or not argv or not argv[0]
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv)):
        raise ValueError("expected a nonempty argv array")
    cwd = command.get("cwd", ".")
    if cwd != ".":
        canonical_path(cwd)
    return {"argv": argv, "cwd": cwd}


def _matches(result, expected_exit, marker):
    return result.get("exit") == expected_exit and marker in result.get("output", "")


def _file_state(state, paths, embedded=None):
    fingerprints = {}
    for path in sorted(paths & state.keys()):
        file = state[path]
        data = file.data
        if embedded and path == embedded[0]:
            data = data.replace(embedded[1], b"", 1)
        fingerprints[path] = [file.mode, hashlib.sha256(data).hexdigest()]
    return fingerprints


def _outside_fingerprint(state, excluded, experiments):
    entries = {
        path: [entry.kind, entry.mode, hashlib.sha256(entry.data).hexdigest()]
        for path, entry in state.items()
        if entry.kind != "directory" and path not in excluded
        and not path.startswith(experiments + "/")
    }
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


class Diagnostics:
    def __init__(self, root, *, ticket_id, problem, block, files, regression,
                 regression_code=None, timeout=120):
        self.root = Path(root).resolve(strict=True)
        if not re.fullmatch(TICKET_ID, ticket_id):
            raise ValueError("invalid ticket ID")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", problem):
            raise ValueError("expected a stable kebab-case problem ID")
        self.experiments = f".obsta-experiments/{ticket_id}"
        self.block = block
        self.files = canonical_paths(files)
        self.regression = canonical_path(regression)
        if self.regression not in self.files:
            raise ValueError("permanent regression must be an exact authorized block file")
        self.embedded = None
        self.sources = self.files - {self.regression}
        if regression_code is not None:
            _text(regression_code)
            self.embedded = (self.regression, regression_code.encode())
            self.sources = self.files
        if not self.sources:
            raise ValueError("identify the exact implementation paths affected by the failure")
        self.identity = {"ticket_id": ticket_id, "problem": problem, "block": block,
                         "files": sorted(self.files), "regression": regression}
        if self.embedded:
            self.identity["regression_code"] = hashlib.sha256(self.embedded[1]).hexdigest()
        self.timeout = timeout

    def _run(self, root, command):
        command = _command(command)
        cwd = root / command["cwd"]
        if cwd.resolve(strict=True) != cwd or not cwd.is_dir():
            raise ValueError(f"unsafe diagnostic working directory: {command['cwd']}")
        before = capture(root)
        try:
            process = subprocess.run(command["argv"], cwd=cwd, capture_output=True,
                                     env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                                     timeout=self.timeout)
            result = {"exit": process.returncode,
                      "output": (process.stdout + process.stderr).decode(errors="replace")}
        except (OSError, subprocess.TimeoutExpired) as exc:
            result = {"error": str(exc)}
        authorize(before, capture(root), self.block, role="obstagent", stage="diagnose",
                  work="bug_fix", experiments=self.experiments)
        return dict(command, **result)

    def _check_scope(self, root):
        state = capture(root)
        authorize(state, state, self.block, role="obstagent", stage="local",
                  work="bug_fix", files=sorted(self.files), experiments=self.experiments)
        return state

    def _implementation_state(self, state):
        return _file_state(state, self.sources, self.embedded)

    def _regression_state(self, state):
        if not self.embedded:
            return _file_state(state, {self.regression})
        file = state.get(self.regression)
        if file is None or file.data.count(self.embedded[1]) != 1:
            raise ValueError(f"expected the exact inline regression once in {self.regression}")
        return {"inline": file.mode}

    def _workspace(self, state):
        if state["worktree"] is None:
            return self.root
        return verify_worktree(self.root, **state["worktree"]).path

    def _result(self, state, status, question=None, **extra):
        return {"status": status, "failed_fixes": state["failed_fixes"],
                "question": question, **extra}

    def _limit_reached(self, state):
        return state["failed_fixes"] - state.get("failures_before_approach", 0) >= LIMIT

    def _architectural_block(self, state):
        return self._result(state, "architecture", state["architecture_question"])

    def _continue(self, state, *, authorized, approach, decision):
        require_boolean(authorized, "authorized")
        if not self._limit_reached(state) or not authorized:
            raise ValueError("continuation requires the user's architecture decision after escalation")
        approach, decision = _text(approach), _text(decision)
        if any(event.get("new_approach") == approach for event in state["events"]):
            raise ValueError("the user-authorized approach must differ from earlier approaches")
        state["events"].append({"new_approach": approach, "architecture_decision": decision})
        state["failures_before_approach"] = state["failed_fixes"]
        return self._result(state, "rediagnose")

    def _diagnose(self, state, *, symptom, cause, reproduce, failure_exit, failure_marker,
                  probe, probe_marker, question, architecture_question):
        if state["pending"]:
            raise ValueError("finish verification of the current attempt before rediagnosing")
        if type(failure_exit) is not int or failure_exit <= 0:
            raise ValueError("expected a positive exit code for the reproduced failure")
        symptom, cause, marker = map(_text, (symptom, cause, failure_marker))
        probe_marker, question, architecture_question = map(
            _text, (probe_marker, question, architecture_question),
        )
        if _command(reproduce) == _command(probe):
            raise ValueError("cause probe must distinguish the hypothesis from the reproduction")
        root = self._workspace(state)
        before = self._check_scope(root)
        red = self._run(root, reproduce)
        result = {"symptom": symptom, "hypothesis": cause, "red": red,
                  "failure_exit": failure_exit, "failure_marker": marker}
        state["events"].append(result)
        state["confirmed"] = None
        if not _matches(red, failure_exit, marker):
            return self._result(state, "needs_evidence", question)
        result["probe"] = self._run(root, probe)
        if not _matches(result["probe"], 0, probe_marker):
            return self._result(state, "needs_evidence", question)
        result["implementation"] = self._implementation_state(before)
        state["confirmed"] = result
        state["architecture_question"] = architecture_question
        return self._result(state, "confirmed", diagnosis=result)

    def _handoff(self, state, *, slug, snapshot_a, inputs, attempt=1, execute_after_plan=False):
        if not state["confirmed"] or state["pending"]:
            raise ValueError("confirm the cause before preparing a Bug fix plan")
        require_boolean(execute_after_plan, "execute_after_plan")
        root = self._workspace(state)
        current = self._check_scope(root)
        if self._implementation_state(current) != state["confirmed"]["implementation"]:
            state["confirmed"] = None
            return self._result(state, "rediagnose")
        identity = {"ticket_id": self.identity["ticket_id"], "slug": slug, "attempt": attempt}
        if state["worktree"] and state["worktree"] != identity:
            raise ValueError("this diagnosis already has a different prepared worktree")
        supplied = canonical_paths(inputs) | self.files | {self.block}
        if any(path == ".obsta-experiments" or path.startswith(".obsta-experiments/") for path in supplied):
            raise ValueError("diagnostic artifacts stay in the ticket experiments directory")
        worktree = prepare_worktree(self.root, snapshot_a=snapshot_a, inputs=sorted(supplied), **identity)
        if worktree.snapshot_a != snapshot_a or worktree.inputs != tuple(sorted(supplied)):
            raise ValueError("prepared worktree no longer matches the diagnostic inputs")
        state["worktree"] = identity
        prepared = self._check_scope(worktree.path)
        if self._implementation_state(prepared) != state["confirmed"]["implementation"]:
            state["confirmed"] = None
            return self._result(state, "rediagnose")
        decision = Decision("plan", self.block, "bug_fix", execute_after_plan=execute_after_plan)
        return self._result(state, "plan", worktree={**asdict(worktree), "path": str(worktree.path)},
                            decision=asdict(decision),
                            diagnosis=state["confirmed"], regression=self.regression, files=sorted(self.files))

    def _begin(self, state, *, command, mandatory=()):
        if not state["confirmed"] or state["pending"]:
            raise ValueError("confirm the cause before starting a fix")
        checks = [_command(item) for item in mandatory]
        root = self._workspace(state)
        before = self._check_scope(root)
        if self.regression not in before:
            raise ValueError(f"write the permanent regression before fixing: {self.regression}")
        regression_state = self._regression_state(before)
        command = _command(command)
        cwd = root / command["cwd"]
        if not any((cwd / arg).resolve() == root / self.regression for arg in command["argv"]):
            raise ValueError(f"the regression command must name the permanent test: {self.regression}")
        if state["worktree"] is None:
            nested = block_scope(before, self.block).descendants
            if any(_inside(path, *_implementation(note)) for path in self.files for note in nested):
                raise ValueError("dependent block changes require a prepared Bug fix worktree")
        cause = state["confirmed"]
        implementation = self._implementation_state(before)
        if implementation != cause["implementation"]:
            state["confirmed"] = None
            return self._result(state, "rediagnose")
        red = self._run(root, command)
        state["events"].append({"regression_before_fix": red})
        if not _matches(red, cause["failure_exit"], cause["failure_marker"]):
            state["confirmed"] = None
            return self._result(state, "needs_evidence",
                                f"How can {self.regression} reproduce the confirmed failure: {cause['symptom']}?")
        state["pending"] = {"regression": command, "mandatory": checks,
                            "test": regression_state,
                            "outside": _outside_fingerprint(before, self.files, self.experiments)}
        return self._result(state, "ready_to_fix", root=str(root), files=sorted(self.sources))

    def _finish(self, state):
        pending = state["pending"]
        if not pending:
            raise ValueError("observe a failing permanent regression before verifying a fix")
        root = self._workspace(state)
        current = self._check_scope(root)
        if (state["worktree"] is None
                and _outside_fingerprint(current, self.files, self.experiments) != pending["outside"]):
            raise ValueError("local fix changed a file outside its exact implementation paths")
        if self._regression_state(current) != pending["test"]:
            raise ValueError("permanent regression changed after the failing baseline")
        cause = state["confirmed"]
        if self._implementation_state(current) == cause["implementation"]:
            raise ValueError("no implementation change to verify")
        result = self._run(root, pending["regression"])
        state["events"].append({"regression_after_fix": result})
        if _matches(result, cause["failure_exit"], cause["failure_marker"]):
            state["pending"] = None
            state["confirmed"] = None
            state["failed_fixes"] += 1
            if self._limit_reached(state):
                return self._architectural_block(state)
            return self._result(state, "rediagnose")
        if result.get("exit") != 0:
            return self._result(state, "verification_blocked",
                                f"How can the regression command be restored for {self.regression}?",
                                evidence=result)
        for check in pending["mandatory"]:
            result = self._run(root, check)
            state["events"].append({"mandatory_check": result})
            if result.get("exit") != 0:
                return self._result(state, "verification_blocked", evidence=result)
        state["pending"] = None
        state["confirmed"] = None
        return self._result(state, "verified")

    def run(self, action, **arguments):
        steps = {"diagnose": self._diagnose, "handoff": self._handoff,
                 "begin": self._begin, "finish": self._finish, "continue": self._continue}
        if action not in steps:
            raise ValueError("unknown diagnostic action")
        self._check_scope(self.root)
        directory = _checked_path(self.root, self.experiments)
        directory.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            path = _checked_path(directory, self.identity["problem"] + ".json")
            if path.exists():
                _check_path(capture(self.root), path.relative_to(self.root).as_posix())
                state = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)
                if state["identity"] != self.identity:
                    raise ValueError("diagnostic journal identity changed")
            else:
                state = {"identity": self.identity, "worktree": None, "events": [], "failed_fixes": 0,
                         "confirmed": None, "pending": None, "architecture_question": None}
            if self._limit_reached(state) and action != "continue":
                return self._architectural_block(state)
            result = steps[action](state, **arguments)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as staged:
                temporary = Path(staged.name)
                try:
                    json.dump(state, staged, ensure_ascii=False, indent=2)
                    staged.close()
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            return result
        finally:
            os.close(descriptor)


def main():
    try:
        request = json.load(sys.stdin, object_pairs_hook=unique_fields)
        result = Diagnostics(**request["case"]).run(request["action"], **request.get("arguments", {}))
        print(json.dumps(result, ensure_ascii=False))
    except (KeyError, OSError, TypeError, ValueError, subprocess.CalledProcessError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
