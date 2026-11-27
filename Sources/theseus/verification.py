"""Verify the accepted worktree revision and collect evidence for independent final review."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

if __package__:
    from .codex import RoleExecutionError, run_role, unique_fields
    from .execution import _check_checkout, _clean, _fingerprint, verify_completed_execution
    from .notes import read_section, replace_section
    from .planning import ACCEPTANCE, cell, line
    from .review import _git_bytes, _report, _run_reviewer
    from .routing import canonical_path, note_path
    from .scope import Entry, _inside, authorize, block_scope, capture
    from .worktree import OBJECT_ID, _git, _line, verify_worktree
else:
    from codex import RoleExecutionError, run_role, unique_fields
    from execution import _check_checkout, _clean, _fingerprint, verify_completed_execution
    from notes import read_section, replace_section
    from planning import ACCEPTANCE, cell, line
    from review import _git_bytes, _report, _run_reviewer
    from routing import canonical_path, note_path
    from scope import Entry, _inside, authorize, block_scope, capture
    from worktree import OBJECT_ID, _git, _line, verify_worktree


CATEGORIES = ("acceptance", "tests", "mandatory")
DIFF_OPTIONS = (
    "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color",
    "--ignore-submodules=none", "--src-prefix=a/", "--dst-prefix=b/",
)
REVIEW_TASK = (
    "Independently review the entire supplied Git diff, the applicable block requirements and "
    "Acceptance, the checks rerun on the final head, and accepted task evidence. "
    "Use task_evidence for pre-fix red observations and per-task green results. "
    "When reviewing refactoring, distinguish the checkout diff from publication. "
    "The listed checkout_only_plan_deletions belong to the confirmed isolated cleanup "
    "and the return helper keeps those historical plans in the main vault. "
    "Check all other changes against the requirements and return scope. "
    "Consult files at the "
    "specified revisions as needed. Recheck every item in prior_blockers against the changed diff. "
    "Identify missing applicable checks, unmet requirements, and "
    "substantial risks as blockers. Return response as JSON with base, head, diff_sha256, and "
    "verification_sha256 copied from the brief, findings (each with id, severity blocker or advisory, "
    "and detail naming the requirement, location and evidence), and dispositions (one per prior blocker "
    "with id, status fixed or open, and detail describing your evidence). Use an empty dispositions array "
    "if there are no prior blockers. "
    "Return outcome success for a completed report, including reports with blockers."
)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _save(path, state):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        pending = Path(file.name)
        try:
            file.write(_json(state))
            file.flush()
            os.fsync(file.fileno())
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)


def _manifest(live, scopes, files, checks):
    required = {scopes[0].note}
    for scope in scopes[1:]:
        if any(path == scope.note or _inside(path, scope.implementation, scope.kind) for path in files):
            required.add(scope.note)
    if not isinstance(checks, dict):
        raise ValueError("supply an explicit verification manifest per applicable block")
    if missing := sorted(required - checks.keys()):
        raise ValueError(f"missing verification manifest for {', '.join(missing)}")
    if unexpected := sorted(checks.keys() - {scope.note for scope in scopes}):
        raise ValueError(f"verification manifest outside the selected block: {', '.join(unexpected)}")
    requirements, commands = {}, []
    for scope in scopes:
        note = scope.note
        if note not in checks:
            continue
        if not read_section(live[note].data, ACCEPTANCE, root_level=2).strip():
            raise ValueError(f"missing Acceptance criteria for {note}")
        requirements[note] = live[note].data.decode("utf-8")
        groups = checks[note]
        if not isinstance(groups, dict) or set(groups) != set(CATEGORIES):
            raise ValueError(f"{note}: specify acceptance, tests and mandatory check lists")
        for category in CATEGORIES:
            entries = groups[category]
            if not isinstance(entries, list) or category == "acceptance" and not entries:
                raise ValueError(f"{note}: missing {category} commands")
            for entry in entries:
                if not isinstance(entry, dict) or set(entry) != {"command", "cwd"}:
                    raise ValueError(f"{note}: {category} check requires command and cwd")
                command = line(entry["command"])
                argv = shlex.split(command)
                if not argv or not argv[0].strip() or argv[0] in {"TBD", "TODO"}:
                    raise ValueError(f"{note}: {category} check requires a concrete command")
                cwd = entry["cwd"]
                if cwd != ".":
                    canonical_path(cwd)
                commands.append({"block": note, "category": category, "command": command, "cwd": cwd})
    return requirements, commands


def _reported_files(live, block, changed):
    selected = block_scope(live, block)
    scopes = [selected, *(block_scope(live, note) for note in selected.descendants)]
    files = {scope.note: sorted(path for path in changed
                                if path == scope.note or _inside(path, scope.implementation, scope.kind))
             for scope in scopes}
    return scopes, files


def _run_check(root, item, timeout):
    cwd = root / item["cwd"]
    result = {"exit": None, "output": ""}
    try:
        if cwd.resolve(strict=True) != cwd or not cwd.is_dir():
            raise ValueError(f"unsafe verification directory: {item['cwd']}")
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        completed = subprocess.run(item["command"], shell=True, cwd=cwd, capture_output=True,
                                   env=dict(env, PYTHONDONTWRITEBYTECODE="1"), timeout=timeout)
        result.update(exit=completed.returncode,
                      output=(completed.stdout + completed.stderr)[-4000:].decode(errors="replace"))
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        result["error"] = str(error)
    return result


def _result(status, version, commands, results, review, files, problems):
    evidence = []
    for index, item in enumerate(commands):
        observed = results.get(str(index))
        outcome = "pending"
        if observed is not None:
            outcome = "passed" if observed["exit"] == 0 else "failed"
        evidence.append({**item, "status": outcome, **(observed or {})})
    return {"status": status, "version": version, "checks": evidence,
            "review": review, "files": files, "problems": problems}


def _open_findings(review):
    new = [item for item in review["findings"] if item["severity"] == "blocker"]
    still_open = [{"id": item["id"], "severity": "blocker", "detail": item["detail"]}
                  for item in review["dispositions"] if item["status"] == "open"]
    return new + still_open


def _blockers(review):
    return [item["detail"] for item in _open_findings(review)]


def _pending_repair(state, scope, accepted_head, repair_of):
    if repair_of is None:
        return None
    if not isinstance(repair_of, str) or not re.fullmatch(OBJECT_ID, repair_of) or repair_of == accepted_head:
        raise ValueError("repair_of must name the full preceding revision")
    if state.get("scope", scope) != scope:
        raise ValueError("final review repair belongs to another plan")
    repairs = state.get("repairs", [])
    if repairs and (repairs[-1]["head"], repairs[-1]["parent"]) == (accepted_head, repair_of):
        return None
    if (state.get("invalid") or state.get("active") is not None or state.get("receipt")
            or state.get("version", {}).get("head") != repair_of):
        raise ValueError("repair_of must identify the revision with a recorded blocking final review or failed repair check")
    previous = state.get("review")
    if previous and all(previous[key] == state["version"][key] for key in ("head", "base", "diff_sha256")):
        if _blockers(previous):
            return {"head": accepted_head, "parent": repair_of, "previous_review": previous}
    if repairs and not previous:
        failed = next((check for check in state["results"].values()
                       if check["revision"] == repair_of and type(check["exit"]) is int and check["exit"] != 0), None)
        if failed is not None:
            return {"head": accepted_head, "parent": repair_of, "failed_check": failed}
    raise ValueError("repair_of must identify the revision with a recorded blocking final review or failed repair check")


def _verify(vault, selected, identity, block, plan, work, accepted_head, checks, settings, timeout, runner,
            journal, state, repair_of):
    root = selected.path
    version, requirements, commands, files, results, review = {}, {}, [], {}, {}, None

    def unchanged(initial):
        if verify_worktree(vault, **identity) != selected or _line(root, "rev-parse", "HEAD") != accepted_head:
            raise ValueError("accepted worktree revision changed during verification")
        _clean(root)
        if initial is not None and capture(root) != initial:
            raise ValueError("worktree files changed during verification")

    try:
        if not isinstance(accepted_head, str) or not re.fullmatch(OBJECT_ID, accepted_head):
            raise ValueError("verification requires the full accepted Git revision")
        unchanged(None)
        if _git(root, "merge-base", "--is-ancestor", selected.base, accepted_head, check=False).returncode:
            raise ValueError("accepted revision is outside the owned worktree history")
        changed = _git(root, "diff", *DIFF_OPTIONS, "--name-only", "-z",
                       selected.base, accepted_head, "--").stdout.split("\0")[:-1]
        diff = _git_bytes(root, "diff", *DIFF_OPTIONS, "--binary", "--full-index",
                          selected.base, accepted_head, "--").stdout
        version = {"base": selected.base, "head": accepted_head, "diff_sha256": _digest(diff)}
        live = capture(root)
        if changed:
            _check_checkout(root, accepted_head, live, changed)
        _check_checkout(root, accepted_head, live, [block, plan])
        scopes, files = _reported_files(live, block, changed)
        if plan not in live or live[plan].kind != "file":
            raise ValueError("missing execution plan")
        scope = [block, plan, work]
        pending_repair = _pending_repair(state, scope, accepted_head, repair_of)
        task_evidence = []
        tasks = verify_completed_execution(selected, live, block, plan, work, pending_repair=pending_repair,
                                           evidence=task_evidence)
        requirements, commands = _manifest(live, scopes, changed, checks)
        registered = {(command["command"], command["cwd"]) for command in commands}
        for task in tasks:
            for check in task.checks:
                if (check.command, check.cwd) not in registered:
                    raise ValueError(f"missing required plan check: {check.command} (cwd {check.cwd})")
        requirements[plan] = live[plan].data.decode("utf-8")
        version["checkout_sha256"] = _fingerprint(live, set())
        version["manifest_sha256"] = _digest(_json([checks, requirements]).encode())
        if state.get("version") != version or state.get("invalid"):
            prior_review = state.get("review")
            if prior_review is not None and state["version"]["head"] == accepted_head:
                if blockers := _blockers(prior_review):
                    return _result("blocked", version, commands, results, None, files, blockers)
            repairs = state.get("repairs", [])
            if pending_repair is not None:
                repairs = [*repairs, pending_repair]
            unchanged(live)
            state.clear()
            state.update(scope=scope, repairs=repairs, version=version, attempts=[], results={}, review=None, active=None)
            _save(journal, state)
        results, review = state["results"], state["review"]
        for index, command in enumerate(commands):
            key = str(index)
            if results.get(key, {}).get("exit") == 0 and state["active"] != key:
                continue
            unchanged(live)
            state["active"] = key
            _save(journal, state)
            observation = _run_check(root, command, timeout)
            state["attempts"].append({**command, **observation, "revision": accepted_head})
            try:
                unchanged(live)
            except (OSError, ValueError, subprocess.SubprocessError):
                state["invalid"] = True
                _save(journal, state)
                raise
            results[key] = {"revision": accepted_head, **observation}
            state["active"] = None
            _save(journal, state)
            if observation["exit"] != 0:
                detail = observation.get("error") or f"exit {observation['exit']}"
                raise ValueError(f"{command['block']}: {command['command']} (cwd {command['cwd']}): {detail}")
        unchanged(live)
        verification_sha256 = _digest(_json([version, commands, results]).encode())
        if review is None:
            local_plan_deletions = []
            if work == "refactoring":
                deleted = _git(root, "diff", *DIFF_OPTIONS, "--diff-filter=D", "--name-only", "-z",
                               selected.base, accepted_head, "--", "Plans/").stdout.split("\0")[:-1]
                local_plan_deletions = [path for path in deleted if path != plan]
            brief = {**version, "verification_sha256": verification_sha256, "requirements": requirements,
                     "work": work, "current_plan": plan, "checkout_only_plan_deletions": local_plan_deletions,
                     "diff": diff.decode("utf-8", errors="backslashreplace"),
                     "task_evidence": [asdict(candidate) for candidate in task_evidence],
                     "checks": _result("review", version, commands, results, None, files, [])["checks"]}
            previous = next((repair["previous_review"] for repair in reversed(state.get("repairs", []))
                             if "previous_review" in repair), None)
            prior_blockers = _open_findings(previous) if previous is not None else []
            if previous is not None:
                brief["previous_final_review"] = previous
                brief["prior_blockers"] = prior_blockers
            state["active"] = "review"
            _save(journal, state)
            answer = _run_reviewer(runner, settings, cwd=root, task=REVIEW_TASK, brief=brief)
            unchanged(live)
            if answer.outcome != "success":
                raise RoleExecutionError(answer.response)
            report = json.loads(answer.response, object_pairs_hook=unique_fields)
            if not isinstance(report, dict) or report.pop("verification_sha256", None) != verification_sha256:
                raise ValueError("final review does not match the actual mandatory check evidence")
            changed_diff = previous is not None and previous["diff_sha256"] != version["diff_sha256"]
            review = _report(_json(report), version, prior_blockers, set(), changed_diff)
            state.update(review={**review, "verification_sha256": verification_sha256}, active=None)
            review = state["review"]
            _save(journal, state)
        if review["verification_sha256"] != verification_sha256:
            raise ValueError("recorded review does not match the actual mandatory check evidence")
        blockers = _blockers(review)
        unchanged(live)
        if not blockers:
            receipt = {"block": block, "plan": plan, "work": work, "commands": commands}
            if state.get("receipt") != receipt:
                state["receipt"] = receipt
                _save(journal, state)
        return _result("blocked" if blockers else "verified", version, commands, results, review, files, blockers)
    except (KeyError, OSError, RoleExecutionError, TypeError, ValueError, subprocess.SubprocessError) as error:
        return _result("blocked", version, commands, results, review, files, [str(error)])


def run_verification(vault, *, ticket_id, slug, block, plan, work, accepted_head, checks, settings, attempt=1,
                     timeout=300, runner=run_role, repair_of=None):
    """Verify a plan result or a scoped correction to a recorded blocking final review."""
    note_path(block, "Sources")
    note_path(plan, "Plans")
    identity = dict(ticket_id=ticket_id, slug=slug, attempt=attempt)
    selected = verify_worktree(vault, **identity)
    admin = Path(_line(selected.path, "rev-parse", "--absolute-git-dir"))
    journal = admin / "theseus-verification.json"
    with os.fdopen(os.open(journal.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
                           0o600), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if journal.is_symlink() or journal.exists() and not journal.is_file():
            raise ValueError("invalid verification journal path")
        state = json.loads(journal.read_text(), object_pairs_hook=unique_fields) if journal.exists() else {}
        return _verify(vault, selected, identity, block, plan, work, accepted_head, checks, settings, timeout, runner,
                       journal, state, repair_of)


def verified_receipt(selected, *, block, plan, work, accepted_head):
    """Read completed check and review evidence bound to the actual accepted checkout."""
    root = selected.path
    _clean(root)
    admin = Path(_line(root, "rev-parse", "--absolute-git-dir"))
    journal = admin / "theseus-verification.json"
    error = "missing or invalid final verification receipt for the accepted revision"
    if journal.is_symlink() or not journal.is_file():
        raise ValueError(error)
    state = json.loads(journal.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)
    receipt, version = state.get("receipt"), state.get("version")
    if (not receipt or not version or state.get("invalid") or state.get("active") is not None
            or (receipt["block"], receipt["plan"], receipt["work"]) != (block, plan, work)
            or (version["base"], version["head"]) != (selected.base, accepted_head)
            or _line(root, "rev-parse", "HEAD") != accepted_head):
        raise ValueError(error)
    diff = _git_bytes(root, "diff", *DIFF_OPTIONS, "--binary", "--full-index",
                      selected.base, accepted_head, "--").stdout
    live = capture(root)
    if version["diff_sha256"] != _digest(diff) or version["checkout_sha256"] != _fingerprint(live, set()):
        raise ValueError(error)
    review, results, commands = state.get("review"), state.get("results"), receipt["commands"]
    evidence = _digest(_json([version, commands, results]).encode())
    if (not commands or set(results) != {str(index) for index in range(len(commands))}
            or any(result["revision"] != accepted_head or result["exit"] != 0
                   for result in results.values())
            or not review or review["verification_sha256"] != evidence or _blockers(review)
            or any(review[key] != version[key] for key in ("base", "head", "diff_sha256"))):
        raise ValueError(error)
    changed = _git(root, "diff", *DIFF_OPTIONS, "--name-only", "-z",
                   selected.base, accepted_head, "--").stdout.split("\0")[:-1]
    _scopes, files = _reported_files(live, block, changed)
    return evidence, report_updates(live, _result("verified", version, commands, results, review, files, []),
                                    block=block, work=work, plan=plan)


def report_updates(live, result, *, block, work, plan):
    """Prepare authorized report updates without changing the reviewed worktree."""
    updates, sections = {}, {}

    def append(path, selector, paragraphs):
        before = updates.get(path, live[path])
        body = read_section(before.data, selector, root_level=2)
        newline = b"\r\n" if b"\r\n" in before.data else b"\n"
        rendered = newline.join(f"*{cell(line(text))}*".encode() for text in paragraphs)
        data = replace_section(before.data, selector, body + newline + rendered + newline * 2,
                               root_level=2)
        updates[path] = Entry("file", data, before.mode)

    files = result["files"]
    problems = [" ".join(detail.split()) for detail in result["problems"]]
    report_notes = {block, *(note for note, paths in files.items() if paths and work != "refactoring")}
    for note in sorted(report_notes):
        selectors = [("Working context", "Files"), ("Working context", "Problems")]
        sections[note] = selectors
        entry = live[note]
        missing = [selector[-1] for selector in selectors
                   if read_section(entry.data, selector, root_level=2, optional=True) is None]
        if missing:
            context = ("Working context",)
            newline = b"\r\n" if b"\r\n" in entry.data else b"\n"
            body = read_section(entry.data, context, root_level=2) + newline
            for title in missing:
                body += f"### *{title}*".encode() + newline * 2
            data = replace_section(entry.data, context, body, root_level=2)
            updates[note] = Entry("file", data, entry.mode)
            sections[note] = [context]
        append(note, selectors[0], ["Changed paths"] + (files.get(note) or ["None established"]))
        append(note, selectors[1], problems or ["No verification blockers"])
    status = result["status"]
    version = result["version"].get("head", "unverified")
    summary = [f"Final verification {status} at {version}"]
    if result["review"] is not None:
        summary.append(f"Final review bound to diff {result['version']['diff_sha256']}")
    summary += [f"{item['block']} · {item['category']} · {item['command']} · cwd {item['cwd']} · {item['status']}"
                + (f" · exit {item['exit']}" if item.get("exit") is not None else "") for item in result["checks"]]
    append(plan, ("Outcome", "Final verification"), summary)
    if problems:
        append(plan, ("Outcome", "Interruption and next step"), problems)
    authorize(live, {**live, **updates}, block, role="orchestrator", stage="execute", work=work,
              plan=plan, files=(), sections=sections)
    return updates


def main():
    try:
        result = run_verification(**json.load(sys.stdin, object_pairs_hook=unique_fields))
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
        print(_json({"error": str(error)}))
        return 1
    print(_json(result))
    return 0 if result["status"] == "verified" else 1


if __name__ == "__main__":
    sys.exit(main())
