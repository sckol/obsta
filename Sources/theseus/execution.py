"""Run tasks sequentially in an owned worktree and reconcile their Git evidence."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__:
    from .codex import RoleExecutionError, run_role, unique_fields
    from .notes import read_section, replace_section
    from .planning import STATUS, TASKS, bug_fix_setup_record, cell, check_plan_context, line, parse_rows
    from .planning import validated_tasks, verify_specification, write_changes
    from .refactoring import refactoring_execution_base
    from .routing import note_path
    from .scope import Entry, authorize, capture
    from .worktree import OBJECT_ID, _clean, _clean_index, _git, _line, owned_checkout, verify_worktree
else:
    from codex import RoleExecutionError, run_role, unique_fields
    from notes import read_section, replace_section
    from planning import STATUS, TASKS, bug_fix_setup_record, cell, check_plan_context, line, parse_rows
    from planning import validated_tasks, verify_specification, write_changes
    from refactoring import refactoring_execution_base
    from routing import note_path
    from scope import Entry, authorize, capture
    from worktree import OBJECT_ID, _clean, _clean_index, _git, _line, owned_checkout, verify_worktree


CONTEXT = ("Working context",)
PENDING = "*Pending*"
RUNNING = re.compile(r"\*Running · ([a-f0-9]{32}) · ([a-f0-9]{64})\*")
DONE = re.compile(r"\*Done · (" + OBJECT_ID + r")\*")
EVIDENCE = "Theseus-evidence: "
TASK_SUBJECT = re.compile(r"Theseus task ([1-9][0-9]*) ([a-f0-9]{32})")


@dataclass(frozen=True)
class Candidate:
    number: int
    commit: str
    parent: str
    files: tuple
    red: dict
    green: tuple
    producer: str | None = None


@dataclass(frozen=True)
class Result:
    stage: str
    candidate: Candidate | None = None


def _read_plan(data):
    table = read_section(data, TASKS, root_level=2)
    rows = table.strip().decode().splitlines()
    progress, normalized = [], rows[:2]
    for row in rows[2:]:
        body, value = row.removesuffix(" |").rsplit(" | ", 1)
        if value != PENDING and not (RUNNING.fullmatch(value) or DONE.fullmatch(value)):
            raise ValueError("invalid execution progress")
        progress.append(value)
        normalized.append(f"{body} | {PENDING} |")
    clean = replace_section(data, TASKS, ("\n".join(normalized) + "\n\n").encode(), root_level=2)
    return progress, parse_rows(clean)


def _fingerprint(state, excluded):
    digest = hashlib.sha256()
    for path, entry in sorted(state.items()):
        if entry.kind != "directory" and path not in excluded:
            digest.update(json.dumps([path, entry.kind, entry.mode, hashlib.sha256(entry.data).hexdigest()])
                          .encode())
    return digest.hexdigest()


def _parent(root, commit):
    chain = _line(root, "rev-list", "--parents", "-1", commit).split()
    if len(chain) != 2:
        raise ValueError("coder evidence must belong to a single-parent commit")
    return chain[1]


def _dispatch_token(root, commit, number):
    message = _line(root, "show", "-s", "--format=%s", commit)
    match = re.fullmatch(rf"Dispatch Theseus task {number} · ([a-f0-9]{{32}})", message)
    if not match:
        raise ValueError(f"task {number}: commit has no matching dispatch")
    return match[1]


def _accepted(number, commit, task):
    return f"Accept Theseus task {number} · {commit} · {len(task.checks)} checks passed"


def _blob(root, object_id):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(["git", "--no-replace-objects", "-C", str(root), "cat-file", "blob", object_id],
                          check=True, capture_output=True, env=env).stdout


def _commit_states(root, parent, commit, live, *, notes=None):
    """Use Git's literal changed paths and modes, independent of the checkout."""
    raw = _git(root, "diff-tree", "--no-commit-id", "--no-renames", "--raw", "-r", "-z",
               parent, commit).stdout.split("\0")[:-1]
    if notes is not None and set(raw[1::2]) != notes:
        raise ValueError("progress commit changed files outside the assigned notes")
    before, after = dict(live), dict(live)
    for meta, path in zip(raw[::2], raw[1::2]):
        oldmode, newmode, oldid, newid, _status = meta.lstrip(":").split()
        for state, mode, object_id in ((before, oldmode, oldid), (after, newmode, newid)):
            if mode == "000000":
                state.pop(path, None)
            elif mode in {"100644", "100755"}:
                read_blob = notes is not None or path.endswith(".md")
                data = _blob(root, object_id) if read_blob else object_id.encode()
                state[path] = Entry("file", data, int(mode[-3:], 8))
            else:
                raise ValueError(f"{path}: coder commits may contain only regular files")
    return before, after


def _check_progress_commit(root, parent, commit, state, block, plan, work):
    before, after = _commit_states(root, parent, commit, state, notes={block, plan})
    if _read_plan(before[plan].data)[1] != _read_plan(after[plan].data)[1]:
        raise ValueError("progress commit changed task definitions")
    authorize(before, after, block, role="orchestrator", stage="execute", work=work,
              plan=plan, files=(), sections={block: [CONTEXT]})


def _tree_files(root, commit, files):
    tracked = _git(root, "--literal-pathspecs", "ls-tree", "--full-tree", "-r", "-z", commit,
                   "--", *files).stdout.split("\0")[:-1]
    entries = {}
    for item in tracked:
        meta, path = item.split("\t", 1)
        mode, _kind, digest = meta.split()
        entries[path] = (mode, digest)
    return {path: entries.get(path) for path in files}


def _working_file(entry, object_id):
    if entry is None:
        return None
    if entry.kind != "file":
        raise ValueError("expected a regular worktree file")
    algorithm = hashlib.sha1 if len(object_id) == 40 else hashlib.sha256
    contents = b"blob " + str(len(entry.data)).encode() + b"\0" + entry.data
    return ("100755" if entry.mode & 0o100 else "100644", algorithm(contents).hexdigest())


def _check_checkout(root, commit, live, files):
    for path, expected in _tree_files(root, commit, files).items():
        if _working_file(live.get(path), commit) != expected:
            raise ValueError(f"{path}: worktree bytes differ from the coder commit")


def _candidate(root, commit, number, task, live, block, work, intent=None):
    parent = _parent(root, commit)
    subject, _, body = _line(root, "show", "-s", "--format=%B", commit).partition("\n\n")
    match = TASK_SUBJECT.fullmatch(subject)
    if not match or int(match[1]) != number or (intent and (parent, match[2]) != intent):
        raise ValueError(f"task {number}: commit does not match its dispatch")
    before, after = _commit_states(root, parent, commit, live)
    # The selected note must stay byte-for-byte unchanged in coder commits.
    if before.get(block) != after.get(block):
        raise ValueError(f"{block}: not writable for coder")
    changes = authorize(before, after, block, role=task.coder, stage="execute", work=work, files=task.files)
    if not changes:
        raise ValueError(f"task {number}: empty coder commit")
    if not body.startswith(EVIDENCE):
        raise ValueError(f"task {number}: missing committed red/green evidence")
    evidence = json.loads(body.removeprefix(EVIDENCE), object_pairs_hook=unique_fields)
    if not isinstance(evidence, dict) or set(evidence) not in ({"red", "green"}, {"red", "green", "producer"}):
        raise ValueError("expected red and green evidence")
    producer = evidence.get("producer")
    if "producer" in evidence:
        recorded = _read_coder_journal(_coder_journal(root, match[2]))
        files = _tree_files(root, commit, [change.path for change in changes])
        if (producer != "execution.py" or recorded.get("parent") != parent
                or recorded.get("red") != evidence["red"]
                or _evidence_digest(evidence, files) not in recorded.get("prepared", [])):
            raise ValueError("commit evidence does not match the execution helper journal")
    red, green = evidence["red"], evidence["green"]
    if not isinstance(green, list) or len(green) != len(task.checks):
        raise ValueError("green evidence must cover every required check")
    for item, success in [(red, False), *((item, True) for item in green)]:
        if (not isinstance(item, dict) or set(item) != {"command", "cwd", "exit", "observed"}
                or type(item["exit"]) is not int or (item["exit"] == 0) != success
                or not isinstance(item["observed"], str) or not item["observed"].strip()):
            raise ValueError("invalid red/green result")
        line(item["command"])
        line(item["cwd"])
    planned = [(check.command, check.cwd) for check in task.checks]
    if (red["command"], red["cwd"]) not in planned:
        raise ValueError("red evidence differs from the planned checks")
    if [(item["command"], item["cwd"]) for item in green] != planned:
        raise ValueError("green evidence differs from the planned checks")
    return Candidate(number, commit, parent, tuple(change.path for change in changes), red, tuple(green), producer)


def _observe(root, check, timeout):
    directory = root / check.cwd
    if directory.resolve(strict=True) != directory or not directory.is_dir():
        raise ValueError(f"unsafe verification directory: {check.cwd}")
    result = subprocess.run(check.command, shell=True, cwd=directory, capture_output=True,
                            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), timeout=timeout)
    output = (result.stdout + result.stderr)[-2000:].decode(errors="replace").strip()
    return {"command": check.command, "cwd": check.cwd, "exit": result.returncode,
            "observed": output or f"Exit {result.returncode}, no output"}


def _green(root, check, timeout):
    result = _observe(root, check, timeout)
    if result["exit"]:
        raise ValueError(f"check failed: {check.command} (exit {result['exit']})\n{result['observed']}")
    return result


def _checks(root, task, timeout):
    for check in task.checks:
        _green(root, check, timeout)


def _coder_journal(root, token):
    admin = Path(_line(root, "rev-parse", "--absolute-git-dir"))
    return admin / f"theseus-coder-{token}.json"


def _read_coder_journal(journal):
    try:
        with os.fdopen(os.open(journal, os.O_RDONLY | os.O_NOFOLLOW), encoding="utf-8") as stream:
            return json.load(stream, object_pairs_hook=unique_fields)
    except FileNotFoundError:
        return {}


def _evidence_digest(evidence, files):
    return hashlib.sha256(json.dumps([evidence, files], sort_keys=True).encode()).hexdigest()


def _atomic_json(journal, data):
    with tempfile.NamedTemporaryFile(dir=journal.parent, mode="w", delete=False) as temporary:
        path = Path(temporary.name)
        try:
            json.dump(data, temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
            path.replace(journal)
        finally:
            path.unlink(missing_ok=True)


def _coder_task(operation, block, plan, work, number, token, check_number, timeout):
    note_path(block, "Sources")
    note_path(plan, "Plans")
    if not re.fullmatch(r"[a-f0-9]{32}", token):
        raise ValueError("invalid dispatch token")
    _, _, selected = owned_checkout(Path.cwd())
    root = selected.path
    journal = _coder_journal(root, token)
    with os.fdopen(os.open(journal.with_suffix(".lock"),
                          os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        head = _line(root, "rev-parse", "HEAD")
        live = capture(root)
        tasks, progress = _execution_plan(live, block, plan, work)
        if not 1 <= number <= len(tasks):
            raise ValueError("unknown task number")
        match = RUNNING.fullmatch(progress[number - 1])
        if match is None or match[1] != token or _dispatch_token(root, head, number) != token:
            raise ValueError(f"task {number}: worktree HEAD is not the selected dispatch")
        task = tasks[number - 1]
        _check_checkout(root, head, live, [block, plan])
        _check_progress_commit(root, _parent(root, head), head, live, block, plan, work)
        _clean_index(root)
        authorize(live, live, block, role=task.coder, stage="execute", work=work, files=task.files)
        if _fingerprint(live, set(task.files) | {block, plan}) != match[2]:
            raise ValueError(f"task {number}: unrelated worktree files changed")
        recorded = _read_coder_journal(journal)
        if recorded and recorded["parent"] != head:
            raise ValueError("recorded red evidence belongs to a different dispatch")

        if operation == "--record-red":
            if not 1 <= check_number <= len(task.checks):
                raise ValueError("unknown check number")
            check = task.checks[check_number - 1]
            if recorded:
                red = recorded["red"]
                if (red["command"], red["cwd"]) != (check.command, check.cwd):
                    raise ValueError("a different red check is already recorded for this dispatch")
                return {"red": red}
            red = _observe(root, check, timeout)
            if red["exit"] <= 0 or red["exit"] in {126, 127}:
                raise ValueError(f"red check must reach a failing assertion (exit {red['exit']})")
            _unchanged_coder_checkout(root, head, live)
            _atomic_json(journal, {"parent": head, "red": red, "prepared": []})
            return {"red": red}

        if not recorded:
            raise ValueError("record a failing red check before creating the task commit")
        untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")[:-1]
        modified = _git(root, "diff", "--name-only", "--no-renames", "-z", head, "--").stdout.split("\0")[:-1]
        changed = sorted(set(untracked) | set(modified))
        if not changed or not set(changed) <= set(task.files):
            raise ValueError("task commit requires changes only to approved implementation and test paths")
        green = [_green(root, check, timeout) for check in task.checks]
        _unchanged_coder_checkout(root, head, live)
        evidence = {"red": recorded["red"], "green": green, "producer": "execution.py"}
        digest = _evidence_digest(evidence, {path: _working_file(live.get(path), head) for path in changed})
        if digest not in recorded["prepared"]:
            recorded["prepared"].append(digest)
        _atomic_json(journal, recorded)
        if untracked:
            _git(root, "--literal-pathspecs", "add", "--intent-to-add", "--", *untracked)
        _git(root, "--literal-pathspecs", "-c", "commit.gpgsign=false", "commit", "--only",
             "-m", f"Theseus task {number} {token}", "-m", EVIDENCE + json.dumps(evidence), "--", *changed)
        commit = _line(root, "rev-parse", "HEAD")
        _clean(root)
        _candidate(root, commit, number, task, capture(root), block, work, (head, token))
        return {"commit": commit, **evidence}


def _unchanged_coder_checkout(root, head, live):
    _clean_index(root)
    if head != _line(root, "rev-parse", "HEAD") or capture(root) != live:
        raise ValueError("planned check changed the task worktree")


def _progress(root, state, journal, *, block, plan, work, number, previous, next_value, message):
    old_plan, old_note = state[plan], state[block]
    table = read_section(old_plan.data, TASKS, root_level=2)
    rows = table.splitlines(keepends=True)
    needle = re.compile(rb"^\| " + str(number).encode() + rb" \|")
    indexes = [index for index, row in enumerate(rows) if needle.match(row)]
    if len(indexes) != 1:
        raise ValueError(f"ambiguous task {number} progress row")
    index = indexes[0]
    suffix = f" | {previous} |".encode()
    row = rows[index].rstrip(b"\r\n")
    if not row.endswith(suffix):
        raise ValueError("task progress changed during execution")
    rows[index] = (row[:-len(suffix)] + f" | {next_value} |".encode()
                   + rows[index][len(row):])
    data = replace_section(old_plan.data, TASKS, b"".join(rows), root_level=2)
    if read_section(data, STATUS, root_level=2).strip() == b"*Ready*":
        status = read_section(data, STATUS, root_level=2)
        data = replace_section(data, STATUS, status.replace(b"*Ready*", b"*Running*", 1), root_level=2)
    body = read_section(old_note.data, CONTEXT, root_level=2)
    newline = b"\r\n" if b"\r\n" in old_note.data else b"\n"
    note = replace_section(old_note.data, CONTEXT,
                           body + newline + f"*{cell(message)}*".encode() + newline * 2, root_level=2)
    updates = {plan: Entry("file", data, old_plan.mode), block: Entry("file", note, old_note.mode)}
    _clean(root)
    head = _line(root, "rev-parse", "HEAD")
    transaction = {
        "head": head, "message": message, "fingerprint": _fingerprint(state, {plan, block}),
        "notes": {path: [base64.b64encode(content).decode()
                         for content in (state[path].data, entry.data)] for path, entry in updates.items()},
    }
    _atomic_json(journal, transaction)
    _replay_progress(root, journal, block, plan, work)


def _replay_progress(root, journal, block, plan, work):
    if journal.is_symlink():
        raise ValueError("invalid execution progress transaction")
    if not journal.exists():
        return
    if not journal.is_file():
        raise ValueError("invalid execution progress transaction")
    transaction = json.loads(journal.read_text(), object_pairs_hook=unique_fields)
    state = capture(root)
    paths = {block, plan}
    if set(transaction["notes"]) != paths or _fingerprint(state, paths) != transaction["fingerprint"]:
        raise ValueError("unrelated worktree files changed during progress recording")
    head = _line(root, "rev-parse", "HEAD")
    committed = head != transaction["head"]
    if committed and (
        _parent(root, head) != transaction["head"]
        or _line(root, "show", "-s", "--format=%s", head) != transaction["message"]
        or set(_git(root, "diff-tree", "--no-commit-id", "-r", "-z", "--name-only", head)
               .stdout.split("\0")[:-1]) != paths
    ):
        raise ValueError("worktree HEAD changed during progress recording")
    _clean_index(root)
    before, updates = dict(state), {}
    for path, encoded in transaction["notes"].items():
        old, new = (base64.b64decode(data, validate=True) for data in encoded)
        actual = state[path]
        if actual.kind != "file" or actual.data not in ({new} if committed else {old, new}):
            raise ValueError(f"{path}: changed during progress recording")
        before[path], updates[path] = (Entry("file", data, actual.mode) for data in (old, new))
    authorize(before, {**before, **updates}, block, role="orchestrator", stage="execute", work=work,
              plan=plan, files=(), sections={block: [CONTEXT]})
    if not committed:
        write_changes(root, state, {path: entry for path, entry in updates.items() if state[path] != entry})
        _git(root, "--literal-pathspecs", "-c", "commit.gpgsign=false", "commit", "--only",
             "-m", transaction["message"], "--", plan, block)
    _clean(root)
    head = _line(root, "rev-parse", "HEAD")
    _check_progress_commit(root, transaction["head"], head, capture(root), block, plan, work)
    journal.unlink()


def _dispatch(runner, settings, root, role, task, number, token, block, plan, work, context, timeout):
    def command(operation):
        return shlex.join(["python3", str(Path(__file__).resolve()), operation, block, plan,
                           work, str(number), token, "--timeout", str(timeout)])

    brief = {
        "number": number, "block": block, "plan": plan, "context": context,
        "depends_on": task.depends_on, "checks": [asdict(check) for check in task.checks],
        "commit_subject": f"Theseus task {number} {token}",
        "red_command": command("--record-red"),
        "commit_command": command("--commit-task"),
    }
    answer = runner(
        role, settings, cwd=root, allowed_paths=list(task.files), allowed_sections={},
        checks=[check.command for check in task.checks], context=json.dumps(brief, ensure_ascii=False),
        task=(f"{task.description}\nPrepare the failing test if needed, then run red_command verbatim "
              "before changing production behavior. If a different planned check is the red test, append "
              "--check followed by its 1-based index. After the scoped fix, run commit_command verbatim. "
              "The helper runs every green check and creates the single task commit with its evidence. "
              "Leave the index unstaged and let the helper create the commit. Report any blocker."),
    )
    if answer.outcome != "success":
        raise RoleExecutionError(answer.response)


def run_execution(vault, *, ticket_id, slug, block, plan, work, settings, attempt=1, context="",
                  coder_overrides=None, check_timeout=300, runner=run_role, review=None):
    """Run the first unfinished task; accept code only when its verified candidate is approved.

    Without a review callback, return await_review and the frozen candidate. A caller may
    resume later with a callback accepting exactly that candidate; only True accepts it.
    """
    note_path(block, "Sources")
    note_path(plan, "Plans")
    identity = dict(ticket_id=ticket_id, slug=slug, attempt=attempt)
    selected = verify_worktree(vault, **identity)
    root = selected.path
    admin = Path(_line(root, "rev-parse", "--absolute-git-dir"))
    with os.fdopen(os.open(admin / "theseus-execution.lock",
                          os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        base = _execution_base(selected, capture(root), block, plan, work)
        journal = admin / "theseus-progress.json"
        _replay_progress(root, journal, block, plan, work)
        return _run(vault, identity, selected, base, block, plan, work, settings, context, journal,
                    coder_overrides or {}, check_timeout, runner, review)


def _execution_plan(live, block, plan, work):
    plan_data = live[plan].data
    check_plan_context(plan_data, block, work)
    if read_section(plan_data, STATUS, root_level=2).strip() not in {b"*Ready*", b"*Running*"}:
        raise ValueError("the plan must be Ready or Running")
    progress, entries = _read_plan(plan_data)
    allow = {path for item in entries for path in item["files"]}
    tasks = validated_tasks(entries, allow, live)
    verify_specification(live[block].data, tasks, require_task_check=work == "first")
    return tasks, progress


def _execution_base(selected, live, block, plan, work):
    if work == "refactoring":
        return refactoring_execution_base(selected, block, plan)
    root, base = selected.path, selected.base
    if work != "bug_fix" or _git(root, "cat-file", "-e", f"{base}:{plan}", check=False).returncode == 0:
        return base
    record = bug_fix_setup_record(selected, block, plan)
    if record is None:
        raise ValueError("missing prepared Bug fix plan commit")
    setup = record["commit"]
    if _git(root, "merge-base", "--is-ancestor", setup, "HEAD", check=False).returncode:
        raise ValueError("Bug fix plan setup is outside the current worktree history")
    before, after = _commit_states(root, base, setup, live)
    changes = authorize(before, after, block, role="planner", stage="plan", work=work, plan=plan,
                        sections=record["sections"])
    if not any(change.path == plan for change in changes):
        raise ValueError("Bug fix setup commit must add the current plan")
    check_plan_context(after[plan].data, block, work)
    if read_section(after[plan].data, STATUS, root_level=2).strip() != b"*Ready*":
        raise ValueError("Bug fix plan was not committed as Ready")
    return setup


def _accepted_history(root, base, live, block, plan, work, tasks, progress, first, evidence=None):
    expected_head = base
    history = _line(root, "rev-list", "--first-parent", "--reverse", expected_head + "..HEAD").splitlines()
    for number, task in enumerate(tasks[:first], 1):
        commit = DONE.fullmatch(progress[number - 1])[1]
        segment = history[(number - 1) * 3:number * 3]
        if len(segment) != 3 or segment[1] != commit:
            raise ValueError(f"task {number}: accepted commit is missing from the worktree history")
        dispatch, _, acceptance = segment
        if _parent(root, dispatch) != expected_head or _parent(root, acceptance) != commit:
            raise ValueError(f"task {number}: accepted history is not linear")
        _check_progress_commit(root, expected_head, dispatch, live, block, plan, work)
        candidate = _candidate(root, commit, number, task, live, block, work,
                               (dispatch, _dispatch_token(root, dispatch, number)))
        if evidence is not None:
            evidence.append(candidate)
        _check_progress_commit(root, commit, acceptance, live, block, plan, work)
        if _line(root, "show", "-s", "--format=%s", acceptance) != _accepted(number, commit, task):
            raise ValueError(f"task {number}: acceptance commit is missing")
        expected_head = acceptance
    return expected_head, history[first * 3:]


def _completed_repairs(root, live, block, plan, work, tasks, head, remaining, pending=None):
    admin = Path(_line(root, "rev-parse", "--absolute-git-dir"))
    journal = admin / "theseus-verification.json"
    if journal.is_symlink() or journal.exists() and not journal.is_file():
        raise ValueError("invalid verification journal path")
    state = json.loads(journal.read_text(encoding="utf-8"), object_pairs_hook=unique_fields) if journal.exists() else {}
    repairs = state.get("repairs", [])
    if repairs and state.get("scope") != [block, plan, work]:
        raise ValueError("recorded final review repairs belong to another plan")
    if pending is not None:
        repairs = [*repairs, pending]
    if remaining != [repair["head"] for repair in repairs]:
        raise ValueError("unrecorded worktree HEAD")
    allowed = sorted({file for task in tasks for file in task.files})
    for repair in repairs:
        commit = repair["head"]
        if _parent(root, commit) != head or repair["parent"] != head:
            raise ValueError("final review repair does not follow its blocked revision")
        before, after = _commit_states(root, head, commit, live)
        if before.get(block) != after.get(block):
            raise ValueError(f"{block}: not writable for coder")
        changes = authorize(before, after, block, role="coder", stage="execute", work=work, files=allowed)
        if not changes:
            raise ValueError("empty final review repair commit")
        head = commit


def verify_completed_execution(selected, live, block, plan, work, *, pending_repair=None, evidence=None):
    """Verify plan completion and all its acceptance commits without dispatching or writing."""
    tasks, progress = _execution_plan(live, block, plan, work)
    if not all(DONE.fullmatch(value) for value in progress):
        raise ValueError("the execution plan still contains unfinished tasks")
    base = _execution_base(selected, live, block, plan, work)
    head, remaining = _accepted_history(selected.path, base, live, block, plan, work, tasks, progress,
                                        len(tasks), evidence=evidence)
    _completed_repairs(selected.path, live, block, plan, work, tasks, head, remaining, pending_repair)
    return tasks


def _run(vault, identity, selected, base, block, plan, work, settings, context, journal, overrides,
         timeout, runner, review):
    root = selected.path
    _clean(root)
    live = capture(root)
    tasks, progress = _execution_plan(live, block, plan, work)
    if any(type(n) is not int or not 1 <= n <= len(tasks) or role != "coder_heavy"
           for n, role in overrides.items()):
        raise ValueError("coder overrides must select coder_heavy for an existing task")

    first = next((i for i, value in enumerate(progress) if not DONE.fullmatch(value)), len(tasks))
    if any(value != PENDING for value in progress[first + 1:]):
        raise ValueError("execution progress is not sequential")
    expected_head, remaining = _accepted_history(root, base, live, block, plan, work, tasks, progress, first)
    if first == len(tasks):
        _completed_repairs(root, live, block, plan, work, tasks, expected_head, remaining)
        return Result("complete")
    if len(remaining) > (2 if RUNNING.fullmatch(progress[first]) else 0):
        raise ValueError("unrecorded worktree HEAD")

    for number, task in enumerate(tasks[first:], first + 1):
        previous = progress[number - 1]
        role = overrides.get(number, task.coder)
        exclusions = set(task.files) | {plan, block}
        authorize(live, live, block, role=role, stage="execute", work=work, files=task.files)
        if previous == PENDING:
            token, fingerprint = uuid.uuid4().hex, _fingerprint(live, exclusions)
            previous = f"*Running · {token} · {fingerprint}*"
            _progress(root, live, journal, block=block, plan=plan, work=work, number=number,
                      previous=PENDING, next_value=previous,
                      message=f"Dispatch Theseus task {number} · {token}")
        else:
            token, fingerprint = RUNNING.fullmatch(previous).groups()
        start = _line(root, "log", "-1", "--format=%H", "--", plan)
        if _parent(root, start) != expected_head or _dispatch_token(root, start, number) != token:
            raise ValueError(f"task {number}: missing dispatch commit")
        _check_progress_commit(root, expected_head, start, live, block, plan, work)
        commit = _line(root, "rev-parse", "HEAD")
        if commit == start:
            _dispatch(runner, settings, root, role, task, number, token, block, plan, work, context, timeout)
            commit = _line(root, "rev-parse", "HEAD")
        if verify_worktree(vault, **identity) != selected:
            raise ValueError("the execution worktree changed ownership")
        _clean(root)
        live = capture(root)
        if commit == start:
            raise ValueError(f"task {number}: coder supplied no commit")
        candidate = _candidate(root, commit, number, task, live, block, work, (start, token))
        _check_checkout(root, commit, live, task.files)
        if _fingerprint(live, exclusions) != fingerprint:
            raise ValueError(f"task {number}: unrelated worktree files changed")
        _checks(root, task, timeout)
        _clean(root)
        if capture(root) != live or _line(root, "rev-parse", "HEAD") != commit:
            raise ValueError("verification changed the candidate worktree")
        if review is None:
            return Result("await_review", candidate)
        approved = review(candidate)
        _clean(root)
        if capture(root) != live or _line(root, "rev-parse", "HEAD") != commit:
            raise ValueError("review changed the candidate worktree")
        if verify_worktree(vault, **identity) != selected:
            raise ValueError("the execution worktree changed ownership during review")
        if approved is not True:
            return Result("await_review", candidate)
        _progress(root, live, journal, block=block, plan=plan, work=work, number=number,
                  previous=previous, next_value=f"*Done · {commit}*",
                  message=_accepted(number, commit, task))
        expected_head = _line(root, "rev-parse", "HEAD")
        live = capture(root)
    return Result("complete")


def main():
    try:
        if len(sys.argv) > 1:
            parser = argparse.ArgumentParser(description="Record actual task checks or commit an approved task.")
            parser.add_argument("block")
            parser.add_argument("plan")
            parser.add_argument("work")
            parser.add_argument("number", type=int)
            parser.add_argument("token")
            operation = parser.add_mutually_exclusive_group(required=True)
            operation.add_argument("--record-red", action="store_const", const="--record-red", dest="operation")
            operation.add_argument("--commit-task", action="store_const", const="--commit-task", dest="operation")
            parser.add_argument("--check", type=int, default=1, dest="check_number")
            parser.add_argument("--timeout", type=float, default=300)
            result = _coder_task(**vars(parser.parse_args()))
            print(json.dumps(result, ensure_ascii=False))
            return 0
        request = json.load(sys.stdin, object_pairs_hook=unique_fields)
        if "coder_overrides" in request:
            roles = request["coder_overrides"]
            if not isinstance(roles, dict) or any(not re.fullmatch(r"[1-9][0-9]*", key) for key in roles):
                raise ValueError("coder_overrides keys must be positive task numbers")
            request["coder_overrides"] = {int(key): role for key, role in roles.items()}
        result = run_execution(**request)
    except (KeyError, OSError, RoleExecutionError, TypeError, ValueError, subprocess.SubprocessError) as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
        print(json.dumps({"error": detail}, ensure_ascii=False))
        return 1
    print(json.dumps(asdict(result), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
