"""Plan a routed Theseus ticket and validate the handoff before execution."""

from __future__ import annotations

import fcntl
import html
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

if __package__:
    from .codex import RoleExecutionError, run_role
    from .notes import changed_sections, read_section, replace_section
    from .routing import Decision, canonical_path, canonical_paths, note_path, unique_fields
    from .scope import Entry, authorize, capture
    from .worktree import OBJECT_ID, _clean, _commit_files, _git, _line, _publish_commit, _save_record
    from .worktree import owned_checkout, verify_worktree
else:
    from codex import RoleExecutionError, run_role
    from notes import changed_sections, read_section, replace_section
    from routing import Decision, canonical_path, canonical_paths, note_path, unique_fields
    from scope import Entry, authorize, capture
    from worktree import OBJECT_ID, _clean, _commit_files, _git, _line, _publish_commit, _save_record
    from worktree import owned_checkout, verify_worktree


WORK_LABELS = {
    "first": "First implementation", "modification": "Modification",
    "refactoring": "Refactoring", "bug_fix": "Bug fix",
}
BUG_FIX_PLAN_SUBJECT = "Prepare Theseus bug fix plan"
COMPLEXITY = {"specified", "design", "contract", "coordination", "end_to_end"}
ARCHITECTURE = ("Specification", "Architecture and techniques")
ACCEPTANCE = ("Specification", "Acceptance")
STATUS = ("Context", "Plan status")
TASKS = ("Tasks", "Execution order and progress")
TABLE_HEAD = """| # | *Task / exact files* | *Coder* | *Verification and expected result* | *Progress / result* |
| --- | --- | --- | --- | --- |"""


@dataclass(frozen=True)
class Check:
    command: str
    cwd: str
    setup: str
    expected: str


@dataclass(frozen=True)
class Task:
    description: str
    files: tuple
    coder: str
    complexity: str
    depends_on: tuple
    checks: tuple


@dataclass(frozen=True)
class Result:
    stage: str
    response: str
    plan: str | None = None
    tasks: tuple = ()


def line(value):
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError("expected nonempty single-line text")
    return value.strip()


def check(fields, state, files):
    item = Check(**fields)
    tokens = shlex.split(line(item.command))
    if not tokens or not tokens[0].strip() or tokens[0] in {"TBD", "TODO"}:
        raise ValueError("verification requires a concrete command")
    if item.cwd != ".":
        path = canonical_path(item.cwd)
        for parent in (Path(path), *Path(path).parents):
            entry = state.get(parent.as_posix())
            if entry and entry.kind != "directory":
                raise ValueError(f"invalid verification directory: {path}")
        if path not in state and not any(file.startswith(path + "/") for file in files):
            raise ValueError(f"missing verification directory: {path}")
    line(item.setup)
    line(item.expected)
    return item


def validated_tasks(items, allowed, state):
    if not isinstance(items, list) or not items:
        raise ValueError("a plan requires at least one task")
    tasks = []
    for number, fields in enumerate(items, 1):
        task = Task(**fields)
        line(task.description)
        paths = canonical_paths(task.files)
        if not paths or len(paths) != len(task.files) or not paths <= allowed:
            raise ValueError(f"task {number}: use unique exact paths from the approved scope")
        if any(any(char in path for char in "*?`") for path in paths):
            raise ValueError(f"task {number}: file globs are unsupported")
        if task.complexity not in COMPLEXITY:
            raise ValueError(f"task {number}: unknown complexity")
        expected_coder = "coder_light" if task.complexity == "specified" else "coder"
        if task.coder != expected_coder:
            raise ValueError(f"task {number}: {task.complexity} requires {expected_coder}")
        deps = task.depends_on
        if not isinstance(deps, (list, tuple)) or any(
            type(dep) is not int or not 1 <= dep < number for dep in deps
        ) or len(set(deps)) != len(deps):
            raise ValueError(f"task {number}: dependencies must refer to unique earlier tasks")
        if not isinstance(task.checks, (list, tuple)) or not task.checks:
            raise ValueError(f"task {number}: provide verification command, cwd, setup and expected result")
        checks = tuple(check(item, state, allowed) for item in task.checks)
        tasks.append(Task(task.description, tuple(task.files), task.coder, task.complexity, tuple(deps), checks))
    return tuple(tasks)


def cell(text):
    return html.escape(text).replace("|", "&#124;").replace("*", "&#42;").replace("`", "&#96;")


def code(text):
    return f"<code>{cell(text)}</code>"


def row(number, task):
    files = "<br>".join(code(path) for path in task.files)
    dependencies = ", ".join(map(str, task.depends_on)) or "none"
    checks = "<br>".join(
        f"{code(c.command)} · cwd {code(c.cwd)} · setup <em>{cell(c.setup)}</em> · expect <em>{cell(c.expected)}</em>"
        for c in task.checks
    )
    return (f"| {number} | *{cell(task.description)}*<br>{files} | "
            f"*`{task.coder}` · {task.complexity} · depends on {dependencies}* | {checks} | *Pending* |")


def parse_rows(data):
    table = read_section(data, TASKS, root_level=2).decode().strip().splitlines()
    if table[:2] != TABLE_HEAD.splitlines():
        raise ValueError("invalid task table header")
    tasks = []
    for number, row_text in enumerate(table[2:], 1):
        fields = row_text.split(" | ")
        if len(fields) != 5 or fields[0] != f"| {number}" or fields[4] != "*Pending* |":
            raise ValueError(f"task {number}: invalid row or unexpected progress")
        description, *paths = fields[1].split("<br>")
        coder, complexity, deps = fields[2].strip("*").split(" · ")
        checks = []
        for entry in fields[3].split("<br>"):
            command, rest = entry.removeprefix("<code>").split("</code> · cwd <code>", 1)
            cwd, rest = rest.split("</code> · setup <em>", 1)
            setup, expected = rest.removesuffix("</em>").split("</em> · expect <em>", 1)
            checks.append(dict(command=html.unescape(command), cwd=html.unescape(cwd),
                               setup=html.unescape(setup), expected=html.unescape(expected)))
        tasks.append(dict(description=html.unescape(description.strip("*")),
                          files=[html.unescape(path.removeprefix("<code>").removesuffix("</code>")) for path in paths],
                          coder=coder.strip("`"), complexity=complexity,
                          depends_on=[] if deps == "depends on none" else
                          [int(dep) for dep in deps.removeprefix("depends on ").split(", ")],
                          checks=checks))
    return tasks


def plan_header(block, work):
    link = json.dumps(f"[[{block[:-3]}]]", ensure_ascii=False)
    return f"Template: Implementation plan\nBuilding block: {link}\nPlan type: {WORK_LABELS[work]}\n"


def check_plan_context(data, block, work):
    normalized = data.decode("utf-8-sig").replace("\r\n", "\n")
    if not normalized.startswith("---\n" + plan_header(block, work)):
        raise ValueError("plan header does not identify the routed building block and work type")


def render_plan(block, work, goal, tasks, sections, external):
    scope = [f"*[[{block[:-3]}#Specification|Specification and Acceptance]].*"]
    for path in sorted({path for task in tasks for path in task.files}):
        scope.append(f"- *Implementation path {code(path)}.*")
    for path, selectors in sorted(sections.items()):
        for selector in selectors:
            scope.append(f"- *{code(path)} · {cell(' / '.join(selector))}.*")
    for dependency, item in external:
        scope.append(f"- *External dependency {cell(dependency)}. Run {code(item.command)} "
                     f"from {code(item.cwd)}. Setup {cell(item.setup)}. Expected {cell(item.expected)}.*")
    rows = [row(number, task) for number, task in enumerate(tasks, 1)]
    return "\n".join([
        "---", plan_header(block, work).rstrip("\n"), "Status:", "---", "",
        "## Context", "", "### Goal", "", f"*{cell(line(goal))}*", "",
        "### Specification and scope", "", *scope, "", "### Plan status", "", "*Ready*", "",
        "## Tasks", "", "### Execution order and progress", "", TABLE_HEAD, *rows, "",
        "### Task details", "", "## Actions", "",
        "- [ ] *Execute this plan*", "- [ ] *Continue this plan*", "",
        "## Outcome", "", "### Final verification", "", "### Interruption and next step", "",
    ]).encode()


def edited_specification(before, updates, sections):
    result = {}
    seen = set()
    for item in updates:
        path = note_path(item["note"], "Sources")
        selector = tuple(item["section"])
        if selector not in sections.get(path, ()) or (path, selector) in seen:
            raise ValueError(f"unapproved or duplicate Specification section: {path}: {selector}")
        seen.add((path, selector))
        original = result.get(path, before[path])
        old = read_section(original.data, selector, root_level=2)
        if re.search(rb"(?m)^ {0,3}#{1,6}[ 	]+", old):
            raise ValueError(f"select a leaf Specification subsection: {path}: {selector}")
        newline = b"\r\n" if b"\r\n" in original.data else b"\n"
        text = item["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Specification updates require nonempty prose")
        paragraphs = [f"*{cell(line(part))}*".encode() for part in text.splitlines() if part.strip()]
        # Render prose only. The editor retains existing heading anchors.
        content = newline.join(paragraphs) + newline
        if old.startswith(newline):
            content = newline + content
        if old.endswith(newline * 2):
            content += newline
        result[path] = Entry("file", replace_section(original.data, selector, content, root_level=2), original.mode)
    return result


def verify_specification(data, tasks, *, require_task_check=False):
    architecture = read_section(data, ARCHITECTURE, root_level=2).strip()
    acceptance = read_section(data, ACCEPTANCE, root_level=2).strip().decode()
    if not architecture or not acceptance:
        raise ValueError("Architecture and techniques and Acceptance must be populated")
    if require_task_check and not any(
        item.command in html.unescape(acceptance) for task in tasks for item in task.checks
    ):
        raise ValueError("Acceptance must include a concrete verification command from the plan")


def write_changes(root, before, updates):
    """Compare the input state, stage all files, then apply and roll back on write errors."""
    if capture(root) != before:
        raise ValueError("vault changed during planning; rerun with the latest ticket and notes")
    staged = {}
    applied = []
    try:
        for path, entry in updates.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
                staged[path] = Path(temporary.name)
                temporary.write(entry.data)
                os.fchmod(temporary.fileno(), entry.mode)
        for path, temporary in staged.items():
            target = root / path
            if path not in before:
                # A new plan must never displace one created after the input capture.
                os.link(temporary, target)
            else:
                if target.is_symlink() or target.stat().st_nlink != 1 or target.read_bytes() != before[path].data:
                    raise ValueError(f"{path} changed during planning")
                temporary.replace(target)
            applied.append(path)
    except (OSError, ValueError):
        for path in reversed(applied):
            target = root / path
            current = updates[path]
            if target.is_file() and not target.is_symlink() and target.read_bytes() == current.data:
                if path in before:
                    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as restore:
                        restore.write(before[path].data)
                        os.fchmod(restore.fileno(), before[path].mode)
                    Path(restore.name).replace(target)
                else:
                    target.unlink()
        raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def bug_fix_setup_record(selected, block, plan):
    """Load the publication record for a Bug fix plan prepared in this checkout."""
    root = selected.path
    admin = Path(_line(root, "rev-parse", "--absolute-git-dir"))
    marker = admin / "theseus-bug-fix-plan.json"
    if marker.is_symlink() or marker.exists() and not marker.is_file():
        raise ValueError("invalid Bug fix plan setup record")
    if not marker.exists():
        return None
    record = json.loads(marker.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)
    if (not isinstance(record, dict) or set(record) != {"base", "block", "plan", "commit", "sections"}
            or (record["base"], record["block"], record["plan"]) != (selected.base, block, plan)
            or not isinstance(record["commit"], str) or not re.fullmatch(OBJECT_ID, record["commit"])
            or not isinstance(record["sections"], dict)):
        raise ValueError("Bug fix plan setup belongs to another block, plan, or input base")
    commit = record["commit"]
    if _line(root, "rev-list", "--parents", "-1", commit).split() != [commit, selected.base]:
        raise ValueError("Bug fix plan setup is outside the recorded input history")
    selectors = {}
    for path, entries in record["sections"].items():
        note_path(path, "Sources")
        if not isinstance(entries, list) or any(
            not isinstance(item, list) or len(item) < 2 or item[0] != "Specification"
            or any(not isinstance(part, str) or not part.strip() for part in item)
            for item in entries
        ):
            raise ValueError("invalid Specification selectors in the Bug fix plan setup record")
        selectors[path] = [tuple(item) for item in entries]
    record["sections"] = selectors
    return record


@contextmanager
def _bug_fix_setup_lock(admin):
    lock = admin / "theseus-execution.lock"
    with os.fdopen(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "a") as opened:
        fcntl.flock(opened, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _planner_schema(allow, selectors):
    def record(properties):
        return {"type": "object", "properties": properties,
                "required": list(properties), "additionalProperties": False}

    def items(schema):
        return {"type": "array", "items": schema}

    text = {"type": "string"}
    path = {**text, "enum": sorted(allow)} if allow else text
    note = {**text, "enum": sorted(selectors)} if selectors else text
    check_fields = record({field: text for field in ("command", "cwd", "setup", "expected")})
    return record({
        "goal": text,
        "specification": items(record({"note": note, "section": items(text), "text": text})),
        "tasks": items(record({
            "description": text,
            "files": items(path),
            "coder": {**text, "enum": ["coder_light", "coder"]},
            "complexity": {**text, "enum": sorted(COMPLEXITY)},
            "depends_on": items({"type": "integer"}),
            "checks": items(check_fields),
        })),
        "external_dependencies": items(record({"description": text, "check": check_fields})),
    })


def run_planning(root, decision, *, ticket, plan_path=None, files=(), sections=None, settings=None,
                 context="", question=None, discussion=None, local_verification=None, runner=run_role,
                 _refactoring_publish=None):
    """Use an interpreted route and exact authorizations. Return the original ticket's reply.

    Pass the main vault for first implementation or modification and the verified
    prepared worktree for refactoring or a dependent bug fix.
    """
    if not isinstance(decision, Decision):
        raise ValueError("expected an interpreted routing decision")
    if decision.stage in {"clarify", "brainstorm"}:
        pending = question or decision.question
        if pending:
            return Result("await_decision", f"*{line(pending)}*")
        return Result("discussed", f"*{line(discussion)}*")
    if decision.stage == "await_execution":
        return Result("await_execution", f"*The plan [[{decision.plan[:-3]}]] is ready for an execution request.*",
                      decision.plan)
    if decision.stage not in {"local", "plan", "execute"}:
        raise ValueError(f"not a planning stage: {decision.stage}")
    root = Path(root).absolute()
    before = input_state = capture(root)
    block, work = decision.block, decision.work
    rebuild = work == "refactoring" and decision.stage == "plan"
    if rebuild and _refactoring_publish is None:
        raise ValueError("use the confirmed refactoring helper for worktree planning")
    allow = canonical_paths(files)
    if decision.stage == "local":
        command = line(local_verification)
        if not allow:
            raise ValueError("specify exact paths for the local change")
        authorize(before, before, block, role="obstagent", stage="local", work=work, files=files)
        listed = ", ".join(f"`{path}`" for path in sorted(allow))
        return Result("local", f"*Local change scoped to {listed}. Verify with `{command}`.*")
    authorize(before, before, block, role="coder", stage="execute", work=work, files=files)
    path = note_path(decision.plan or plan_path, "Plans")
    if decision.plan and plan_path is not None and path != plan_path:
        raise ValueError("plan path differs from the routed plan")
    bug_fix_setup = work == "bug_fix" and decision.stage == "plan"
    if bug_fix_setup:
        vault, identity, selected = owned_checkout(root)
        admin = Path(_line(root, "rev-parse", "--absolute-git-dir"))
        with _bug_fix_setup_lock(admin):
            _clean(root)
            head = _line(root, "rev-parse", "HEAD")
            if record := bug_fix_setup_record(selected, block, path):
                commit = record["commit"]
                if head == selected.base:
                    _publish_commit(root, selected.base, commit)
                    head = commit
                if head == commit:
                    result = run_planning(root, replace(decision, stage="execute"), ticket=ticket, plan_path=path,
                                          files=files)
                    if decision.execute_after_plan:
                        return result
                    return replace(result, stage="await_execution",
                                   response=f"*The Bug fix plan [[{path[:-3]}]] is ready for execution.*")
                if _git(root, "merge-base", "--is-ancestor", commit, head, check=False).returncode:
                    raise ValueError("Bug fix plan setup is outside the current worktree history")
                return Result("resume", f"*Continue the recorded Bug fix plan [[{path[:-3]}]].*", path)
            if head != selected.base or path in before:
                raise ValueError("Bug fix planning needs a new plan at the prepared input base")
    selectors = {note: [tuple(item) for item in selected] for note, selected in (sections or {}).items()}
    if any(not selector or selector[0] != "Specification" or len(selector) < 2
           for selected in selectors.values() for selector in selected):
        raise ValueError("planning requires precise Specification subsections")
    authorize(before, before, block, role="planner", stage="plan", work=work, plan=path, sections=selectors)
    if rebuild:
        if set(selectors) != {block} or set(selectors[block]) != {ARCHITECTURE, ACCEPTANCE}:
            raise ValueError("refactoring planning requires the standard Specification subsections")
        entry = before[block]
        old = read_section(entry.data, ("Specification",), root_level=2)
        newline = b"\r\n" if b"\r\n" in entry.data else b"\n"
        body = newline.join((b"", b"### Architecture and techniques", b"", b"",
                             b"### Acceptance", b"", b""))
        if old:
            body += b"### Actions" + newline * 2 + old
        scaffold = replace_section(entry.data, ("Specification",), body, root_level=2)
        before = {**before, block: Entry("file", scaffold, entry.mode)}
    existing = before.get(path)
    if existing is not None:
        if existing.kind != "file":
            raise ValueError(f"not a regular plan file: {path}")
        check_plan_context(existing.data, block, work)
    if decision.stage == "execute":
        if existing is None or read_section(existing.data, STATUS, root_level=2).strip() != b"*Ready*":
            raise ValueError("the selected plan must be Ready")
        tasks = validated_tasks(parse_rows(existing.data), allow, before)
        verify_specification(before[block].data, tasks, require_task_check=work == "first")
        return Result("execute", f"*Execute the validated plan [[{path[:-3]}]].*", path, tasks)
    if existing is not None and read_section(existing.data, STATUS, root_level=2).strip() != b"*Draft*":
        raise ValueError("only a Draft plan can be updated")

    planner_input = json.dumps({
        "ticket": ticket, "context": context, "block": block, "work": work, "plan": path,
        "approved_implementation_files": sorted(allow), "approved_specification_sections": selectors,
        **({"refactoring": "Preserve the User requirements and descendant contracts. Plan a fresh "
                            "implementation using the specified heading paths for the new Specification."}
           if rebuild else {}),
    }, ensure_ascii=False)
    options = {}
    if runner is run_role or getattr(runner, "accepts_response_schema", False) is True:
        options["response_schema"] = _planner_schema(allow, selectors)
    response = runner(
        "planner", settings, cwd=root, allowed_paths=[], allowed_sections={}, checks=[], read_only=True,
        context=planner_input,
        task=("Return the proposal in response as a JSON object with goal, specification, tasks and "
              "external_dependencies. Use a nonempty single line for the goal, each task description, "
              "and each command, setup and expected result. Read the block requirements and relevant contexts. "
              "Each specification "
              "item has note, section (heading array), text (plain prose, one paragraph per line); "
              "update only approved sections. First implementation requires Architecture and techniques and "
              "Acceptance, linking checks to observable requirements. Each ordered task has description, exact approved files, "
              "coder, complexity (specified for coder_light; design, contract, coordination or end_to_end "
              "for coder), depends_on (earlier 1-based task numbers), and checks (each contains command, cwd "
              "relative to vault or '.', setup and expected). Each external dependency has description and "
              "check in the same check format. If a material decision is open return outcome error and the "
              "precise question in response. Leave file writing to the caller."),
        **options,
    )
    if response.outcome != "success":
        return Result("await_decision", f"*{line(response.response.strip().strip('*'))}*")
    if capture(root) != input_state:
        raise ValueError("vault changed during planning; rerun with the latest ticket and notes")
    proposal = json.loads(response.response, object_pairs_hook=unique_fields)
    tasks = validated_tasks(proposal["tasks"], allow, before)
    external = [(line(item["description"]), check(item["check"], before, allow))
                for item in proposal["external_dependencies"]]
    spec = proposal["specification"]
    if work == "first":
        changed = {tuple(item["section"]) for item in spec if item["note"] == block}
        if not {ARCHITECTURE, ACCEPTANCE} <= changed:
            raise ValueError("First implementation requires Architecture and techniques and Acceptance")
    updates = edited_specification(before, spec, selectors)
    verify_specification(updates.get(block, before[block]).data, tasks, require_task_check=work == "first")
    planned = render_plan(block, work, proposal["goal"], tasks, selectors, external)
    if existing is not None:
        # Only replace untouched template-stub bodies. Human additions and the user Status survive.
        body_selectors = (("Context", "Goal"), ("Context", "Specification and scope"), STATUS, TASKS)
        template_stub = (
            b"*Record the observable result.*",
            b"*Identify the Specification and Acceptance, exact allowed files and sections, affected descendants, exclusions and external dependencies.*",
            b"*Draft*",
            (TABLE_HEAD + "\n| 1 | *Describe the smallest independently checked change.* | "
             "*Choose `coder` or `coder_light`.* | *Record the command, working directory, setup and expected output.* | *Pending* |").encode(),
        )
        data = existing.data
        for selector, stub in zip(body_selectors, template_stub):
            old = read_section(data, selector, root_level=2).replace(b"\r\n", b"\n").strip()
            if old not in {b"", stub}:
                raise ValueError(f"draft has authored content in {' / '.join(selector)}; choose a new plan path")
            body = read_section(planned, selector, root_level=2)
            if b"\r\n" in data:
                body = body.replace(b"\n", b"\r\n")
            data = replace_section(data, selector, body, root_level=2)
        changed_sections(existing.data, data, body_selectors, root_level=2)
        planned = data
    # Check the serialized handoff as well as the original structured proposal.
    if validated_tasks(parse_rows(planned), allow, before) != tasks:
        raise ValueError("task table cannot be read back without losing information")
    updates[path] = Entry("file", planned, existing.mode if existing else 0o644)
    authorize(before, {**before, **updates}, block, role="planner", stage="plan", work=work,
              plan=path, sections=selectors)
    if rebuild:
        authorize(input_state, {**input_state, **updates}, block, role="planner", stage="plan", work=work,
                  plan=path, sections={block: [("Specification",)]})
        _refactoring_publish(input_state, updates)
    elif bug_fix_setup:
        with _bug_fix_setup_lock(admin):
            if (verify_worktree(vault, **identity) != selected or capture(root) != input_state
                    or _line(root, "rev-parse", "HEAD") != selected.base
                    or bug_fix_setup_record(selected, block, path) is not None):
                raise ValueError("worktree ownership, files or setup changed during Bug fix planning")
            _clean(root)
            commit = _commit_files(root, admin, selected.base, updates, BUG_FIX_PLAN_SUBJECT)
            _save_record(admin / "theseus-bug-fix-plan.json",
                         dict(base=selected.base, block=block, plan=path, commit=commit, sections=selectors))
            _publish_commit(root, selected.base, commit)
    else:
        write_changes(root, before, updates)
    stage = "execute" if decision.execute_after_plan else "await_execution"
    verb = "Execute" if decision.execute_after_plan else "Ready for execution"
    return Result(stage, f"*{verb}. [[{path[:-3]}]] records {len(tasks)} tasks and their checks.*", path, tasks)


def main():
    try:
        request = json.load(sys.stdin, object_pairs_hook=unique_fields)
        if "_refactoring_publish" in request:
            raise ValueError("use the confirmed refactoring helper for worktree planning")
        request["decision"] = Decision(**request["decision"])
        result = run_planning(**request)
    except (OSError, TypeError, ValueError, KeyError, RoleExecutionError, subprocess.SubprocessError) as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
        print(f"planning: {detail}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(result), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
