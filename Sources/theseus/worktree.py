"""Prepare isolated development from a snapshot and explicitly supplied live inputs."""

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__:
    from .routing import canonical_paths, require_boolean
else:
    from routing import canonical_paths, require_boolean


TICKET_ID = r"(?:[A-Z][A-Z0-9]*-)?[1-9][0-9]*(?:/[1-9][0-9]*)*"
OBJECT_ID = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
GIT_IDENTITY = {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE"}


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    base: str
    snapshot_a: str
    inputs: tuple[str, ...]
    attempt: int


def _commit_files(root, admin, parent, changes, subject):
    with tempfile.TemporaryDirectory(dir=admin) as temporary:
        index = Path(temporary) / "index"
        _git(root, "read-tree", parent, index=index)
        for path, entry in sorted(changes.items()):
            if entry is None:
                _git(root, "update-index", "--force-remove", "--", path, index=index)
            else:
                blob = _git(root, "hash-object", "-w", "--stdin", data=entry.data).stdout.decode().strip()
                mode = "100755" if entry.mode & 0o100 else "100644"
                _git(root, "update-index", "--add", "--cacheinfo", mode, blob, path, index=index)
        tree = _git(root, "write-tree", index=index).stdout.strip()
    return _line(root, "-c", "commit.gpgsign=false", "commit-tree", tree, "-p", parent, "-m", subject)


def _publish_commit(root, parent, commit):
    _clean(root)
    if _line(root, "rev-parse", "HEAD") != parent:
        raise ValueError("worktree HEAD moved during setup")
    _git(root, "-c", "merge.autoStash=false", "-c", "merge.verifySignatures=false", "merge",
         "--ff-only", "--no-edit", "--no-overwrite-ignore", commit)
    _clean(root)


def _save_record(path, record):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(record, file)
            file.flush()
            os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def owned_checkout(root):
    """Resolve and verify an existing Theseus linked checkout from its Git registration."""
    root, common = _repository(root)
    admin = Path(_line(root, "rev-parse", "--absolute-git-dir"))
    marker = admin / "theseus-owner.json"
    if (root.parent.name != ".worktrees" or admin.parent != common / "worktrees"
            or marker.is_symlink() or not marker.is_file()):
        raise ValueError("Bug fix planning requires a prepared Theseus worktree")
    record = json.loads(marker.read_text(encoding="utf-8"))
    vault = root.parent.parent
    identity = dict(ticket_id=record["ticket"], slug=record["slug"], attempt=record["attempt"])
    selected = verify_worktree(vault, **identity)
    if selected.path != root:
        raise ValueError("ownership record refers to a different worktree")
    return vault, identity, selected


def _git(root, *args, check=True, index=None, data=None):
    # The watcher may call us with an alternate index or other repository overrides.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") or key in GIT_IDENTITY}
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    return subprocess.run(
        ["git", "--no-replace-objects", "-C", str(root), "-c", f"core.hooksPath={os.devnull}", *args],
        check=check, capture_output=True, text=data is None, input=data, env=env,
    )


def _clean_index(root):
    if _git(root, "diff", "--cached", "--quiet", "HEAD", "--", check=False).returncode:
        raise ValueError("the worktree index contains staged changes")
    entries = _git(root, "ls-files", "-v", "-z").stdout.split("\0")
    if any(entry and not entry.startswith("H ") for entry in entries):
        raise ValueError("the worktree index contains hidden or unmerged paths")


def _clean(root):
    _clean_index(root)
    if _git(root, "diff-files", "--quiet", "--ignore-submodules=none", "--",
            check=False).returncode:
        # Refresh stat-only changes before deciding that bytes differ.
        if _git(root, "diff", "--quiet", "--ignore-submodules=none", "--",
                check=False).returncode:
            raise ValueError("the worktree contains uncommitted tracked changes")
    if _git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout:
        raise ValueError("the worktree contains untracked changes")


def _line(root, *args):
    return _git(root, *args).stdout.rstrip("\n")


def _checked_path(root, name):
    path = root
    for part in name.split("/"):
        path /= part
        if path.is_symlink():
            raise ValueError(f"symlink input: {name}")
    return path


def _read_inputs(root, inputs):
    result = {}
    for name in inputs:
        if any(part.casefold() in {".git", ".worktrees"} for part in name.split("/")):
            raise ValueError(f"reserved input: {name}")
        source = _checked_path(root, name)
        if not source.exists():
            result[name] = None
        elif source.is_file() and source.stat().st_nlink == 1:
            result[name] = (source.read_bytes(), source.stat().st_mode & 0o777)
        else:
            raise ValueError(f"expected a regular file: {name}")
    return result


def _write_inputs(path, contents):
    staged = []
    for name, entry in contents.items():
        target = _checked_path(path, name)
        if entry is None:
            if not target.exists():
                continue
            target.unlink()
        else:
            data, mode = entry
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(mode)
        staged.append(name)
    if staged:
        _git(path, "--literal-pathspecs", "add", "-f", "-A", "--", *staged)


def _registered(root):
    entries = _git(root, "worktree", "list", "--porcelain", "-z").stdout.split("\0\0")
    return {Path(fields[0].removeprefix("worktree ")): fields[1:]
            for entry in entries if (fields := entry.split("\0"))[0].startswith("worktree ")}


def _admin_dir(path, common, branch):
    pointer = path / ".git"
    if path.is_symlink() or not path.is_dir() or pointer.is_symlink() or not pointer.is_file():
        raise ValueError(f"invalid linked worktree: {path}")
    try:
        admin = Path(_line(path, "rev-parse", "--absolute-git-dir"))
        if (
            admin.parent != common / "worktrees" or admin.is_symlink()
            or Path(_line(path, "rev-parse", "--show-toplevel")) != path
            or _line(path, "symbolic-ref", "HEAD") != "refs/heads/" + branch
        ):
            raise ValueError(f"invalid linked worktree: {path}")
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"invalid linked worktree: {path}") from exc
    return admin


def _owned(root, path, branch, common, ticket_id, slug, attempt):
    try:
        if "branch refs/heads/" + branch not in _registered(root).get(path, ()):
            raise ValueError("branch and directory do not match Git's registration")
        marker = _admin_dir(path, common, branch) / "theseus-owner.json"
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("missing ownership record")
        data = json.loads(marker.read_text(encoding="utf-8"))
        if (
            data["ticket"] != ticket_id or data["path"] != str(path)
            or data["branch"] != branch or data["slug"] != slug or data["attempt"] != attempt
            or not re.fullmatch(OBJECT_ID, data["base"])
            or not re.fullmatch(OBJECT_ID, data["snapshot_a"])
            or tuple(sorted(canonical_paths(data["inputs"]))) != tuple(data["inputs"])
            or _line(path, "rev-parse", data["base"] + "^") != data["snapshot_a"]
            or _git(path, "merge-base", "--is-ancestor", data["base"], "HEAD",
                    check=False).returncode
        ):
            raise ValueError("ownership record does not match worktree")
    except (KeyError, OSError, TypeError, ValueError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"worktree is not owned by {ticket_id}: {path} ({exc})") from exc
    return Worktree(path, branch, data["base"], data["snapshot_a"], tuple(data["inputs"]), attempt)


def _location(root, ticket_id, slug, attempt):
    if not isinstance(ticket_id, str) or not re.fullmatch(TICKET_ID, ticket_id):
        raise ValueError("invalid ticket ID")
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError("expected a kebab-case slug")
    if type(attempt) is not int or attempt < 1:
        raise ValueError("attempt must be a positive integer")
    directory = root / ".worktrees"
    if directory.is_symlink():
        raise ValueError("worktree directory is a symlink")
    name = ticket_id.lower().replace("/", "-") + "-" + slug
    if attempt > 1:
        name += f"-{attempt}"
    return directory / name, "theseus/" + name


def _repository(root):
    root = Path(root).resolve()
    if Path(_line(root, "rev-parse", "--show-toplevel")) != root:
        raise ValueError("expected the vault root")
    common = Path(_line(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    return root, common


def verify_worktree(root, *, ticket_id, slug, attempt=1):
    """Verify the selected attempt against Git's registration and its ownership record."""
    root, common = _repository(root)
    path, branch = _location(root, ticket_id, slug, attempt)
    return _owned(root, path, branch, common, ticket_id, slug, attempt)


def _cleanup_setup(root, path, branch, common, identity, expected_head, branch_created):
    """Roll back preparation while the created directory and branch still match."""
    ref = "refs/heads/" + branch
    registered = _registered(root)
    if os.path.lexists(path):
        stat = path.lstat()
        if path.is_symlink() or (stat.st_dev, stat.st_ino) != identity:
            raise ValueError(f"setup directory changed ownership: {path}")
        if path in registered:
            if "branch " + ref not in registered[path] or not branch_created:
                raise ValueError(f"setup registration changed ownership: {path}")
            _admin_dir(path, common, branch)
            if _line(path, "rev-parse", "HEAD") != expected_head:
                raise ValueError(f"setup branch changed during preparation: {branch}")
            _git(root, "worktree", "remove", "--force", str(path))
        else:
            path.rmdir()
    if branch_created:
        if any("branch " + ref in fields for fields in _registered(root).values()):
            raise ValueError(f"setup branch is checked out: {branch}")
        _git(root, "update-ref", "--no-deref", "-d", ref, expected_head)


def prepare_worktree(root, *, ticket_id, slug, snapshot_a=None, inputs=(), independent=False, attempt=1):
    """Resume a selected attempt, or create an independent attempt from the original base."""
    require_boolean(independent, "independent")
    if independent and attempt != 1:
        raise ValueError("an independent attempt starts from the original")
    root, common = _repository(root)
    path, branch = _location(root, ticket_id, slug, attempt)
    lock_path = common / "theseus-worktrees.lock"
    with os.fdopen(os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)

        def occupied(path, branch):
            registered = _registered(root)
            return (
                os.path.lexists(path) or path in registered
                or any(f"branch refs/heads/{branch}" in fields for fields in registered.values())
                or _git(root, "show-ref", "--verify", "--quiet", "refs/heads/" + branch,
                        check=False).returncode == 0
            )

        if independent:
            original = _owned(root, path, branch, common, ticket_id, slug, attempt)
            snapshot_a, inputs, start = original.snapshot_a, original.inputs, original.base
            attempt = 2
            path, branch = _location(root, ticket_id, slug, attempt)
            while occupied(path, branch):
                attempt += 1
                path, branch = _location(root, ticket_id, slug, attempt)
        elif attempt != 1 or occupied(path, branch):
            return _owned(root, path, branch, common, ticket_id, slug, attempt)
        else:
            if not isinstance(snapshot_a, str) or not re.fullmatch(OBJECT_ID, snapshot_a):
                raise ValueError("expected the full snapshot A commit ID")
            start = _line(root, "rev-parse", "--verify", snapshot_a + "^{commit}")
            if start != snapshot_a:
                raise ValueError("snapshot A must be a commit")
            inputs = tuple(sorted(canonical_paths(inputs)))
            contents = _read_inputs(root, inputs)

        path.parent.mkdir(exist_ok=True)
        path.mkdir()
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        ref = "refs/heads/" + branch
        branch_created = False
        expected_head = start
        try:
            _git(root, "update-ref", "--no-deref", ref, start, "0" * len(start))
            branch_created = True
            _git(root, "worktree", "add", str(path), branch)
            admin = _admin_dir(path, common, branch)
            if _line(path, "rev-parse", "HEAD") != start:
                raise ValueError(f"setup branch changed during preparation: {branch}")
            if not independent:
                _write_inputs(path, contents)
                _git(path, "-c", "commit.gpgsign=false", "commit", "--allow-empty",
                     "-m", f"Prepare Theseus inputs for {ticket_id}")
                expected_head = _line(path, "rev-parse", "HEAD")
            with (admin / "theseus-owner.json").open("x", encoding="utf-8") as marker:
                json.dump(dict(ticket=ticket_id, path=str(path), branch=branch, base=expected_head,
                               snapshot_a=snapshot_a, inputs=inputs, slug=slug, attempt=attempt), marker)
        except BaseException as exc:
            try:
                _cleanup_setup(root, path, branch, common, identity, expected_head, branch_created)
            except (OSError, ValueError, subprocess.CalledProcessError) as cleanup_exc:
                raise RuntimeError(f"{exc}; partial setup preserved: {cleanup_exc}") from exc
            raise
        return Worktree(path, branch, expected_head, snapshot_a, inputs, attempt)


def main():
    try:
        request = json.load(sys.stdin)
        operation = request.pop("operation", "prepare")
        if operation == "prepare":
            result = prepare_worktree(**request)
        elif operation == "verify":
            result = verify_worktree(**request)
        else:
            raise ValueError(f"unknown worktree operation: {operation}")
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, subprocess.CalledProcessError) as exc:
        error = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(json.dumps({"error": error}))
        return 1
    result = asdict(result)
    print(json.dumps(dict(result, path=str(result["path"]))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
