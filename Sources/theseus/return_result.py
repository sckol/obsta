"""Return a verified Theseus worktree delta to the main vault."""

from __future__ import annotations

import base64
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import unicodedata
import uuid
from contextlib import contextmanager, suppress
from dataclasses import replace

if __package__:
    from .codex import unique_fields
    from .evaluation import Evaluation
    from .notes import changed_sections, editable_section, merge_return_section
    from .refactoring import _changes, _revision, refactoring_execution_base
    from .routing import WORK_TYPES, canonical_path, note_path
    from .scope import Change, Entry, _belongs, _check_plan, _PLAN_PROGRESS, authorize, block_scope, capture
    from .verification import _json, _save, verified_receipt
    from .worktree import OBJECT_ID, _clean, _git, _line, _location, _registered, _repository, verify_worktree
else:
    from codex import unique_fields
    from evaluation import Evaluation
    from notes import changed_sections, editable_section, merge_return_section
    from refactoring import _changes, _revision, refactoring_execution_base
    from routing import WORK_TYPES, canonical_path, note_path
    from scope import Change, Entry, _belongs, _check_plan, _PLAN_PROGRESS, authorize, block_scope, capture
    from verification import _json, _save, verified_receipt
    from worktree import OBJECT_ID, _clean, _git, _line, _location, _registered, _repository, verify_worktree


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _value(entry):
    if entry is None:
        return None
    return {"kind": entry.kind, "mode": entry.mode, "data": base64.b64encode(entry.data).decode("ascii")}


def _entry(value):
    if value is None:
        return None
    return Entry(value["kind"], base64.b64decode(value["data"], validate=True), value["mode"])


def _persist(journal, state):
    _save(journal, state)
    descriptor = os.open(journal.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _key(path):
    return unicodedata.normalize("NFC", path).casefold()


def _historical_collision(plan, before):
    name = _key(plan)
    for path in before:
        other = _key(path)
        entry = before[path]
        if other == name or other.startswith(name + "/") or (
            name.startswith(other + "/") and entry.kind != "directory"
        ):
            raise ValueError(f"{plan}: historical plan path collision with {path}")


def _changes_to_return(selected, *, block, plan, work, head, report):
    before = _revision(selected.path, selected.base, block, plan)
    candidate = _revision(selected.path, head, block, plan)
    for path, entry in report.items():
        candidate[path] = replace(candidate[path], data=entry.data)
    if work == "refactoring":
        refactoring_execution_base(selected, block, plan)
        _historical_collision(plan, before)
        for path, entry in _changes(before, candidate).items():
            if path.startswith("Plans/") and path != plan:
                if entry is not None:
                    raise ValueError(f"{path}: only the current refactoring plan may be returned")
                candidate[path] = before[path]
    new_plan = None
    if work in {"bug_fix", "refactoring"} and plan not in before:
        _historical_collision(plan, before)
        new_plan = Change(plan, None, candidate.pop(plan, None))
        _check_plan(new_plan, "plan")
    changes = authorize(before, candidate, block, role="obstagent", stage="return", work=work, plan=plan)
    if new_plan is not None:
        changes += (new_plan,)
    result = []
    for change in changes:
        if not change.sections and change.path != plan and change.after is not None:
            data = _git(selected.path, "cat-file", "blob", head + ":" + change.path, data=b"").stdout
            change = replace(change, after=replace(change.after, data=data))
        if change.path == plan and change.before is not None:
            sections = changed_sections(change.before.data, change.after.data, _PLAN_PROGRESS,
                                        root_level=2, optional=True)
            change = replace(change, sections=sections)
        result.append(change)
    return tuple(sorted(result, key=lambda change: (not change.sections and change.path != plan, change.path)))


def _writable(live, changes, block, plan, work):
    scope = block_scope(live, block)
    descendants = set(scope.descendants)
    aliases = {}
    for name in live:
        aliases.setdefault(_key(name), []).append(name)
    for change in changes:
        path = change.path
        for prefix in (*_parents(path), path):
            for name in aliases.get(_key(prefix), ()):
                if name != prefix:
                    raise ValueError(f"{path}: live path collision with {name}")
            entry = live.get(prefix)
            kind = "file" if prefix == path else "directory"
            if entry is not None and entry.kind != kind:
                raise ValueError(f"{path}: expected {kind} at {prefix}, found {entry.kind}")
        if path == plan:
            continue
        if change.sections:
            if path != block and (work == "refactoring" or path not in descendants):
                raise ValueError(f"{path}: live block no longer permits this note update")
        elif not _belongs(path, scope, descendants, work):
            raise ValueError(f"{path}: live block no longer permits this implementation update")


def _parents(path):
    parts = path.split("/")
    return ["/".join(parts[:end]) for end in range(1, len(parts))]


def _check_name(directory, name):
    if any(other != name and _key(other) == _key(name) for other in os.listdir(directory)):
        raise ValueError(f"live path collision at {name}")


@contextmanager
def _target_parent(vault, path, *, create=False):
    parts = canonical_path(path).split("/")
    if len(parts) < 2 or parts[0] not in {"Sources", "Plans"}:
        raise ValueError(f"{path}: invalid return destination")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(vault, flags)
    try:
        for part in parts[:-1]:
            _check_name(directory, part)
            try:
                child = os.open(part, flags, dir_fd=directory)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, dir_fd=directory)
                child = os.open(part, flags, dir_fd=directory)
            try:
                if create:
                    # Sync existing parents too, so a retry covers an interrupted mkdir.
                    os.fsync(directory)
            except BaseException:
                os.close(child)
                raise
            os.close(directory)
            directory = child
        yield directory, parts[-1]
    finally:
        os.close(directory)


def _file_stamp(info):
    return (info.st_dev, info.st_ino, info.st_nlink, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _live_file(directory, name):
    _check_name(directory, name)
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as file:
        initial = os.fstat(file.fileno())
        if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
            raise ValueError(f"unsafe live file at {name}")
        data = file.read()
        final = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if _file_stamp(initial) != _file_stamp(final):
            raise ValueError(f"live file changed while checking {name}")
        return Entry("file", data, stat.S_IMODE(final.st_mode))


def _prepare_replacement(file, replacement):
    file.write(replacement.data)
    file.flush()
    os.fchmod(file.fileno(), replacement.mode)
    os.fsync(file.fileno())


def _open_staging_directory(vault):
    vault, common = _main_repository(vault)
    if common.is_relative_to(vault) and not common.is_relative_to(vault / ".git"):
        raise ValueError("Git administrative directory is inside the vault snapshot")
    return os.open(common, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


@contextmanager
def _staged_replacement(vault, replacement):
    if replacement is None:
        yield None, None
        return
    # An interrupted process can leave scratch data here outside the vault snapshots.
    directory = _open_staging_directory(vault)
    temporary = f".theseus-return-{uuid.uuid4().hex}.tmp"
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "wb") as file:
                _prepare_replacement(file, replacement)
            yield directory, temporary
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
    finally:
        os.close(directory)


def _exclusive_rename():
    # Use the platform's atomic no-clobber rename without an intermediate hard link.
    systems = {"darwin": ("renameatx_np", 0x00000004), "linux": ("renameat2", 1)}
    if sys.platform not in systems:
        raise ValueError("this platform does not support exclusive return publication")
    symbol, flag = systems[sys.platform]
    rename = getattr(ctypes.CDLL(None, use_errno=True), symbol, None)
    if rename is None:
        raise ValueError("exclusive return publication is unavailable")
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    return rename, flag


def _installed_write(vault, path, expected, replacement):
    """Prepare, then check and publish. Editors can still write after the final check."""
    create = expected is None and replacement is not None
    try:
        if create:
            rename, flag = _exclusive_rename()
        with (
            _staged_replacement(vault, replacement) as (staging_directory, temporary),
            _target_parent(vault, path, create=create) as (directory, name),
        ):
            if create:
                source, target = os.fsencode(temporary), os.fsencode(name)
            if _live_file(directory, name) != expected:
                return False
            if create:
                if rename(staging_directory, source, directory, target, flag) != 0:
                    error = ctypes.get_errno()
                    if error == errno.EEXIST:
                        return False
                    raise OSError(error, os.strerror(error))
            elif replacement is None:
                os.unlink(name, dir_fd=directory)
            else:
                os.replace(temporary, name, src_dir_fd=staging_directory, dst_dir_fd=directory)
            os.fsync(directory)
            if staging_directory is not None:
                os.fsync(staging_directory)
    except (OSError, ValueError) as error:
        raise ValueError(f"{path}: {error}") from error
    return True


def _sync_recovered_write(vault, path, desired):
    # Publication can succeed just before the directory sync fails or the process stops.
    with _target_parent(vault, path) as (directory, name):
        if _live_file(directory, name) != desired:
            raise ValueError(f"{path}: conditional write conflict while recovering")
        os.fsync(directory)
    if desired is not None:
        directory = _open_staging_directory(vault)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _replacement(change, live, work, plan):
    if change.sections:
        if live is None:
            raise ValueError(f"{change.path}: missing live note")
        data = live.data
        for section in change.sections:
            try:
                data = merge_return_section(
                    change.before.data, change.after.data, data, section,
                    partial=work == "bug_fix" and section == ("Specification",),
                    preserve_fences=(work, section) != ("refactoring", ("Specification",))
                    and (work, section) != ("refactoring", ("Working context",)),
                )
            except ValueError as error:
                raise ValueError(f"{change.path}: {error}") from error
        return replace(live, data=data)
    if change.path == plan and change.before is None and live is not None:
        raise ValueError(f"{plan}: live historical plan path already exists")
    if change.after is None:
        return None
    if live is None:
        return change.after
    mode = (live.mode & ~0o111) | (change.after.mode & 0o111)
    return replace(change.after, mode=mode)


def _operations(live, changes, context):
    _writable(live, changes, context["block"], context["plan"], context["work"])
    operations = []
    for change in changes:
        expected = live.get(change.path)
        replacement = _replacement(change, expected, context["work"], context["plan"])
        operations.append({
            "path": change.path, "expected": _value(expected), "replacement": _value(replacement),
            "applied": expected == replacement,
        })
    return operations


def _recheck_pending(change, operation, actual, context):
    expected = _entry(operation["expected"])
    if actual == expected:
        return
    path = change.path
    if not change.sections or actual is None or actual.kind != "file" or actual.mode != expected.mode:
        raise ValueError(f"{path}: live file changed after return preparation")
    for section in change.sections:
        if editable_section(actual.data, section) != editable_section(expected.data, section):
            raise ValueError(f"{path}: {section[-1]} changed after return preparation")
    operation["expected"] = _value(actual)
    operation["replacement"] = _value(_replacement(change, actual, context["work"], context["plan"]))


def _main_index(vault):
    path = vault / _line(vault, "rev-parse", "--git-path", "index")
    if path.is_symlink():
        raise ValueError("main vault index is a symlink")
    try:
        return _digest(path.read_bytes())
    except FileNotFoundError:
        return None


def _assert_main(vault, state):
    if _line(vault, "rev-parse", "HEAD") != state["main_head"]:
        raise ValueError("main vault HEAD changed during return")
    if _main_index(vault) != state["main_index"]:
        raise ValueError("main vault index changed during return")


def _assert_clean_candidate(vault, identity, selected, head):
    if verify_worktree(vault, **identity) != selected or _line(selected.path, "rev-parse", "HEAD") != head:
        raise ValueError("owned worktree revision changed during return")
    _clean(selected.path)
    untracked = _git(selected.path, "ls-files", "--others", "-z").stdout.split("\0")[:-1]
    if untracked:
        raise ValueError(f"owned worktree contains untracked data: {untracked[0]}")


def _matched_branch(vault, state):
    branch = state["branch"]
    head = state["context"]["accepted_head"]
    if _line(vault, "rev-parse", "--verify", f"refs/heads/{branch}") != head:
        raise ValueError("the saved candidate branch moved")


def _publish(vault, journal, state, changes, check_candidate, writer):
    context, operations = state["context"], state["operations"]
    by_path = {change.path: change for change in changes}
    if [operation["path"] for operation in operations] != list(by_path):
        raise ValueError("return journal paths differ from the verified return delta")
    live = capture(vault)
    for operation in operations:
        path = operation["path"]
        expected, replacement = _entry(operation["expected"]), _entry(operation["replacement"])
        if _replacement(by_path[path], expected, context["work"], context["plan"]) != replacement:
            raise ValueError(f"{path}: journal value does not match the authorized return change")
        if operation["applied"] and live.get(path) != replacement:
            raise ValueError(f"{path}: previously applied value changed")
    pending = next((operation["path"] for operation in operations if not operation["applied"] and not (
        state["active"] == operation["path"] and live.get(operation["path"]) == _entry(operation["replacement"])
    )), None)
    if pending is not None and writer is None:
        raise ValueError(f"{pending}: conditional writer unavailable; "
                         "this operation requires compare-and-write")
    state["stage"] = "applying"
    _persist(journal, state)
    for operation in operations:
        if operation["applied"]:
            continue
        path = operation["path"]
        _assert_main(vault, state)
        check_candidate()
        live = capture(vault)
        _writable(live, changes, context["block"], context["plan"], context["work"])
        actual, desired = live.get(path), _entry(operation["replacement"])
        if state["active"] != path or actual != desired:
            _recheck_pending(by_path[path], operation, actual, context)
            desired = _entry(operation["replacement"])
            state["active"] = path
            _persist(journal, state)
            if actual != desired:
                if writer is None or writer(vault, path, actual, desired) is not True:
                    raise ValueError(f"{path}: conditional write conflict")
            if capture(vault).get(path) != desired:
                raise ValueError(f"{path}: conditional writer did not leave the prepared value")
        elif writer is _installed_write:
            _sync_recovered_write(vault, path, desired)
        operation["applied"], state["active"] = True, None
        _persist(journal, state)
    final = capture(vault)
    for operation in operations:
        if final.get(operation["path"]) != _entry(operation["replacement"]):
            raise ValueError(f"{operation['path']}: previously applied value changed before completion")


def _transfer_started(vault, state):
    """Distinguish completed or recovered writes from preparation and no-ops."""
    for operation in state["operations"]:
        expected, replacement = operation["expected"], operation["replacement"]
        if expected == replacement:
            continue
        if operation["applied"]:
            return True
        path = operation["path"]
        if state["active"] == path and capture(vault).get(path) == _entry(replacement):
            return True
    return False


def _finish(vault, path, journal, state):
    _matched_branch(vault, state)
    if os.path.lexists(path) or path in _registered(vault):
        raise ValueError(f"{path}: worktree was not removed")
    if state["stage"] != "finished":
        state["stage"] = "finished"
        _persist(journal, state)
    status = "discarded" if state["context"]["action"] == "discard_new" else "returned"
    return {"status": status, "accepted_head": state["context"]["accepted_head"],
            "files": [operation["path"] for operation in state["operations"]]}


def _read_journal(journal):
    if journal.is_symlink() or journal.exists() and not journal.is_file():
        raise ValueError(f"{journal.name}: invalid return journal path")
    if not journal.exists():
        return None
    state = json.loads(journal.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)
    if not isinstance(state, dict) or state.get("stage") not in {"prepared", "applying", "saved", "finished"}:
        raise ValueError(f"{journal.name}: invalid return journal state")
    return state


def _assert_saved_values(vault, state):
    _assert_main(vault, state)
    live = capture(vault)
    for operation in state["operations"]:
        if not operation["applied"] or live.get(operation["path"]) != _entry(operation["replacement"]):
            raise ValueError(f"{operation['path']}: saved result changed before worktree removal")


def _run(vault, common, path, branch, identity, context, writer, *, resume=False):
    journal = common / f"theseus-return-{path.name}.json"
    state = _read_journal(journal)
    if resume and state is None:
        raise ValueError("the selected return journal disappeared")
    if state is not None:
        if state.get("vault", str(vault)) != str(vault) or resume and "vault" not in state:
            raise ValueError("return journal belongs to another vault or lacks vault provenance")
        if state["context"] != context or state["branch"] != branch:
            raise ValueError("return journal belongs to another revision or action")
        if state["stage"] == "finished":
            return _finish(vault, path, journal, state)
        if state["stage"] == "saved" and not os.path.lexists(path) and path not in _registered(vault):
            _assert_saved_values(vault, state)
            return _finish(vault, path, journal, state)
        _assert_main(vault, state)
    selected = verify_worktree(vault, **identity)
    head, action = context["accepted_head"], context["action"]
    block, plan, work = context["block"], context["plan"], context["work"]

    def check_candidate():
        _assert_clean_candidate(vault, identity, selected, head)

    def validate_choice():
        handoff = Evaluation(vault, **identity, block=block, plan=plan).validate_choice(
            report_id=context["report_id"], action=action,
        )
        if handoff["pins"]["new"] != head or handoff["pins"]["old"] != selected.base:
            raise ValueError("the refactoring choice does not match the owned revisions")
        return handoff

    check_candidate()
    receipt, changes = None, ()
    if action == "return_new":
        receipt, report = verified_receipt(selected, block=block, plan=plan, work=work, accepted_head=head)
        changes = _changes_to_return(selected, block=block, plan=plan, work=work, head=head, report=report)
    if state is None:
        choice = validate_choice() if work == "refactoring" else None
        main_head = _line(vault, "rev-parse", "HEAD")
        if choice is not None and choice["pins"]["main_head"] != main_head:
            raise ValueError("main vault HEAD changed after the refactoring choice")
        state = {"vault": str(vault), "context": context, "branch": branch, "base": selected.base, "receipt": receipt,
                 "choice": choice, "main_head": main_head, "main_index": _main_index(vault),
                 "stage": "prepared", "active": None,
                 "operations": _operations(capture(vault), changes, context)}
        _persist(journal, state)
    elif (state["base"], state["receipt"]) != (selected.base, receipt):
        raise ValueError("owned worktree base or verification receipt changed")
    if state["stage"] != "saved":
        _assert_main(vault, state)
        if work == "refactoring" and not _transfer_started(vault, state):
            if validate_choice() != state["choice"]:
                raise ValueError("refactoring choice changed since return preparation")
        _publish(vault, journal, state, changes, check_candidate, writer)
        state["stage"] = "saved"
        _persist(journal, state)
    check_candidate()
    _assert_saved_values(vault, state)
    _git(vault, "worktree", "remove", str(path))
    return _finish(vault, path, journal, state)


def _main_repository(vault):
    vault, common = _repository(vault)
    if _line(vault, "rev-parse", "--absolute-git-dir") != str(common):
        raise ValueError("return requires the main vault, outside the linked worktree")
    return vault, common


def _context(*, ticket_id, slug, block, plan, work, accepted_head, attempt=1,
             action="return_new", report_id=None):
    block, plan = note_path(block, "Sources"), note_path(plan, "Plans")
    if work not in WORK_TYPES or action not in {"return_new", "discard_new"}:
        raise ValueError("invalid work type or return action")
    if work == "refactoring":
        if not isinstance(report_id, str) or not re.fullmatch(r"[0-9a-f]{64}", report_id):
            raise ValueError("refactoring return requires the exact evaluation report ID")
    elif action != "return_new" or report_id is not None:
        raise ValueError("evaluation choices apply only to refactoring")
    if not isinstance(accepted_head, str) or not re.fullmatch(OBJECT_ID, accepted_head):
        raise ValueError("return requires the full accepted Git revision")
    return dict(ticket_id=ticket_id, slug=slug, attempt=attempt, block=block, plan=plan, work=work,
                accepted_head=accepted_head, action=action, report_id=report_id)


def _return_lock(common):
    return os.fdopen(os.open(common / "theseus-worktrees.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
                             0o600), "a")


def _blocked(error):
    problem = error.stderr if isinstance(error, subprocess.CalledProcessError) else None
    if isinstance(problem, bytes):
        problem = problem.decode(errors="replace")
    return {"status": "blocked", "problems": [(problem or "").strip() or str(error)]}


def run_return(vault, *, ticket_id, slug, block, plan, work, accepted_head, attempt=1,
               action="return_new", report_id=None, conditional_writer=None):
    """Publish through an injected conditional writer, then remove the owned clean worktree.

    The writer receives (vault, path, expected Entry or None, replacement Entry or
    None). Return True only after comparing expected bytes and mode or absence
    and durably applying exactly that replacement. Return False without writing
    on detected mismatch.
    The backend must also guard path traversal and keep Git refs and index untouched.
    The installed command supplies a backend with a final comparison immediately
    before atomic publication and a residual race with non-cooperating editors.
    """
    try:
        if conditional_writer is not None and not callable(conditional_writer):
            raise ValueError("conditional writer must be callable")
        identity = dict(ticket_id=ticket_id, slug=slug, attempt=attempt)
        context = _context(**identity, block=block, plan=plan, work=work, accepted_head=accepted_head,
                           action=action, report_id=report_id)
        vault, common = _main_repository(vault)
        path, branch = _location(vault, **identity)
        with _return_lock(common) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return _run(vault, common, path, branch, identity, context, conditional_writer)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as error:
        return _blocked(error)


def _find_return(vault, common, ticket_id, slug, attempt):
    path, _ = _location(vault, ticket_id, "return" if slug is None else slug, 1 if attempt is None else attempt)
    exact = slug is not None and attempt is not None
    if exact:
        journals = (common / f"theseus-return-{path.name}.json",)
    else:
        prefix = ticket_id.lower().replace("/", "-")
        journals = sorted(common.glob(f"theseus-return-{prefix}-*.json"))
    matches = []
    for journal in journals:
        state = _read_journal(journal)
        if state is None:
            continue
        context = state["context"]
        if context["ticket_id"] != ticket_id or (slug is not None and context["slug"] != slug) or (
                attempt is not None and context["attempt"] != attempt):
            # Filename prefixes can also match nested tickets or numbered slugs.
            if exact:
                raise ValueError(f"{journal.name}: return journal belongs to another attempt or ticket")
            continue
        identity = {key: context[key] for key in ("ticket_id", "slug", "attempt")}
        path, branch = _location(vault, **identity)
        if (journal.name != f"theseus-return-{path.name}.json" or state["branch"] != branch
                or state.get("vault") != str(vault) or _context(**context) != context):
            raise ValueError(f"{journal.name}: return journal context or vault provenance does not match")
        matches.append((path, branch, identity, state))
    if len(matches) > 1:
        options = ", ".join(f"{identity['slug']} (attempt {identity['attempt']})"
                            for _, _, identity, _ in matches)
        raise ValueError(f"multiple return journals for {ticket_id}: specify slug and attempt ({options})")
    return matches[0] if matches else None


def inspect_return(vault, *, ticket_id, slug=None, attempt=None):
    """Find a saved return for routing without applying it or creating a journal."""
    try:
        vault, common = _main_repository(vault)
        selected = _find_return(vault, common, ticket_id, slug, attempt)
        if selected is None:
            return {"status": "missing"}
        state = selected[3]
        return {"status": "found", "stage": state["stage"], "context": state["context"]}
    except (KeyError, OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as error:
        return _blocked(error)


def resume_return(vault, *, ticket_id, slug=None, attempt=None, conditional_writer=None):
    """Resume only the saved action and revision, with the same checks as an initial return."""
    try:
        if conditional_writer is not None and not callable(conditional_writer):
            raise ValueError("conditional writer must be callable")
        vault, common = _main_repository(vault)
        with _return_lock(common) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            selected = _find_return(vault, common, ticket_id, slug, attempt)
            if selected is None:
                raise ValueError(f"no saved return journal for {ticket_id}")
            path, branch, identity, state = selected
            return _run(vault, common, path, branch, identity, state["context"], conditional_writer, resume=True)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as error:
        return _blocked(error)


def main():
    try:
        arguments = json.load(sys.stdin, object_pairs_hook=unique_fields)
        if not isinstance(arguments, dict):
            raise ValueError("expected a JSON object")
        if "conditional_writer" in arguments:
            raise ValueError("the installed return command does not accept a conditional writer from JSON")
        mode = arguments.pop("mode", "start")
        handlers = {"start": run_return, "inspect": inspect_return, "resume": resume_return}
        if not isinstance(mode, str) or mode not in handlers:
            raise ValueError("return mode must be start, inspect or resume")
        if mode in {"start", "resume"}:
            arguments["conditional_writer"] = _installed_write
        result = handlers[mode](**arguments)
    except (TypeError, ValueError) as error:
        result = _blocked(error)
    print(_json(result))
    return int(result["status"] == "blocked")


if __name__ == "__main__":
    sys.exit(main())
