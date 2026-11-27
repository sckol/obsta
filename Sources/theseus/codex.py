"""Run a scoped Theseus role in a fresh Codex CLI process."""

import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath


EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
CODER_INSTRUCTIONS = (
    "Implement the selected task. For new behavior, reproduce the failure before changing code. "
    "Run the required checks and report evidence, changed files and any authorized commit."
)
ROLE_INSTRUCTIONS = {
    "orchestrator": "Coordinate the selected plan. Record progress and check evidence in the permitted sections.",
    "planner": "Prepare the permitted Specification and plan changes. Assign each task coder or coder_light.",
    "coder_light": CODER_INSTRUCTIONS,
    "coder": CODER_INSTRUCTIONS,
    "coder_heavy": CODER_INSTRUCTIONS,
    "reviewer": "Review read-only against the supplied requirements and actual diff. Report findings with evidence.",
    "evaluator": "Compare the specified versions against the same obligations. Report the evidence and uncertainties.",
}
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["success", "error"]},
        "response": {"type": "string"},
    },
    "required": ["outcome", "response"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class RoleResult:
    outcome: str
    response: str


class RoleExecutionError(RuntimeError):
    """Codex did not return a valid, completed role result."""


def unique_fields(pairs):
    fields = {}
    for name, value in pairs:
        if name in fields:
            raise ValueError(f"duplicate JSON field: {name}")
        fields[name] = value
    return fields


def strings(values, field):
    if not isinstance(values, (list, tuple)) or any(
        not isinstance(value, str) or not value.strip() for value in values
    ):
        raise ValueError(f"{field} must be an array of nonempty strings")
    return list(values)


def scope_path(name):
    path = PurePosixPath(name)
    if (not path.parts or path.is_absolute() or ".." in path.parts or path.as_posix() != name
            or "\\" in name or any(ord(character) < 32 for character in name)):
        raise ValueError(f"expected a normalized path relative to cwd: {name!r}")


def role_prompt(role, task, context, allowed_paths, allowed_sections, checks, *, structured_response=False):
    if not isinstance(task, str) or not task.strip() or not isinstance(context, str):
        raise ValueError("task must be nonempty text and context must be text")
    paths = strings(allowed_paths, "allowed_paths")
    for path in paths:
        scope_path(path)
    if not isinstance(allowed_sections, Mapping):
        raise ValueError("allowed_sections must map paths to arrays of section headings")
    sections = {}
    for path, headings in allowed_sections.items():
        if not isinstance(path, str):
            raise ValueError("allowed_sections keys must be paths")
        scope_path(path)
        sections[path] = strings(headings, "section headings")
    brief = {
        "role": role, "task": task, "context": context,
        "allowed_paths": paths, "allowed_sections": sections, "checks": strings(checks, "checks"),
    }
    response_instructions = (
        "On success put the report object directly in response. "
        "On error put a nonempty plain-text explanation in response."
        if structured_response else
        "Provide a nonempty response including any question needed."
    )
    return (
        f"{ROLE_INSTRUCTIONS[role]}\n"
        "Use this brief's task and selected context. The allowed paths and note sections are "
        "the complete write authorization. Paths are relative to the working directory. "
        "Preserve User requirements, historical discussions, ticket markers and service lines. "
        "Use italic Markdown for new prose in writable note sections. Report the checks performed, "
        "their results and any blockers. Return a JSON object with exactly outcome and response. "
        "Use outcome success when the role's required result and acceptance conditions are met and "
        "error for blocked or incomplete work. "
        + response_instructions + "\n\n" + json.dumps(brief, ensure_ascii=False)
    )


def final_result(path, *, structured_response=False):
    try:
        result = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)
        if isinstance(result, dict) and set(result) == {"outcome", "response"}:
            outcome, response = result["outcome"], result["response"]
            if outcome == "success" and structured_response:
                if isinstance(response, dict):
                    return RoleResult(outcome, json.dumps(response, ensure_ascii=False))
            elif outcome in ("success", "error") and isinstance(response, str) and response.strip():
                return RoleResult(outcome, response)
    except (OSError, ValueError):
        pass
    raise RoleExecutionError("Codex returned an invalid final result")


def run_role(role, settings, *, cwd, task, context, allowed_paths, allowed_sections, checks, timeout=1800,
             read_only=False, response_schema=None):
    """Run once using effective obstagent settings and caller-verified scope.

    Return the validated role result. Raise ValueError for an invalid request or
    RoleExecutionError (including on interruption) if the process fails.
    The caller checks actual worktree changes and role-specific evidence.
    """
    if not isinstance(role, str) or role not in ROLE_INSTRUCTIONS:
        raise ValueError(f"unknown Theseus role: {role!r}")
    key = f"theseus.{role}"
    value = settings.get(key) if isinstance(settings, Mapping) else None
    if not isinstance(value, str):
        raise ValueError(f"set {key} to model/effort")
    model, _, effort = value.partition("/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model) or effort not in EFFORTS:
        raise ValueError(f"invalid {key}, expected model/effort")
    directory = Path(cwd)
    if not directory.is_absolute() or not directory.is_dir():
        raise ValueError(f"cwd must be an existing absolute directory: {cwd!r}")
    directory = directory.resolve(strict=True)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout must be a positive finite number of seconds")
    if not isinstance(read_only, bool):
        raise ValueError("read_only must be a boolean")
    if read_only and (allowed_paths or allowed_sections):
        raise ValueError("a read-only brief cannot authorize writes")
    schema_data = RESULT_SCHEMA
    if response_schema is not None:
        if not isinstance(response_schema, dict) or response_schema.get("type") != "object":
            raise ValueError("response_schema must specify a structured object")
        schema_data = {**RESULT_SCHEMA, "properties": {**RESULT_SCHEMA["properties"],
                       "response": {"anyOf": [response_schema, {"type": "string"}]}}}
    prompt = role_prompt(role, task, context, allowed_paths, allowed_sections, checks,
                         structured_response=response_schema is not None)
    try:
        with tempfile.TemporaryDirectory(prefix="theseus-codex-") as temporary:
            schema = Path(temporary) / "schema.json"
            result = Path(temporary) / "result.json"
            schema.write_text(json.dumps(schema_data), encoding="utf-8")
            args = [
                "codex", "exec", "--ephemeral", "--skip-git-repo-check",
                "-C", str(directory), "-m", model,
                "-c", f'model_reasoning_effort="{effort}"', "-c", 'approval_policy="never"',
            ]
            if read_only:
                args.extend(("--sandbox", "read-only"))
            args.extend(("--output-schema", str(schema), "--output-last-message", str(result), "-"))
            with tempfile.TemporaryFile() as brief, tempfile.TemporaryFile() as errors:
                brief.write(prompt.encode("utf-8"))
                brief.seek(0)
                try:
                    child = subprocess.Popen(
                        args, cwd=directory, stdin=brief, stdout=subprocess.DEVNULL,
                        stderr=errors, start_new_session=True,
                    )
                except OSError as exc:
                    raise RoleExecutionError(f"could not launch Codex for {role}: {exc}") from exc
                try:
                    code = child.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    raise RoleExecutionError(f"Codex {role} timed out after {timeout:g} seconds") from None
                finally:
                    # The process group can still contain tool processes after Codex exits.
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
                if code:
                    errors.seek(max(0, errors.seek(0, os.SEEK_END) - 2000))
                    detail = errors.read().decode("utf-8", errors="replace").strip()
                    raise RoleExecutionError(f"Codex {role} exit {code}" + (f": {detail}" if detail else ""))
            return final_result(result, structured_response=response_schema is not None)
    except KeyboardInterrupt:
        raise RoleExecutionError(f"Codex {role} interrupted") from None


def main():
    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        request = json.load(sys.stdin, object_pairs_hook=unique_fields)
        if not isinstance(request, dict):
            raise ValueError("expected a JSON request object")
        result = run_role(**request)
    except (TypeError, ValueError, OSError, RoleExecutionError) as exc:
        print(json.dumps(asdict(RoleResult("error", str(exc))), ensure_ascii=False))
        return 2
    except KeyboardInterrupt:
        print(json.dumps(asdict(RoleResult("error", "Codex adapter interrupted"))))
        return 2
    print(json.dumps(asdict(result), ensure_ascii=False))
    return 0 if result.outcome == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
