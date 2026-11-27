"""Build the Theseus skill bundle from the pinned local Superpowers archive."""

import argparse
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


SOURCE = Path(__file__).resolve().parent
PACKAGE = "theseus"
SKILLS = {
    "using-theseus": ("using-superpowers", ("references/codex-tools.md",)),
    "brainstorming": ("brainstorming", ("builder-check-prompt.md",)),
    "using-git-worktrees": ("using-git-worktrees", ()),
    "writing-plans": ("writing-plans", ()),
    "subagent-driven-development": ("subagent-driven-development", (
        "implementer-prompt.md", "re-review-prompt.md", "task-reviewer-prompt.md",
        "references/batching.md", "references/cannot-verify.md", "references/final-review.md",
        "references/fix-loop.md", "references/handling-reports.md", "references/preflight.md",
        "references/resuming.md", "scripts/review-package", "scripts/sdd-repo",
        "scripts/sdd-workspace", "scripts/task-brief",
    )),
    "test-driven-development": ("test-driven-development", ("writing-good-tests.md",)),
    "systematic-debugging": ("systematic-debugging", (
        "condition-based-waiting-example.ts", "condition-based-waiting.md",
        "defense-in-depth.md", "find-polluter.sh", "root-cause-tracing.md",
    )),
    "requesting-code-review": ("requesting-code-review", ("code-reviewer.md",)),
    "receiving-code-review": ("receiving-code-review", ()),
    "verification-before-completion": ("verification-before-completion", ()),
    "compare-implementations": (None, ()),
    "finishing-a-development-branch": ("finishing-a-development-branch", ()),
}


class BuildError(Exception):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def load_lock():
    lock = json.loads((SOURCE / "upstream.lock").read_text(encoding="utf-8"))
    if (
        not isinstance(lock, dict)
        or set(lock) != {"url", "commit", "sha256"}
        or not all(isinstance(value, str) for value in lock.values())
        or not lock["url"].startswith("https://")
        or not re.fullmatch(r"[0-9a-f]{40}", lock["commit"])
        or not re.fullmatch(r"[0-9a-f]{64}", lock["sha256"])
    ):
        raise BuildError("invalid upstream.lock")
    return lock


def path_parts(name):
    parts = name.split("/")
    if "\\" in name or any(part in {"", ".", ".."} for part in parts):
        raise BuildError(f"unsafe archive path: {name!r}")
    return parts


def extract_source(archive, lock, bundle):
    data = archive.read_bytes()
    actual = sha256(data)
    if actual != lock["sha256"]:
        raise BuildError(f"SHA-256 mismatch for {archive}: expected {lock['sha256']}, got {actual}")
    root = "superpowers-" + lock["commit"]
    wanted = {"LICENSE": "LICENSE"}
    for name, (upstream, resources) in SKILLS.items():
        if upstream is not None:
            for suffix in ("SKILL.md", *resources):
                wanted[f"skills/{upstream}/{suffix}"] = f"skills/{name}/{suffix}"

    paths = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as source:
        for member in source:
            parts = path_parts(member.name)
            if parts[0] != root or (len(parts) == 1 and not member.isdir()):
                raise BuildError(f"unexpected archive root: {member.name!r}")
            if member.name in paths:
                raise BuildError(f"duplicate archive path: {member.name!r}")
            paths.add(member.name)
            if member.isdir():
                continue
            if not member.isfile() or member.sparse is not None:
                raise BuildError(f"unsupported archive entry: {member.name!r}")
            relative = "/".join(parts[1:])
            if relative in wanted:
                target = bundle / wanted.pop(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as content, target.open("xb") as destination:
                    shutil.copyfileobj(content, destination)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    if wanted:
        raise BuildError("archive is missing required files: " + ", ".join(sorted(wanted)))


def file_inventory(directory):
    entries = {}
    for parent, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs + files:
            path = Path(parent) / name
            relative = path.relative_to(directory).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise BuildError(f"unsupported bundle entry: {relative}")
            entries[relative] = {"sha256": sha256(path.read_bytes()), "mode": stat.S_IMODE(mode)}
    return entries


def check_bundle(bundle, files):
    for relative in files:
        parts = PurePosixPath(relative).parts
        if relative == "LICENSE":
            continue
        if (len(parts) < 3 or parts[0] != "skills" or parts[1] not in SKILLS
                or any(part.startswith(".") for part in parts)):
            raise BuildError(f"unexpected bundle file: {relative}")
    for name, (upstream, _) in SKILLS.items():
        if upstream is not None and f"skills/{name}/SKILL.md" not in files:
            raise BuildError(f"missing SKILL.md for {name}")
    license_path = bundle / "LICENSE"
    if "LICENSE" not in files or not license_path.read_bytes().startswith(b"MIT License\n"):
        raise BuildError("bundle is missing the upstream MIT license")


def apply_patches(bundle):
    patch_dir = SOURCE / "patches"
    patches = []
    seen = set()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CEILING_DIRECTORIES=str(bundle.parent), GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull)
    for entry in (patch_dir / "series").read_text(encoding="utf-8").splitlines():
        name = entry.strip()
        if not name or name.startswith("#"):
            continue
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*\.patch", name) or name in seen:
            raise BuildError(f"invalid or duplicate patch in series: {name!r}")
        seen.add(name)
        patch = patch_dir / name
        if patch.is_symlink() or not patch.is_file():
            raise BuildError(f"patch is not a regular file: {name}")
        data = patch.read_bytes()
        result = subprocess.run(
            ["git", "-c", "core.protectHFS=true", "-c", "core.protectNTFS=true",
             "apply", "--whitespace=nowarn", "-"],
            cwd=bundle, env=env, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            umask=0o022,
        )
        if result.returncode:
            raise BuildError(f"patch failed: {name}\n{result.stderr.decode(errors='replace').strip()}")
        check_bundle(bundle, file_inventory(bundle))
        patches.append({"path": name, "sha256": sha256(data)})
    return patches


def write_manifest(bundle, lock, patches):
    files = file_inventory(bundle)
    check_bundle(bundle, files)
    skills = {}
    for name, (upstream, _) in SKILLS.items():
        path = f"skills/{name}/SKILL.md"
        skills[name] = {"upstream": upstream, "path": path, "included": path in files}
    manifest = {"package": PACKAGE, "format": 1, "upstream": lock,
                "patches": patches, "skills": skills, "files": files}
    target = bundle / "manifest.json"
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    target.chmod(0o644)


def confirm_previous_install(output):
    if output.is_symlink() or not output.is_dir():
        raise BuildError(f"output is not a Theseus installation: {output}")
    manifest_path = output / "manifest.json"
    if not manifest_path.exists() and not any(output.iterdir()):
        return
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise BuildError(f"output is not a Theseus installation: {output}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise BuildError(f"output is not a Theseus installation: {output}") from exc
    if (not isinstance(manifest, dict) or manifest.get("package") != PACKAGE
            or manifest.get("format") != 1):
        raise BuildError(f"output is not a Theseus installation: {output}")
    current = file_inventory(output)
    current.pop("manifest.json")
    expected_dirs = {
        parent.as_posix()
        for name in current
        for parent in PurePosixPath(name).parents
    } - {"."}
    actual_dirs = {Path(root).relative_to(output).as_posix() for root, _, _ in os.walk(output)} - {"."}
    if current != manifest.get("files") or actual_dirs != expected_dirs:
        raise BuildError(f"output has been modified: {output}")
    check_bundle(output, current)


def publish(bundle, output):
    # The parent lock serializes builders publishing to this installation directory.
    parent_fd = os.open(output.parent, os.O_RDONLY)
    try:
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        if not os.path.lexists(output):
            bundle.rename(output)
            return
        confirm_previous_install(output)
        backup_dir = Path(tempfile.mkdtemp(prefix=".theseus-backup-", dir=output.parent))
        previous = backup_dir / "previous"
        installed = False
        try:
            output.rename(previous)
            try:
                bundle.rename(output)
                installed = True
            except BaseException:
                try:
                    previous.rename(output)
                except OSError as exc:
                    raise BuildError(f"could not restore {output}; previous bundle is at {previous}") from exc
                raise
        finally:
            if installed or not previous.exists():
                shutil.rmtree(backup_dir)
    finally:
        os.close(parent_fd)


def build(archive, output):
    lock = load_lock()
    if output.name in {"", ".."}:
        raise BuildError(f"invalid output directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output = output.parent.resolve() / output.name
    with tempfile.TemporaryDirectory(prefix=".theseus-build-", dir=output.parent) as temporary:
        bundle = Path(temporary) / "bundle"
        bundle.mkdir()
        extract_source(archive, lock, bundle)
        scripts = bundle / "skills" / "using-theseus" / "scripts"
        scripts.mkdir()
        for name in ("routing.py", "codex.py", "notes.py", "scope.py", "planning.py", "worktree.py",
                     "execution.py", "review.py", "diagnostics.py", "verification.py", "refactoring.py",
                     "evaluation.py", "return_result.py"):
            script = scripts / name
            shutil.copyfile(SOURCE / name, script)
            script.chmod(0o644)
        patches = apply_patches(bundle)
        write_manifest(bundle, lock, patches)
        publish(bundle, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True, help="Pinned local Superpowers tar.gz")
    parser.add_argument("--output", type=Path, required=True, help="Destination for the bundle")
    args = parser.parse_args()
    try:
        build(args.archive, args.output)
    except (BuildError, ValueError, OSError, tarfile.TarError) as exc:
        parser.exit(1, f"build: {exc}\n")


if __name__ == "__main__":
    main()
