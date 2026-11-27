"""Route interpreted ticket intent. This module neither resolves nor authorizes writes."""

import json
import sys
from dataclasses import asdict, dataclass
from typing import Optional, Sequence


WORK_TYPES = {"first", "modification", "refactoring", "bug_fix"}
ACTIONS = {"auto", "discuss", "plan", "execute", "continue"}
RESUMABLE = {"in_progress", "interrupted", "blocked"}
PLAN_STATES = {"draft", "ready", "done", "rejected"} | RESUMABLE
PROTECTED = {"Sources", "Plans"}
PROTECTED_CASEFOLDED = {name.casefold() for name in PROTECTED}


@dataclass(frozen=True)
class Plan:
    path: str
    block: str
    work: str
    status: str


@dataclass(frozen=True)
class LocalChange:
    paths: Sequence[str]
    verification: str
    independent: bool
    single_block: bool


@dataclass(frozen=True)
class Decision:
    stage: str
    block: Optional[str] = None
    work: Optional[str] = None
    plan: Optional[str] = None
    execute_after_plan: bool = False
    question: Optional[str] = None


def canonical_path(path):
    if not isinstance(path, str) or path.strip() != path:
        raise ValueError("expected a canonical vault-relative path")
    parts = path.split("/")
    if (
        any(part in {"", ".", ".."} or part.strip() != part for part in parts)
        or "\\" in path
        or ":" in parts[0]
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or (parts[0].casefold() in PROTECTED_CASEFOLDED and parts[0] not in PROTECTED)
    ):
        raise ValueError(f"expected a canonical vault-relative path: {path!r}")
    return path


def canonical_paths(paths):
    if not isinstance(paths, (tuple, list)):
        raise ValueError("expected an array of canonical vault-relative paths")
    return {canonical_path(path) for path in paths}


def note_path(path, directory):
    if not canonical_path(path).startswith(directory + "/") or not path.endswith(".md"):
        raise ValueError(f"expected a Markdown note path under {directory}: {path!r}")
    return path


def require_boolean(value, name):
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")


def validate_plan(plan):
    if not isinstance(plan, Plan):
        raise ValueError("expected verified plan context")
    note_path(plan.path, "Plans")
    note_path(plan.block, "Sources")
    if (not isinstance(plan.work, str) or plan.work not in WORK_TYPES
            or not isinstance(plan.status, str) or plan.status not in PLAN_STATES):
        raise ValueError("unknown plan work type or status")


def local_paths(local):
    if not isinstance(local, LocalChange) or not isinstance(local.verification, str):
        raise ValueError("expected local change paths and a verification command")
    require_boolean(local.independent, "local.independent")
    require_boolean(local.single_block, "local.single_block")
    paths = canonical_paths(local.paths)
    if local.independent and local.single_block and local.verification.strip():
        return paths
    return set()


def route(ticket_path, *, write_targets=(), block_candidates=(), selected_block=None, work=None,
          action="auto", plan=None, requirements_ready=False, diagnosis_confirmed=False,
          execute_after_plan=False, local=None, refactor_confirmed=False,
          refactor_workspace_ready=False):
    """Select a stage from interpreted intent and independently verified vault context."""
    ticket_path = canonical_path(ticket_path)
    targets = canonical_paths(write_targets)
    candidates = {note_path(path, "Sources") for path in canonical_paths(block_candidates)}
    if selected_block is not None:
        note_path(selected_block, "Sources")
    if ((work is not None and (not isinstance(work, str) or work not in WORK_TYPES))
            or not isinstance(action, str) or action not in ACTIONS):
        raise ValueError("unknown work type or action")
    for name, flag in (
        ("requirements_ready", requirements_ready), ("diagnosis_confirmed", diagnosis_confirmed),
        ("execute_after_plan", execute_after_plan), ("refactor_confirmed", refactor_confirmed),
        ("refactor_workspace_ready", refactor_workspace_ready),
    ):
        require_boolean(flag, name)
    paths = local_paths(local) if local is not None else set()
    if plan is not None:
        validate_plan(plan)

    protected = {path for path in targets if path.split("/", 1)[0] in PROTECTED}
    if ticket_path.split("/", 1)[0] not in PROTECTED and not protected:
        return Decision("ordinary")

    if plan is not None:
        if ticket_path.startswith("Plans/") and ticket_path != plan.path:
            return Decision("clarify", question="Which plan should this ticket use?")
        if work is not None and work != plan.work:
            return Decision("clarify", question="Does the requested work type supersede the recorded plan?")
        candidates.add(plan.block)
        work = plan.work
    if selected_block is None and len(candidates) == 1:
        selected_block = next(iter(candidates))
    if selected_block not in candidates:
        return Decision("clarify", question="Which verified building-block note is in scope?")
    block = selected_block
    if plan is not None and block != plan.block:
        return Decision("clarify", question="Which building block should the selected plan target?")

    def result(stage, question=None, execute_next=False):
        return Decision(stage, block, work, plan.path if plan else None, execute_next, question)

    if action == "discuss":
        return result("brainstorm")
    if work is None:
        return result("clarify", "Which type of block development or stage is requested?")
    if work == "refactoring" and not refactor_confirmed:
        return result("clarify", "Is the explicitly requested refactoring and its worktree cleanup confirmed?")

    if action in {"execute", "continue"}:
        statuses = {"ready"} if action == "execute" else RESUMABLE
        if plan is None or plan.status not in statuses:
            question = ("Which verified, ready plan should be executed?" if action == "execute"
                        else "Which in-progress, interrupted or blocked plan should be resumed?")
            return result("clarify", question)
        if not ticket_path.startswith("Plans/") and plan.path not in targets:
            return result("clarify", "Is updating the verified plan authorized for this ticket?")
        if work == "refactoring" and not refactor_workspace_ready:
            return result("clarify", "Where is the prepared worktree for the confirmed refactoring?")
        return result("execute" if action == "execute" else "resume")

    if plan is not None and plan.status != "draft":
        if action == "auto" and plan.status == "ready":
            return result("await_execution")
        return result("clarify", "Should the existing plan be continued or replaced by a new plan?")
    if work == "bug_fix" and not diagnosis_confirmed:
        return result("diagnose")
    if not requirements_ready:
        if action == "plan" or execute_after_plan:
            return result("clarify", "Which missing requirements must be resolved before planning?")
        return result("brainstorm")
    if work == "refactoring" and not refactor_workspace_ready:
        return result("prepare_refactoring", execute_next=execute_after_plan)
    if (
        action == "auto" and not execute_after_plan and plan is None
        and work in {"modification", "bug_fix"}
        and paths and paths == targets and all(path.startswith("Sources/") for path in paths)
        and not (paths & candidates)
    ):
        return result("local")
    return result("plan", execute_next=execute_after_plan)


def unique_fields(pairs):
    fields = {}
    for name, value in pairs:
        if name in fields:
            raise ValueError(f"duplicate input field: {name}")
        fields[name] = value
    return fields


def main():
    try:
        arguments = json.load(sys.stdin, object_pairs_hook=unique_fields)
        if not isinstance(arguments, dict):
            raise ValueError("expected a JSON object")
        if arguments.get("plan") is not None:
            arguments["plan"] = Plan(**arguments["plan"])
        if arguments.get("local") is not None:
            arguments["local"] = LocalChange(**arguments["local"])
        decision = route(**arguments)
    except (ValueError, TypeError) as exc:
        print(f"routing: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(decision), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
