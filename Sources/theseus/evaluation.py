"""Compare pinned refactoring revisions and record the user's exact choice."""

import fcntl
import hashlib
import io
import json
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

if __package__:
    from .codex import RoleExecutionError, run_role
    from .execution import _check_checkout, verify_completed_execution
    from .notes import _Document
    from .refactoring import _check_untracked, _revision, _save
    from .routing import canonical_path, canonical_paths, note_path, unique_fields
    from .scope import _inside, block_scope, capture
    from .worktree import OBJECT_ID, _checked_path, _clean, _git, _line, verify_worktree
else:
    from codex import RoleExecutionError, run_role
    from execution import _check_checkout, verify_completed_execution
    from notes import _Document
    from refactoring import _check_untracked, _revision, _save
    from routing import canonical_path, canonical_paths, note_path, unique_fields
    from scope import _inside, block_scope, capture
    from worktree import OBJECT_ID, _checked_path, _clean, _git, _line, verify_worktree


ACTIONS = {"old": "discard_new", "new": "return_new"}
CHECK_KINDS = {"common", "acceptance", "typical_change"}
ASSESS = (
    "Assess these two read-only revision directories against the frozen goal and obligations. "
    "Compare a typical change on both sides by edit locations, dependencies, settings and ease of verification. "
    "Return response as a JSON string with exactly typical_change (an object mapping each revision ID to "
    "its concrete assessment), benefit (null, or an object with revision, explanation, check), concerns "
    "(array of objects with revision, explanation, check), and dispute (null, or an object with revision, "
    "explanation, check for a contested observation that would decide the recommendation). "
    "Cite an existing check ID for each evidence claim. A benefit must cite a passing typical_change check. "
    "A material concern must explain conditions and consequences. Treat unavailable evidence as a gap. "
    "Apply the same criteria to both revision IDs regardless of presentation order."
)
CONFIRM = (
    "Independently assess only the disputed observation using the pinned read-only revisions, "
    "the relevant frozen obligation and the original and repeated paired check. "
    "Return response as a JSON string with exactly verdict (confirmed, rejected or unavailable) and evidence "
    "(a concrete explanation citing the data or source that supports the verdict)."
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _read(path):
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"missing or invalid evaluation artifact: {path.name}")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected nonempty evaluation text")
    return value.strip()


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
        raise ValueError("expected a kebab-case obligation or check ID")
    return value


def _items(values):
    if not isinstance(values, list):
        raise ValueError("expected an array of evaluation records")
    by_id = {}
    for item in values:
        identity = _id(item["id"])
        if identity in by_id:
            raise ValueError(f"duplicate evaluation ID: {identity}")
        by_id[identity] = item
    return by_id


def _basis(goal, obligations, checks, code, tests, timeout, revisions):
    required = {}
    for identity, item in _items(obligations).items():
        required[identity] = {"source": _text(item["source"]), "behavior": _text(item["behavior"])}
    commands = {}
    for identity, item in _items(checks).items():
        argv, kind = item["argv"], item["kind"]
        if (not isinstance(argv, list) or not argv or not isinstance(argv[0], str) or not argv[0]
                or any(not isinstance(arg, str) or "\0" in arg for arg in argv)):
            raise ValueError(f"{identity}: expected a nonempty argv array")
        cwd = item.get("cwd", ".")
        if cwd != ".":
            canonical_path(cwd)
        stdin = item.get("stdin", "")
        fails = item.get("failure_exits", [1])
        if (kind not in CHECK_KINDS or not isinstance(stdin, str) or not isinstance(fails, list)
                or any(type(code) is not int or not 0 < code < 126 for code in fails)):
            raise ValueError(f"{identity}: invalid check kind, input or failure exits")
        obligation = item.get("obligation")
        if (kind == "common" and obligation not in required) or (kind != "common" and obligation is not None):
            raise ValueError(f"{identity}: only a common check must name a frozen obligation")
        applies_to = item.get("for_revision")
        if applies_to is not None and (kind != "acceptance" or applies_to not in revisions):
            raise ValueError(f"{identity}: version-specific acceptance must name a pinned revision")
        commands[identity] = {"argv": argv, "kind": kind, "cwd": cwd, "stdin": stdin,
                              "obligation": obligation, "failure_exits": sorted(set(fails)),
                              "for_revision": applies_to}
    if not required or set(required) != {item["obligation"] for item in commands.values() if item["obligation"]}:
        raise ValueError("every mandatory obligation needs a frozen common check")
    if not any(item["kind"] == "typical_change" for item in commands.values()):
        raise ValueError("provide a paired command measuring a typical maintenance change")
    code, tests = canonical_paths(code), canonical_paths(tests)
    if not code or code & tests:
        raise ValueError("code and test file inventories must be disjoint")
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number of seconds")
    return {"goal": _text(goal), "obligations": required, "checks": commands,
            "code": sorted(code), "tests": sorted(tests), "timeout": timeout}


def _snapshot(vault, revision, root):
    """Materialize exact Git blobs without checkout filters, export attributes or live files."""
    root.mkdir()
    files = []
    for item in _git(vault, "ls-tree", "-rz", revision).stdout.split("\0")[:-1]:
        meta, name = item.split("\t", 1)
        mode, kind, oid = meta.split()
        if kind != "blob":
            raise ValueError(f"expected a Git blob for evaluation: {name}")
        files.append((canonical_path(name), mode, oid))
    queries = "".join(oid + "\n" for _name, _mode, oid in files).encode()
    blobs = io.BytesIO(_git(vault, "cat-file", "--batch", data=queries).stdout)
    for name, mode, oid in files:
        parts = blobs.readline().split()
        if len(parts) != 3 or parts[:2] != [oid.encode(), b"blob"]:
            raise ValueError(f"missing Git blob for evaluation: {name}")
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        size = int(parts[2])
        data = blobs.read(size)
        if len(data) != size or blobs.read(1) != b"\n":
            raise ValueError(f"incomplete Git blob for evaluation: {name}")
        if mode == "120000":
            target = Path(os.fsdecode(data))
            if target.is_absolute():
                raise ValueError(f"absolute symlink in evaluation revision: {name}")
            path.symlink_to(target)
        else:
            path.write_bytes(data)
            path.chmod(int(mode[-3:], 8))
    for path in root.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(root):
            raise ValueError(f"symlink escapes evaluation revision: {path.relative_to(root)}")


def _run(check, source, directory, timeout):
    root = directory / "workspace"
    shutil.copytree(source, root, symlinks=True)
    cwd = root / check["cwd"]
    try:
        if cwd.resolve(strict=True) != cwd or not cwd.is_dir():
            raise ValueError("expected a real worktree-relative check directory")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", LC_ALL="C", TZ="UTC")
        env = {key: value for key, value in env.items() if not key.startswith("GIT_") and key not in {"PYTHONPATH", "PYTHONHOME"}}
        env.update(GIT_CEILING_DIRECTORIES=str(directory), TMPDIR=str(directory))
        with subprocess.Popen(check["argv"], cwd=cwd, env=env, start_new_session=True,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            try:
                stdout, stderr = process.communicate(check["stdin"].encode(), timeout=timeout)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        status = {0: "pass", **dict.fromkeys(check["failure_exits"], "fail")}.get(
            process.returncode, "unavailable")
        return {"status": status, "exit": process.returncode,
                "stdout": stdout.decode(errors="replace"), "stderr": stderr.decode(errors="replace")}
    except subprocess.TimeoutExpired as error:
        return {"status": "unavailable", "error": f"timed out after {timeout:g} seconds",
                "stdout": (error.stdout or b"").decode(errors="replace"),
                "stderr": (error.stderr or b"").decode(errors="replace")}
    except (OSError, ValueError) as error:
        return {"status": "unavailable", "error": str(error)}


def _volume(root, files):
    entries = {}
    for name in files:
        path = _checked_path(root, name)
        if path.exists():
            if not path.is_file():
                raise ValueError(f"volume inventory expects a regular file: {name}")
            data = path.read_bytes()
            entries[name] = {"bytes": len(data), "lines": data.count(b"\n") + bool(data and not data.endswith(b"\n"))}
    return {"files": entries, "lines": sum(item["lines"] for item in entries.values()),
            "bytes": sum(item["bytes"] for item in entries.values())}


def _report(answer, revisions, checks, evidence, requested_dispute):
    if answer.outcome != "success":
        raise RoleExecutionError(answer.response)
    report = json.loads(answer.response, object_pairs_hook=unique_fields)
    if not isinstance(report, dict) or set(report) != {"typical_change", "benefit", "concerns", "dispute"}:
        raise ValueError("invalid evaluator report fields")
    if requested_dispute is not None:
        report["dispute"] = requested_dispute
    if not isinstance(report["typical_change"], dict) or set(report["typical_change"]) != set(revisions):
        raise ValueError("typical change must cover each pinned revision")
    for detail in report["typical_change"].values():
        _text(detail)
    if not isinstance(report["concerns"], list):
        raise ValueError("concerns must be an array")
    for observation in [report["benefit"], report["dispute"], *report["concerns"]]:
        if observation is None:
            continue
        if (not isinstance(observation, dict) or set(observation) != {"revision", "explanation", "check"}
                or observation["revision"] not in revisions or observation["check"] not in checks):
            raise ValueError("observation must cite a pinned revision and a recorded check")
        _text(observation["explanation"])
    if any(item is None for item in report["concerns"]):
        raise ValueError("concerns must name observations")
    benefit = report["benefit"]
    if benefit and (checks[benefit["check"]]["kind"] != "typical_change"
                    or any(item["status"] != "pass" for item in evidence[benefit["check"]].values())):
        raise ValueError("claimed maintenance benefit needs passing paired evidence")
    return report


def _recommend(revisions, checks, evidence, report, confirmation):
    eligible = []
    outcomes = {}
    for revision in revisions:
        relevant = {key: item for key, item in checks.items() if item["kind"] == "common"
                    or item["kind"] == "acceptance" and item["for_revision"] in {None, revision}}
        outcomes[revision] = {evidence[key][revision]["status"] for key in relevant}
        if outcomes[revision] == {"pass"} and not any(
                finding["revision"] == revision for finding in report["concerns"]):
            eligible.append(revision)
    if confirmation and confirmation["verdict"] != "confirmed":
        return None, "The independent check did not confirm the disputed deciding observation."
    if len(eligible) == 1:
        other = next(revision for revision in revisions if revision not in eligible)
        if "fail" in outcomes[other]:
            return eligible[0], "The other revision failed a common obligation or applicable Acceptance check."
    if any("unavailable" in outcomes[revision] for revision in revisions):
        return None, "A mandatory comparison or applicable Acceptance check is unavailable."
    if len(eligible) == 2 and report["benefit"]:
        return report["benefit"]["revision"], report["benefit"]["explanation"]
    return None, "The evidence did not establish an eligible revision with a demonstrated advantage."


def _contract(entry, section=None):
    """Compare contract text while a ticket's report and replies evolve in the same note."""
    document = _Document(entry.data)
    left, right = 0, len(entry.data)
    if section:
        selected = document.section(section, root_level=2, optional=True)
        if selected is None:
            return None
        left, right = selected.body, selected.end
    fragments = []
    for start, end in document.protected:
        if start < right and end > left:
            fragments.append(entry.data[left:max(left, start)])
            left = end
    fragments.append(entry.data[left:right])
    body = b"".join(fragments).strip()
    return entry.kind, entry.mode & 0o100, body


class Evaluation:
    def __init__(self, vault, *, ticket_id, slug, block, plan, attempt=1, settings=None, runner=run_role):
        self.vault = Path(vault).resolve(strict=True)
        self.identity = dict(ticket_id=ticket_id, slug=slug, attempt=attempt)
        self.scope = {**self.identity, "block": note_path(block, "Sources"), "plan": note_path(plan, "Plans")}
        verify_worktree(self.vault, **self.identity)
        self.directory = _checked_path(self.vault, f".obsta-experiments/{ticket_id}/evaluation-{slug}-{attempt}")
        self.settings, self.runner = settings, runner

    def _pins(self, *, verify_execution=False):
        selected = verify_worktree(self.vault, **self.identity)
        block, plan = self.scope["block"], self.scope["plan"]
        _clean(selected.path)
        _check_untracked(selected.path, block)
        candidate = capture(selected.path)
        candidate_scope = block_scope(candidate, block)
        if verify_execution:
            verify_completed_execution(selected, candidate, block, plan, "refactoring")
        old, new = selected.base, _line(selected.path, "rev-parse", "HEAD")
        main = _line(self.vault, "rev-parse", "HEAD")
        if old == new:
            raise ValueError("evaluation requires distinct old and new Git revisions")

        candidate_paths = [name for name, entry in candidate.items() if entry.kind != "directory"
                           and _inside(name, candidate_scope.implementation, candidate_scope.kind)]
        _check_checkout(selected.path, new, candidate, sorted({block, plan, *candidate_paths}))
        baseline = _revision(self.vault, old, block, plan)
        live = capture(self.vault)
        scope = block_scope(baseline, block)
        current = block_scope(live, block)
        old_paths = {name for name, entry in baseline.items() if entry.kind != "directory"
                     and _inside(name, scope.implementation, scope.kind)}
        live_paths = _git(self.vault, "--literal-pathspecs", "ls-files", "--cached", "--others",
                          "--exclude-standard", "-z", "--", scope.implementation).stdout.split("\0")[:-1]
        files = old_paths.union(live_paths) - {block, *scope.descendants}
        if files:
            _check_checkout(self.vault, old, live, sorted(files))
        if scope.descendants != current.descendants:
            raise ValueError("live descendant contracts differ from the frozen baseline")
        if _contract(live[block], "User requirements") != _contract(baseline[block], "User requirements"):
            raise ValueError("live User requirements differ from the frozen baseline")
        for note in scope.descendants:
            if _contract(live[note]) != _contract(baseline[note]):
                raise ValueError(f"live descendant contract differs from the frozen baseline: {note}")
        return {"old": old, "new": new, "main_head": main, "branch": selected.branch}

    def _current(self, pins):
        stale = "stale evaluation: repeat the comparison and obtain a new choice"
        try:
            current = self._pins()
        except (ValueError, subprocess.CalledProcessError) as error:
            raise ValueError(f"{stale} ({error})") from error
        if current != pins:
            raise ValueError(f"{stale} (Git revisions changed)")

    @contextmanager
    def _lock(self):
        directory = _checked_path(self.vault, self.directory.relative_to(self.vault).as_posix())
        directory.mkdir(parents=True, exist_ok=True)
        lock = os.open(directory / ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock, "a"):
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield directory

    def prepare(self, *, accepted_head, goal, obligations, checks, code, tests, timeout=120):
        """Freeze common obligations, commands and Git revisions before evaluator inspection."""
        with self._lock() as directory:
            pins = self._pins(verify_execution=True)
            if accepted_head != pins["new"]:
                raise ValueError("candidate must match the accepted full Git revision")
            basis = _basis(goal, obligations, checks, code, tests, timeout, {pins["old"], pins["new"]})
            frozen = {"scope": self.scope, "pins": pins, "basis": basis}
            identity = _digest(frozen)
            _save(directory / f"basis-{identity}.json", frozen)
        return {"status": "prepared", "basis_id": identity, "revisions": pins}

    def _role(self, directory, task, context):
        return self.runner("evaluator", self.settings, cwd=directory, task=task, context=_json(context),
                           allowed_paths=[], allowed_sections={}, checks=[], read_only=True)

    def _compare(self, directory, frozen, requested_dispute):
        basis, pins = frozen["basis"], frozen["pins"]
        revisions = sorted([pins["old"], pins["new"]])
        checks = basis["checks"]
        with tempfile.TemporaryDirectory(prefix="compare-", dir=directory) as working:
            working = Path(working)
            sources = working / "revisions"
            sources.mkdir()
            for revision in revisions:
                _snapshot(self.vault, revision, sources / revision)
            volumes = {revision: {kind: _volume(sources / revision, basis[kind]) for kind in ("code", "tests")}
                       for revision in revisions}
            if any(not any(name in volume[kind]["files"] for volume in volumes.values())
                   for kind in ("code", "tests") for name in basis[kind]):
                raise ValueError("volume inventory lists a file missing from both pinned revisions")

            def measure(check):
                results = {}
                for revision in revisions:
                    with tempfile.TemporaryDirectory(prefix="check-", dir=working) as area:
                        results[revision] = _run(check, sources / revision, Path(area), basis["timeout"])
                return results

            evidence = {name: measure(check) for name, check in checks.items()}
            context = {"goal": basis["goal"], "obligations": basis["obligations"], "checks": checks,
                       "evidence": evidence, "volumes": volumes}
            report = _report(self._role(sources, ASSESS, context), revisions, checks, evidence, requested_dispute)
            confirmation = None
            if dispute := report["dispute"]:
                check = dispute["check"]
                observed = evidence[check]
                favored, _reason = _recommend(revisions, checks, evidence, report, None)
                disputed_revision = dispute["revision"]
                without_observation = {**evidence, check: {**observed, disputed_revision: {"status": "unavailable"}}}
                without_claim = report
                if report["benefit"] and (disputed_revision, check) == (
                        report["benefit"]["revision"], report["benefit"]["check"]):
                    without_claim = {**report, "benefit": None}
                remaining, _reason = _recommend(revisions, checks, without_observation, without_claim, None)
                if favored is None or favored == remaining:
                    raise ValueError("dispute must identify a deciding recorded observation")
                repeated = measure(checks[check])
                obligation = basis["obligations"].get(checks[check]["obligation"])
                brief = {"dispute": dispute, "obligation": obligation, "check": checks[check],
                         "original": observed, "repeated": repeated}
                answer = self._role(sources, CONFIRM, brief)
                if answer.outcome != "success":
                    raise RoleExecutionError(answer.response)
                response = json.loads(answer.response, object_pairs_hook=unique_fields)
                if (not isinstance(response, dict) or set(response) != {"verdict", "evidence"}
                        or response["verdict"] not in {"confirmed", "rejected", "unavailable"}):
                    raise ValueError("invalid independent observation report")
                _text(response["evidence"])
                if any(observed[revision]["status"] != repeated[revision]["status"] for revision in revisions):
                    response["verdict"] = "unavailable"
                    response["evidence"] += " The repeated check changed status."
                confirmation = {**brief, **response}
        revision, reason = _recommend(revisions, checks, evidence, report, confirmation)
        return {"evidence": evidence, "volumes": volumes, "assessment": report, "confirmation": confirmation,
                "recommendation": {"revision": revision, "reason": reason},
                "environment": {"platform": platform.platform(), "python": sys.version,
                                "LC_ALL": "C", "TZ": "UTC", "PYTHONDONTWRITEBYTECODE": "1"}}

    def evaluate(self, *, basis_id, dispute=None):
        with self._lock() as directory:
            frozen = self._artifact(directory, "basis", basis_id)
            self._current(frozen["pins"])
            report = {**frozen, "basis_id": basis_id, **self._compare(directory, frozen, dispute)}
            self._current(frozen["pins"])
            identity = _digest(report)
            path = directory / f"report-{identity}.json"
            _save(path, report)
        revision = report["recommendation"]["revision"]
        favored = next((label for label in ACTIONS if report["pins"][label] == revision), None)
        volume = ", ".join(f"{label} {report['volumes'][report['pins'][label]]['code']['lines']} code / "
                           f"{report['volumes'][report['pins'][label]]['tests']['lines']} test lines" for label in ACTIONS)
        gaps = []
        for check_id, results in report["evidence"].items():
            check = report["basis"]["checks"][check_id]
            for label in ACTIONS:
                revision = report["pins"][label]
                status = results[revision]["status"]
                if status == "pass" or check["for_revision"] not in {None, revision}:
                    continue
                obligation = report["basis"]["obligations"].get(check["obligation"])
                impact = f" ({' '.join(obligation['behavior'].split())})" if obligation else ""
                gaps.append(f"{label} {check_id} {status}{impact}")
        effects = f" Check results requiring attention ({', '.join(gaps)})." if gaps else ""
        summary = (f"*Recommendation {favored or 'undetermined'}. {report['recommendation']['reason']}{effects} "
                   f"Volume in lines, {volume}. Reply with exactly `old {identity}` or `new {identity}`.*")
        return {"status": "await_choice", "report_id": identity, "report_path": path.relative_to(self.vault).as_posix(),
                "recommendation": favored, "response": summary}

    def _artifact(self, directory, kind, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ValueError("expected the exact evaluation fingerprint")
        artifact = _read(directory / f"{kind}-{identity}.json")
        if _digest(artifact) != identity or artifact["scope"] != self.scope:
            raise ValueError("evaluation artifact differs from its fingerprint or selected refactoring scope")
        return artifact

    def select(self, *, reply):
        """Capture an exact reply sourced from the user, never from the evaluator."""
        if not isinstance(reply, str) or not (match := re.fullmatch(r"(old|new) ([0-9a-f]{64})", reply)):
            raise ValueError("expected the user's exact old/new reply and report fingerprint")
        choice, identity = match.groups()
        with self._lock() as directory:
            report = self._artifact(directory, "report", identity)
            self._current(report["pins"])
            handoff = {"scope": self.scope, "pins": report["pins"], "report_id": identity,
                       "reply": reply, "action": ACTIONS[choice], "revision": report["pins"][choice]}
            path = directory / f"choice-{identity}.json"
            if path.exists() and _read(path) != handoff:
                raise ValueError("a different user choice is already recorded for this report")
            _save(path, handoff)
        return {"status": "selected", **handoff}

    def validate_choice(self, *, report_id, action):
        """Revalidate the persisted choice immediately before row 15 begins its operation."""
        with self._lock() as directory:
            report = self._artifact(directory, "report", report_id)
            handoff = _read(directory / f"choice-{report_id}.json")
            choice = next((label for label, value in ACTIONS.items() if action == value), None)
            expected = {"scope": self.scope, "pins": report["pins"], "report_id": report_id,
                        "reply": f"{choice} {report_id}", "action": action, "revision": report["pins"].get(choice)}
            if choice is None or handoff != expected:
                raise ValueError("requested action does not match the persisted user's choice")
            self._current(report["pins"])
            return expected


def main():
    try:
        request = json.load(sys.stdin, object_pairs_hook=unique_fields)
        evaluation = Evaluation(**request["case"])
        steps = {"prepare": evaluation.prepare, "evaluate": evaluation.evaluate,
                 "select": evaluation.select, "validate_choice": evaluation.validate_choice}
        result = steps[request["action"]](**request["arguments"])
        print(_json(result))
    except (KeyError, OSError, RoleExecutionError, TypeError, ValueError, subprocess.SubprocessError) as error:
        print(_json({"status": "error", "error": str(error)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
