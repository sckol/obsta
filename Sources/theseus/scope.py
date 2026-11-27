"""Verify the actual file and note changes allowed by a Theseus task."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

if __package__:
    from .notes import changed_sections, clear_section, preserve_protected
    from .routing import WORK_TYPES, canonical_path, canonical_paths, note_path
else:
    from notes import changed_sections, clear_section, preserve_protected
    from routing import WORK_TYPES, canonical_path, canonical_paths, note_path


@dataclass(frozen=True)
class Entry:
    kind: str
    data: bytes = b""
    mode: int = 0


@dataclass(frozen=True)
class Change:
    path: str
    before: Entry | None
    after: Entry | None
    sections: tuple = ()


@dataclass(frozen=True)
class Block:
    note: str
    implementation: str
    kind: str
    descendants: tuple


_DIRECTORY = Entry("directory")
_CODERS = {"coder", "coder_light", "coder_heavy"}
_STAGES = {
    "plan": {"planner"},
    "execute": _CODERS | {"orchestrator"},
    "local": {"obstagent"},
    "cleanup": {"obstagent"},
    "return": {"obstagent"},
    "review": {"reviewer"},
    "evaluate": {"evaluator"},
    "brainstorm": {"obstagent"},
    "diagnose": {"obstagent"},
}
_PLAN_PROGRESS = (("Context", "Plan status"), "Tasks", "Outcome")
_BLOCK_TEMPLATE = re.compile(rb"(?m)^Template:[ \t]*Building block[ \t]*\r?$")
_PLAN_STATUS = re.compile(rb"""^[ \t]*(?:Status|'Status'|"Status")[ \t]*:""")


def _entry(directory, name):
    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if stat.S_ISDIR(info.st_mode):
        return _DIRECTORY
    if stat.S_ISLNK(info.st_mode):
        return Entry("symlink", os.fsencode(os.readlink(name, dir_fd=directory)))
    if not stat.S_ISREG(info.st_mode):
        return Entry("special", mode=info.st_mode)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(name, flags, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as opened:
        initial = os.fstat(opened.fileno())
        if not os.path.samestat(info, initial):
            raise ValueError(f"path changed while capturing {name}")
        data = opened.read()
        final = os.fstat(opened.fileno())
    if (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (
        final.st_size, final.st_mtime_ns, final.st_ctime_ns
    ):
        raise ValueError(f"path changed while capturing {name}")
    kind = "hardlink" if final.st_nlink > 1 else "file"
    return Entry(kind, data, stat.S_IMODE(final.st_mode))


def capture(root):
    """Capture vault-relative files without following symlinks or reading worktree metadata."""
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("snapshot root must be a real vault directory")
    entries = {}

    def failed(error):
        raise error

    for parent, folders, files, directory in os.fwalk(root, onerror=failed, follow_symlinks=False):
        relative = Path(parent).relative_to(root)
        if relative == Path("."):
            folders[:] = [name for name in folders if name not in {".git", ".worktrees"}]
            files = [name for name in files if name not in {".git", ".worktrees"}]
        for name in folders + files:
            path = (relative / name).as_posix()
            entries[path] = _entry(directory, name)
    return MappingProxyType(entries)


def _check_path(state, path):
    canonical_path(path)
    parts = path.split("/")
    for end in range(1, len(parts) + 1):
        prefix = "/".join(parts[:end])
        entry = state.get(prefix)
        if entry is not None and entry.kind in {"symlink", "hardlink"}:
            raise ValueError(f"{path}: {entry.kind} at {prefix}")


def _implementation(note):
    path = note[:-3]
    kind = "file" if PurePosixPath(path).suffix else "folder"
    return path, kind


def _frontmatter(data):
    lines = data.removeprefix(b"\xef\xbb\xbf").splitlines(keepends=True)
    if not lines or lines[0].rstrip(b"\r\n") != b"---":
        return ()
    for index, line in enumerate(lines[1:], 1):
        if line.strip() in (b"---", b"..."):
            return lines[1:index]
    raise ValueError("unclosed Markdown frontmatter")


def _plan_status(data):
    lines = _frontmatter(data)
    matches = [index for index, line in enumerate(lines) if _PLAN_STATUS.match(line)]
    if len(matches) > 1:
        raise ValueError("ambiguous plan frontmatter Status")
    if not matches:
        return b""
    start = matches[0]
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = start + 1
    while end < len(lines) and len(lines[end]) - len(lines[end].lstrip()) > indent:
        end += 1
    return b"".join(lines[start:end])


def block_scope(state, note):
    """Resolve the selected block and independent child contracts under its implementation."""
    note_path(note, "Sources")
    _check_path(state, note)
    if note not in state or state[note].kind != "file":
        raise ValueError(f"missing building-block note: {note}")
    implementation, kind = _implementation(note)
    _check_path(state, implementation)
    entry = state.get(implementation)
    if entry is not None and entry.kind != ("directory" if kind == "folder" else "file"):
        raise ValueError(f"{note}: expected a {kind} implementation at {implementation}")
    descendants = []
    if kind == "folder":
        for path, candidate in state.items():
            if path.startswith(implementation + "/") and path.endswith(".md"):
                paired, _ = _implementation(path)
                if candidate.kind in {"file", "hardlink"} and (
                    paired in state or _BLOCK_TEMPLATE.search(b"".join(_frontmatter(candidate.data)))
                ):
                    descendants.append(path)
    return Block(note, implementation, kind, tuple(sorted(descendants)))


def _inside(path, implementation, kind):
    return path == implementation if kind == "file" else path.startswith(implementation + "/")


def _belongs(path, block, descendants, work):
    if not _inside(path, block.implementation, block.kind) or path in descendants:
        return False
    if work != "refactoring":
        return True
    return not any(_inside(path, *_implementation(note)) or path == _implementation(note)[0]
                   for note in descendants)


def _note_sections(stage, work):
    if stage == "plan":
        return ("Specification",)
    if stage == "execute":
        return ("Working context",)
    if stage == "cleanup" or stage == "return" and work in {"refactoring", "bug_fix"}:
        return ("Specification", "Working context")
    if stage == "return":
        return ("Working context",)
    return ()


def _section_allowlist(sections, writable_notes, defaults):
    if sections is None:
        return None
    result = {}
    for path, selectors in sections.items():
        if path not in writable_notes:
            raise ValueError(f"{path}: note is outside the writable block scope")
        result[path] = []
        for selector in selectors:
            if isinstance(selector, str):
                selector = (selector,)
            if (not isinstance(selector, tuple) or not selector or selector[0] not in defaults):
                raise ValueError(f"{path}: section is not writable: {selector!r}")
            result[path].append(selector)
    return result


def _check_note(change, selectors, stage, optional, *, preserve_fences):
    before, after = change.before, change.after
    if before is None or after is None or before.mode != after.mode:
        raise ValueError(f"{change.path}: a block note may only change existing section bodies")
    changes = changed_sections(before.data, after.data, selectors, root_level=2,
                               optional=optional, preserve_fences=preserve_fences)
    if stage == "cleanup":
        expected = before.data
        for section in changes:
            expected = clear_section(expected, section, root_level=2)
        if after.data != expected:
            raise ValueError(f"{change.path}: cleanup may only clear writable section bodies")
    return changes


def _check_plan(change, stage):
    before, after = change.before, change.after
    if after is None or before is None and stage != "plan":
        raise ValueError(f"{change.path}: this stage cannot create or delete the current plan")
    if before is not None and before.mode != after.mode:
        raise ValueError(f"{change.path}: the current plan mode changed")
    status = _plan_status(after.data)
    if before is not None and _plan_status(before.data) != status:
        raise ValueError(f"{change.path}: plan frontmatter Status is user-owned")
    if stage == "execute":
        changed_sections(before.data, after.data, _PLAN_PROGRESS, root_level=2, optional=True)
    else:
        preserve_protected(before.data if before else b"", after.data)


def authorize(before, after, note, *, role, stage, work, plan=None, files=None, sections=None,
              experiments=None):
    """Check all changed leaves in two captured states and return their exact paths.

    `files` narrows implementation changes to exact paths. `sections` maps exact
    block note paths to permitted heading selectors. Both grant subsets of the
    role and stage rules. A move is a deletion plus an addition, so omitting
    rename detection cannot omit either endpoint from the authorization check.
    Pass the same snapshot twice to validate explicit targets before running an
    agent. Validation of captured states does not constrain live process access.
    """
    if stage not in _STAGES or role not in _STAGES[stage] or work not in WORK_TYPES:
        raise ValueError("invalid Theseus role, stage or work type")
    if stage == "cleanup" and work != "refactoring":
        raise ValueError("cleanup is only available during refactoring")
    if stage == "local" and work not in {"modification", "bug_fix"}:
        raise ValueError("only modification and bug fixing can run as local changes")
    if plan is not None:
        note_path(plan, "Plans")
    if experiments is not None:
        canonical_path(experiments)
        if not experiments.startswith(".obsta-experiments/"):
            raise ValueError("expected a ticket directory under .obsta-experiments")

    initial, final = block_scope(before, note), block_scope(after, note)
    descendants = set(initial.descendants) | set(final.descendants)
    writable_notes = {note}
    if work != "refactoring":
        writable_notes.update(descendants)
    note_sections = _note_sections(stage, work) if role not in _CODERS else ()
    file_allowlist = canonical_paths(files) if files is not None else None
    explicit_paths = set(file_allowlist or ()) | set(sections or ())
    explicit_paths.update(path for path in (plan, experiments) if path is not None)
    for path in sorted(explicit_paths):
        _check_path(before, path)
        _check_path(after, path)

    section_allowlist = _section_allowlist(sections, writable_notes, note_sections)
    write_implementation = role in _CODERS or stage in {"local", "cleanup", "return"}
    for path in sorted(file_allowlist or ()):
        if not write_implementation or not _belongs(path, initial, descendants, work):
            raise ValueError(f"{path}: not writable for {role} during {stage}")
        if any(path in state and state[path].kind != "file" for state in (before, after)):
            raise ValueError(f"{path}: expected a regular file")

    result = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old == new or {old, new} == {_DIRECTORY, None}:
            continue
        _check_path(before, path)
        _check_path(after, path)
        if any(entry is not None and entry.kind != "file" for entry in (old, new)):
            raise ValueError(f"{path}: only regular files are writable")
        change = Change(path, old, new)
        if path in writable_notes and note_sections:
            selectors = note_sections if section_allowlist is None else section_allowlist.get(path, ())
            try:
                changed = _check_note(
                    change, selectors, stage, section_allowlist is None,
                    preserve_fences=stage != "cleanup" and (stage, work) != ("return", "refactoring"),
                )
            except ValueError as error:
                raise ValueError(f"{path}: {error}") from error
            change = Change(path, old, new, changed)
        elif stage == "cleanup" and path.startswith("Plans/") and new is None:
            pass
        elif path == plan and role in {"planner", "orchestrator", "obstagent"} and stage in {
            "plan", "execute", "return"
        }:
            _check_plan(change, stage)
        elif (write_implementation and _belongs(path, initial, descendants, work)
              and (file_allowlist is None or path in file_allowlist)
              and (stage != "cleanup" or new is None)):
            pass
        elif (experiments is not None and path.startswith(experiments + "/")
              and role in {"orchestrator", "evaluator", "obstagent"}
              and stage in {"execute", "evaluate", "diagnose", "local"}):
            pass
        else:
            raise ValueError(f"{path}: not writable for {role} during {stage}")
        result.append(change)
    return tuple(result)
