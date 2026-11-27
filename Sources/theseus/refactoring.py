"""Prepare and plan an explicitly confirmed refactoring in an owned linked worktree."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path

if __package__:
    from .codex import RoleExecutionError, run_role
    from .notes import clear_section, read_section
    from .planning import ACCEPTANCE, ARCHITECTURE, STATUS, Result, check_plan_context, run_planning
    from .routing import Decision, canonical_paths, note_path, require_boolean, unique_fields
    from .scope import Entry, _belongs, authorize, block_scope, capture
    from .worktree import OBJECT_ID, Worktree, _clean, _git, _line, prepare_worktree, verify_worktree
    from .worktree import _commit_files as _commit, _publish_commit as _publish, _save_record as _save
else:
    from codex import RoleExecutionError, run_role
    from notes import clear_section, read_section
    from planning import ACCEPTANCE, ARCHITECTURE, STATUS, Result, check_plan_context, run_planning
    from routing import Decision, canonical_paths, note_path, require_boolean, unique_fields
    from scope import Entry, _belongs, authorize, block_scope, capture
    from worktree import OBJECT_ID, Worktree, _clean, _git, _line, prepare_worktree, verify_worktree
    from worktree import _commit_files as _commit, _publish_commit as _publish, _save_record as _save


@dataclass(frozen=True)
class Refactoring:
    worktree: Worktree
    cleanup: str
    execution_base: str | None
    planning: Result


def _admin(selected):
    return Path(_line(selected.path, "rev-parse", "--absolute-git-dir"))


def _revision(root, commit, block, plan):
    """Capture the committed tree. Scope tests need Markdown blobs and other files' IDs."""
    state = {}
    for item in _git(root, "ls-tree", "-rtz", commit).stdout.split("\0")[:-1]:
        meta, path = item.split("\t", 1)
        mode, kind, object_id = meta.split()
        if kind == "tree":
            state[path] = Entry("directory")
        elif mode in {"100644", "100755"}:
            read_markdown = path in {block, plan} or path.startswith(block[:-3] + "/") and path.endswith(".md")
            data = (_git(root, "cat-file", "blob", object_id, data=b"").stdout
                    if read_markdown else object_id.encode())
            state[path] = Entry("file", data, int(mode[-3:], 8))
        else:
            state[path] = Entry("symlink" if mode == "120000" else "special", object_id.encode())
    return state


def _cleanup(before, block):
    selected = block_scope(before, block)
    descendants = set(selected.descendants)
    after = {
        path: entry for path, entry in before.items()
        if not (path == "Plans" or path.startswith("Plans/")
                or entry.kind != "directory" and _belongs(path, selected, descendants, "refactoring"))
    }
    original = before[block]
    data = clear_section(original.data, "Specification", root_level=2)
    data = clear_section(data, "Working context", root_level=2)
    after[block] = replace(original, data=data)
    return authorize(before, after, block, role="obstagent", stage="cleanup", work="refactoring")


def _changes(before, after):
    return {path: after.get(path) for path in set(before) | set(after)
            if before.get(path) != after.get(path)
            and not all(entry is None or entry.kind == "directory"
                        for entry in (before.get(path), after.get(path)))}


def _load(selected, block, plan):
    path = _admin(selected) / "theseus-refactoring.json"
    if path.is_symlink() or path.exists() and not path.is_file():
        raise ValueError("invalid refactoring setup record")
    if not path.exists():
        return path, None
    record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)
    if (not isinstance(record, dict) or set(record) != {"base", "block", "plan", "cleanup", "execution_base"}
            or (record["base"], record["block"], record["plan"]) != (selected.base, block, plan)
            or any(not isinstance(record[key], str) or not re.fullmatch(OBJECT_ID, record[key])
                   for key in ("cleanup", "execution_base") if key == "cleanup" or record[key] is not None)):
        raise ValueError("refactoring setup belongs to another block, plan, or input base")
    return path, record


def _full_parent(root, commit, parent):
    if _line(root, "rev-list", "--parents", "-1", commit).split() != [commit, parent]:
        raise ValueError("refactoring setup is outside its recorded single-parent history")


def _validate(selected, record):
    root = selected.path
    base, cleanup, planned = record["base"], record["cleanup"], record["execution_base"]
    block, plan = record["block"], record["plan"]
    _full_parent(root, cleanup, base)
    before, clean = _revision(root, base, block, plan), _revision(root, cleanup, block, plan)
    expected = {change.path: change.after for change in _cleanup(before, block)}
    if _changes(before, clean) != expected:
        raise ValueError("cleanup commit differs from the authorized refactoring cleanup")
    if planned is not None:
        _full_parent(root, planned, cleanup)
        after = _revision(root, planned, block, plan)
        changes = authorize(clean, after, block, role="planner", stage="plan", work="refactoring",
                            plan=plan, sections={block: [("Specification",)]})
        if {change.path for change in changes} != {block, plan}:
            raise ValueError("planning commit must contain the new Specification and plan")
        check_plan_context(after[plan].data, block, "refactoring")
        if read_section(after[plan].data, STATUS, root_level=2).strip() != b"*Ready*":
            raise ValueError("refactoring plan was not committed as Ready")


def _check_untracked(root, block):
    """Refuse uncommitted implementation or plan files, including ignored ones."""
    state = capture(root)
    selected = block_scope(state, block)
    descendants = set(selected.descendants)
    for path in _git(root, "ls-files", "--others", "-z").stdout.split("\0")[:-1]:
        if path == "Plans" or path.startswith("Plans/") or _belongs(path, selected, descendants, "refactoring"):
            raise ValueError(f"untracked refactoring input: {path}")


def refactoring_execution_base(selected, block, plan):
    """Validate the recorded setup before accepting task execution history."""
    path, record = _load(selected, block, plan)
    if record is None or record["execution_base"] is None:
        raise ValueError(f"missing prepared refactoring plan in {path.name}")
    _validate(selected, record)
    head = record["execution_base"]
    if _git(selected.path, "merge-base", "--is-ancestor", head, "HEAD", check=False).returncode:
        raise ValueError("the prepared refactoring plan is outside the current history")
    return head


def run_refactoring_planning(vault, decision, *, requested, confirmed, ticket_id, slug, ticket,
                              plan_path=None, snapshot_a=None, inputs=(), attempt=1, files=(),
                              settings=None, context="", runner=run_role):
    """Apply authorized cleanup once and give the planner the cleaned workspace."""
    require_boolean(requested, "requested")
    require_boolean(confirmed, "confirmed")
    if not requested or not confirmed:
        raise ValueError("refactoring requires an explicit request and confirmation of the cleanup")
    if (not isinstance(decision, Decision) or decision.work != "refactoring"
            or decision.stage not in {"prepare_refactoring", "plan"}):
        raise ValueError("expected a routed refactoring planning decision")
    block = note_path(decision.block, "Sources")
    plan = note_path(decision.plan or plan_path, "Plans")
    if decision.plan and plan_path is not None and plan_path != decision.plan:
        raise ValueError("plan path differs from the routed plan")
    selected = prepare_worktree(vault, ticket_id=ticket_id, slug=slug, snapshot_a=snapshot_a,
                                inputs=sorted(canonical_paths(inputs) | {block}), attempt=attempt)
    root = selected.path
    admin = _admin(selected)

    def verify_owner():
        if verify_worktree(vault, ticket_id=ticket_id, slug=slug, attempt=attempt) != selected:
            raise ValueError("worktree ownership changed during refactoring setup")

    with os.fdopen(os.open(admin / "theseus-refactoring.lock",
                           os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        verify_owner()
        path, record = _load(selected, block, plan)
        head = _line(root, "rev-parse", "HEAD")
        _clean(root)
        if record is None:
            if head != selected.base:
                raise ValueError("refactoring cleanup requires the owned worktree's unmodified input base")
            _check_untracked(root, block)
            before = _revision(root, selected.base, block, plan)
            changes = {change.path: change.after for change in _cleanup(before, block)}
            cleanup = _commit(root, admin, selected.base, changes, "Prepare Theseus refactoring cleanup")
            record = dict(base=selected.base, block=block, plan=plan, cleanup=cleanup, execution_base=None)
            _validate(selected, record)
            _save(path, record)
        else:
            _validate(selected, record)
        cleanup = record["cleanup"]
        if head == selected.base:
            _check_untracked(root, block)
            _publish(root, selected.base, cleanup)
            head = cleanup
        planned = record["execution_base"]
        if planned is not None:
            if head == cleanup:
                _check_untracked(root, block)
                _publish(root, cleanup, planned)
            refactoring_execution_base(selected, block, plan)
            if _line(root, "rev-parse", "HEAD") == planned:
                result = run_planning(root, replace(decision, stage="execute"), ticket=ticket,
                                      plan_path=plan, files=files)
                if not decision.execute_after_plan:
                    message = f"*The refactoring plan [[{plan[:-3]}]] is ready for execution.*"
                    result = replace(result, stage="await_execution", response=message)
            else:
                message = f"*Continue the recorded execution of [[{plan[:-3]}]] in this worktree.*"
                result = Result("resume", message, plan)
            return Refactoring(selected, cleanup, planned, result)
        if head != cleanup:
            raise ValueError("worktree HEAD moved beyond the recorded refactoring cleanup")
        _check_untracked(root, block)

        def publish(before, updates):
            verify_owner()
            if capture(root) != before or _line(root, "rev-parse", "HEAD") != cleanup:
                raise ValueError("worktree changed while the refactoring planner was running")
            commit = _commit(root, admin, cleanup, updates, "Prepare Theseus refactoring plan")
            record["execution_base"] = commit
            _validate(selected, record)
            _save(path, record)
            _publish(root, cleanup, commit)

        result = run_planning(root, replace(decision, stage="plan"), ticket=ticket, plan_path=plan,
                              files=files, sections={block: [ARCHITECTURE, ACCEPTANCE]}, settings=settings,
                              context=context, runner=runner, _refactoring_publish=publish)
        return Refactoring(selected, cleanup, record["execution_base"], result)


def main():
    try:
        request = json.load(sys.stdin, object_pairs_hook=unique_fields)
        request["decision"] = Decision(**request["decision"])
        result = run_refactoring_planning(**request)
    except (OSError, TypeError, ValueError, KeyError, subprocess.CalledProcessError, RoleExecutionError) as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
        print(f"refactoring: {detail}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(result), ensure_ascii=False, default=os.fspath))
    return 0


if __name__ == "__main__":
    sys.exit(main())
