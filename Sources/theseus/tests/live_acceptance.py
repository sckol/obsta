"""Run the installed Theseus acceptance probes in a disposable Git vault."""

import argparse
import concurrent.futures
import hashlib
import http.server
import json
import os
import queue
import runpy
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.request
from contextlib import contextmanager
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1]
PROJECT = SOURCE.parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures/vault"
PYTHON = sys.executable
HISTORY = "refs/heads/obsta-history"
BLOCK = "Sources/file-block.py.md"
PLAN = "Plans/live-plan.md"
INSTRUCTION = "Conventions/Agent Instruction.md"
ROLES = {
    "orchestrator": ("gpt-6.2-sol", "medium"), "planner": ("gpt-6.2-sol", "high"),
    "coder_light": ("gpt-6-luna", "medium"), "coder": ("gpt-6.2-sol", "medium"),
    "coder_heavy": ("gpt-6.2-sol", "high"), "reviewer": ("gpt-6.2-sol", "high"),
    "evaluator": ("gpt-6.2-sol", "medium"),
}
SETUP = """Execute the selected JSON ticket on stdin in this disposable fixture vault.
Use only fixture paths under the present vault and the installed Theseus paths
authorized by the surrounding developer instructions. Return only JSON with
exactly two string fields, as in {"outcome":"success","response":"completed"}. Put
the final answer inside response. Preserve ticket markers in files and never copy their
opening or closing markers into your response. For a route probe, report the
route in your response. For an ordinary route probe, report "ordinary" followed by the proof token.
For a protected route probe, follow the entry skill and actually run its
installed router. For a live workflow, follow the entry skill and perform the
requested scoped change and verification. Include the current developer proof
token in the JSON response without writing it to any files yourself. Keep this
proof in the supplied developer instructions. Do not open Agent Instruction.md to
retrieve the proof. The approved fixture commands may read fixture files for validation.
Current developer proof token:
"""

def isolated_env(area, *, writable_root=None):
    """Keep fixture sessions isolated while using the configured model transport."""
    env = {key: val for key, val in os.environ.items() if not key.startswith("GIT_")}
    original = Path(env.get("CODEX_HOME", Path.home() / ".codex"))
    auth = original / "auth.json"
    home = area / "codex"
    home.mkdir(mode=0o700)
    if auth.is_file():
        shutil.copyfile(auth, home / "auth.json")
        (home / "auth.json").chmod(0o600)
    config = ('approval_policy = "never"\ndefault_permissions = "theseus_fixture"\n'
              'cli_auth_credentials_store = "file"\n')
    provider_table = ""
    if (original / "config.toml").is_file():
        with (original / "config.toml").open("rb") as file:
            settings = tomllib.load(file)
        name = settings.get("model_provider")
        provider = settings.get("model_providers", {}).get(name)
        if isinstance(name, str) and isinstance(provider, dict):
            key = json.dumps(name, ensure_ascii=False)
            config += f"model_provider = {key}\n"
            provider_table = f"\n[model_providers.{key}]\n"
            for field in ("base_url", "env_key", "name", "supports_websockets", "wire_api"):
                value = provider.get(field)
                if isinstance(value, (str, bool)):
                    provider_table += f"{field} = {json.dumps(value, ensure_ascii=False)}\n"
    # The named permission profile allows Git administration in the disposable
    # fixture root without making other workspaces writable.
    writable = json.dumps(str(writable_root or area), ensure_ascii=False)
    config += ('[features]\nplugins = false\nremote_plugin = false\n'
               '[permissions.theseus_fixture.filesystem]\n"/" = "read"\n'
               f'{writable} = "write"\n[permissions.theseus_fixture.network]\nenabled = true\n')
    (home / "config.toml").write_text(config + provider_table, encoding="utf-8")
    return dict(env, CODEX_HOME=str(home), PYTHONDONTWRITEBYTECODE="1",
                GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                GIT_TERMINAL_PROMPT="0")


def require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


def run(args, cwd, env, timeout, *, stdin=None, check=True):
    result = subprocess.run(args, cwd=cwd, env=env, input=stdin or "", text=True,
                            capture_output=True, timeout=timeout)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()[-2500:]
        raise RuntimeError(f"{Path(args[0]).name} exited {result.returncode}: {detail}")
    return result


def obj(line):
    value = json.loads(line)
    require(isinstance(value, dict), "expected a JSON object")
    return value


def tool_documents(item):
    def documents(value):
        if isinstance(value, dict):
            yield value
            for field in ("output", "stdout", "text", "content", "structuredContent"):
                yield from documents(value.get(field))
        elif isinstance(value, list):
            for block in value:
                yield from documents(block)
        elif isinstance(value, str):
            try:
                decoded = json.loads(value)
            except ValueError:
                for line in value.splitlines():
                    try:
                        yield from documents(json.loads(line))
                    except ValueError:
                        continue
            else:
                if isinstance(decoded, (dict, list)):
                    yield from documents(decoded)

    yield from documents(item.get("output"))


def nonce():
    return "LIVE-" + secrets.token_hex(10)


def write(root, path, data):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(data, encoding="utf-8")


class Fixture:
    def __init__(self, area, watcher, timeout, *, boundaries=None):
        self.area, self.timeout = area, timeout
        self.env = isolated_env(area)
        self.install = area / "installed suite"
        self.install.mkdir()
        self.watcher = self.install / "obstagent.py"
        shutil.copyfile(watcher, self.watcher)
        self.boundaries = boundaries or runpy.run_path(str(self.watcher))["boundaries"]
        self.milestones = {}
        self.bundle = self.install / "theseus"
        self.scripts = self.bundle / "skills/using-theseus/scripts"
        self.vault = area / "fixture vault"
        shutil.copytree(FIXTURE, self.vault)
        write(self.vault, ".gitignore", "/.worktrees/\n/.obsta-experiments/\n")
        write(self.vault, "Configs/obsta.md", '`nickname` = LIV\n`subagent` = codex\n' + "".join(
            f"`theseus.{role}` = `{model}/{effort}`\n" for role, (model, effort) in ROLES.items()
        ))
        write(self.vault, INSTRUCTION, SETUP)
        write(self.vault, "Ordinary.md", "# Ordinary fixture\n\nKeep this line.\n")
        write(self.vault, PLAN, "# Plan fixture\n\n[[Sources/file-block.py]]\n")
        write(self.vault, "outside.txt", "committed\n")
        self.admin = area / "vault-admin"
        self.git("init", "-q", "--separate-git-dir", str(self.admin))
        self.git("config", "user.name", "Theseus live acceptance")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("add", "-A")
        self.git("commit", "-qm", "Artificial acceptance fixture")
        write(self.vault, "outside.txt", "staged\n")
        self.git("add", "outside.txt")
        write(self.vault, "outside.txt", "unstaged\n")
        self.main = self.main_state()

    def command(self, args, *, cwd=None, **options):
        return run(args, cwd or self.area, self.env, self.timeout, **options)

    def git(self, *args, check=True):
        return self.command(["git", *args], cwd=self.vault, check=check).stdout

    def main_state(self):
        return (self.git("rev-parse", "HEAD").strip(),
                (self.admin / "index").read_bytes(),
                (self.vault / "outside.txt").read_bytes())

    def build(self):
        lock = obj((SOURCE / "upstream.lock").read_text(encoding="utf-8"))
        archive = PROJECT / "References" / f"superpowers-{lock['commit']}.tar.gz"
        args = [PYTHON, str(SOURCE / "build.py"), "--archive", str(archive), "--output"]
        self.command(args + [str(self.bundle)])
        second = self.area / "second install"
        self.command(args + [str(second)])
        manifest = (self.bundle / "manifest.json").read_bytes()
        require(manifest == (second / "manifest.json").read_bytes(), "different bundle manifests")
        bad = self.area / "corrupt archive"
        bad.write_bytes(b"invalid upstream")
        failure = self.command(args[:3] + [str(bad), "--output", str(self.bundle)], check=False)
        require(failure.returncode and manifest == (self.bundle / "manifest.json").read_bytes(),
                "failed archive verification changed the active installation")
        require(not (self.vault / "Sources/theseus").exists(), "bundle leaked into the vault")
        return {"upstream": lock["commit"], "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                "skills": len(list((self.bundle / "skills").glob("*/SKILL.md"))),
                "failed_archive_preserved_install": True}

    def records(self):
        fields = self.git("log", "--format=%H%x00%b%x00", HISTORY, check=False).split("\0")
        return [{**obj(fields[i + 1]), "oid": fields[i].strip()} for i in range(0, len(fields) - 1, 2)]

    def rollout(self, session):
        files = list((Path(self.env["CODEX_HOME"]) / "sessions").rglob(f"*{session}.jsonl"))
        require(len(files) == 1, f"expected one isolated rollout for {session}, found {len(files)}")
        return [obj(line) for line in files[0].read_text(encoding="utf-8").splitlines()]

    def watch(self):
        return self.command([PYTHON, str(self.watcher), "--vault", str(self.vault),
                             "--once", "--settle", "0", "--interval", "0.01",
                             "--timeout", str(max(1, self.timeout - 5))], cwd=self.install).stderr

    def activate(self, note, comment, *, expected, placement=None, stage=None, proof=None):
        proof = proof or nonce()
        write(self.vault, INSTRUCTION, SETUP + proof + "\n")
        current = (self.vault / note).read_text(encoding="utf-8")
        signal = f"\n\n**{comment}**\n"
        if placement:
            before, marker = placement
            ticket = marker[1:-1]
            matches = [boundary for boundary in self.boundaries(current) if boundary[0] == ticket]
            require(len(matches) == 1, f"ambiguous insertion point: {marker}")
            at = matches[0][3 if before else 2]
            signal = current[:at] + signal + current[at:]
        else:
            signal = current + signal
        require(proof not in comment, "proof leaked into the request")
        count = len(self.records())
        write(self.vault, note, signal)
        log = self.watch()
        records = self.records()
        require(len(records) == count + 2, f"expected one A/B pair: {log[-1700:]}")
        b, a = records[:2]
        require(a["phase"] == "A" and b["phase"] == "B" and b["snapshot_a"] == a["oid"]
                and a["activation_id"] == b["activation_id"] and b["comment"] == comment,
                f"mismatched snapshots for {note}")
        require(b["outcome"] == "success", f"{note}: {b.get('result')}")
        require(proof in b["result"] and expected.casefold() in b["result"].casefold(),
                f"expected response with {proof} and {expected}: {b['result']}")
        earlier = self.git("show", f"{a['oid']}:{note}")
        later = self.git("show", f"{b['oid']}:{note}")
        require(later.count(proof) > earlier.count(proof), "snapshot B lacks the new ticket answer")
        rollout = self.rollout(b["session_id"])
        payload = [event.get("payload", {}) for event in rollout if event.get("type") == "response_item"]
        user = [i for i, item in enumerate(payload) if item.get("role") == "user"
                and a["oid"] in json.dumps(item) and b["activation_id"] in json.dumps(item)]
        require(len(user) == 1, "Codex rollout lacks the separate watcher ticket JSON")
        dev = [item for item in payload[:user[0]] if item.get("role") == "developer"]
        if b.get("fork_parent_session_id"):
            # Fork rollouts persist the new turn. Original developer history is
            # linked by the watcher-recorded fork parent.
            dev.extend(event.get("payload", {}) for event in self.rollout(b["fork_parent_session_id"])
                       if event.get("type") == "response_item"
                       and event.get("payload", {}).get("role") == "developer")
        entry = self.bundle / "skills/using-theseus/SKILL.md"
        require(any(proof in json.dumps(item) and str(entry) in json.dumps(item) for item in dev),
                "current developer instruction and installed entry are missing from the Codex rollout")
        payload = payload[user[0] + 1:]
        calls = [item for item in payload if item.get("type") in {"function_call", "custom_tool_call"}]
        router = [item for item in calls if "routing.py" in json.dumps(item)]
        if stage:
            require(router, "no installed router tool call in real Codex history")
            outputs = [item for item in payload
                       if item.get("type") in {"function_call_output", "custom_tool_call_output"}
                       and item.get("call_id") in {call.get("call_id") for call in router}]
            require(any(document.get("stage") == stage for item in outputs for document in tool_documents(item)),
                    f"no observed router response for {stage}")
        elif expected == "ordinary":
            require(not router, "ordinary ticket called the Theseus router")
        require(self.main == self.main_state(), "watcher changed the main HEAD, index or dirty file")
        return {k: b[k] for k in ("ticket_id", "activation_id", "session_id", "fork_parent_session_id", "snapshot_a", "oid", "result")
                if k in b}

    def lifecycle(self):
        proof = nonce()
        parent = self.activate(BLOCK, "Route probe. Brainstorm what change might be worthwhile in this file block. Discuss only.",
                               expected="brainstorm", stage="brainstorm", proof=proof)
        self.milestones["lifecycle"] = {"new": parent}
        close = f"#{parent['ticket_id']}🛑"
        resume = self.activate(BLOCK, "Route probe. Continue discussing the same file block. Run the installed "
                               "router for this activation, even if you previously routed the ticket.",
                               expected="brainstorm", stage="brainstorm", placement=(True, close), proof=proof)
        self.milestones["lifecycle"]["resume"] = resume
        fork = self.activate(BLOCK, "Route probe. Brainstorm a separate option for this file block. Run the "
                             "installed router for this activation and discuss only.",
                             expected="brainstorm", stage="brainstorm",
                             placement=(False, f"#{parent['ticket_id']}🍏"), proof=proof)
        require(resume["session_id"] == parent["session_id"] and fork["session_id"] != parent["session_id"]
                and fork.get("fork_parent_session_id") == parent["session_id"]
                and fork["ticket_id"].startswith(parent["ticket_id"] + "/"), "resume or fork identity is wrong")
        return {"new": parent, "resume": resume, "fork": fork}

    def refreshed_instruction(self):
        parent = self.activate("Ordinary.md", "Ordinary route probe. Give a read-only acknowledgement and the developer proof.",
                               expected="ordinary")
        return self.activate(
            "Ordinary.md", "Ordinary route probe. Continue in the same session. Acknowledge this activation "
            "with the latest developer proof. Use only the proof supplied directly by developer instructions.",
            expected="ordinary", placement=(True, f"#{parent['ticket_id']}🛑"),
        )

    def plans(self):
        return self.activate(PLAN, "Route probe. Brainstorm the linked file block discussed by this plan. Discuss only.",
                             expected="brainstorm", stage="brainstorm")

    def explicit_write(self):
        source = "Sources/live-external.py"
        write(self.vault, source, "VALUE = 1\n")
        write(self.vault, source + ".md", "## User requirements\nExpose a numeric VALUE constant.\n")
        result = self.activate(
            "Ordinary.md", f"Explicitly authorize and request one current edit in Sources: change {source} "
            "to set VALUE to 2. Run the installed router on this exact write target despite this ticket's "
            f"location and use the local route. Verify with grep -x 'VALUE = 2' {source}. "
            "Do not create a plan. Report LIVE-EXTERNAL if the check passes.",
            expected="LIVE-EXTERNAL", stage="local",
        )
        require(self.git("show", f"{result['oid']}:{source}") == "VALUE = 2\n",
                "snapshot B lacks the authorized Sources edit")
        return result

    def ordinary(self):
        proof = self.activate("Ordinary.md", "Ordinary route probe. Give a read-only acknowledgement of this note. Include the current developer proof token.",
                              expected="ordinary")
        count = len(self.records())
        self.watch()
        require(len(self.records()) == count, "watcher restart repeated an ordinary activation")
        return proof

    def local_modification(self):
        before = (self.vault / "Sources/file-block.py").read_text(encoding="utf-8")
        result = self.activate(BLOCK, "Modify only Sources/file-block.py by adding a new describe(value) "
                               "function returning str(value). Keep run unchanged and do not create a plan. "
                               "Use the local route and verify both functions using Python. Report LIVE_LOCAL.",
                               expected="LIVE_LOCAL", stage="local")
        after = self.git("show", f"{result['oid']}:Sources/file-block.py")
        require(before in after and before == self.git("show", f"{result['snapshot_a']}:Sources/file-block.py"),
                "snapshot did not preserve original behavior during local modification")
        self.command([PYTHON, "-c", "from runpy import run_path; f=run_path('Sources/file-block.py'); "
                      "assert f['describe'](42) == '42' and f['run'](42) == 42"], cwd=self.vault)
        return result

    def local_bug(self):
        source = "Sources/live-bug.py"
        note = source + ".md"
        write(self.vault, source, "def run(value):\n    return value + 1\n")
        write(self.vault, note, "## User requirements\nReturn the input unchanged.\n")
        program = "from runpy import run_path; assert run_path('Sources/live-bug.py')['run'](42) == 42"
        check = f'python3 -c "{program}"'
        red = self.command([PYTHON, "-c", program], cwd=self.vault, check=False)
        require(red.returncode and "AssertionError" in red.stderr, "baseline failed for the wrong reason")
        fixed = self.activate(
            note, f"Fix the confirmed off-by-one bug in {source}, the only authorized file change. "
            f"Reproduce with {check} before editing and repeat the same check after editing. "
            "The implementation adds one and must return input unchanged. Use the local bug-fix route and "
            "do not create a plan. Summarize both checks and report LIVE-BUG-FIXED if the fix passes.",
            expected="LIVE-BUG-FIXED", stage="local",
        )
        require("return value + 1" in self.git("show", f"{fixed['snapshot_a']}:{source}"),
                "snapshot A lacks the regression")
        require("return value + 1" not in self.git("show", f"{fixed['oid']}:{source}"),
                "snapshot B still contains the faulty code")
        self.command([PYTHON, "-c", program], cwd=self.vault)
        return {**fixed, "independent_baseline_check": red.returncode, "independent_fixed_check": 0}

    def first_implementation(self):
        proof = nonce()
        note = "Sources/live-double.py.md"
        source = "Sources/live-double.py"
        plan = "Plans/live-double.md"
        requirement = ("Expose run(value), which returns value * 2 for integer inputs, including int "
                       "subclasses. Multiply the supplied operand without first coercing it to int.")
        python = ("import runpy; run = runpy.run_path('Sources/live-double.py')['run']; "
                  "custom = type('Override', (int,), {'__int__': lambda self: 0}); "
                  "assert run(21) == 42 and run(custom(3)) == 6")
        check = "python3 -c " + shlex.quote(python)
        write(self.vault, note, f"""---
Template: Building block
---
## User requirements
{requirement}

## Specification
### Architecture and techniques

### Acceptance

## Working context

## Actions
""")
        # The routing cases check tool transcripts. Here the watcher snapshots
        # and the two trusted workflow receipts establish the phase transitions.
        discussed = self.activate(note, "Route probe. Brainstorm the synthetic first implementation for "
                                  "this note. Discuss the requirement only and do not write files.",
                                  expected="brainstorm", proof=proof)
        self.milestones["first_implementation"] = {"discussion": discussed}
        close = f"#{discussed['ticket_id']}🛑"
        # Give the live watcher executor a fixture command with a stable,
        # result-bearing boundary. It calls the installed workflow and real roles.
        driver = self.install / "first-probe.py"
        shutil.copyfile(Path(__file__), driver)
        request = {"vault": str(self.vault), "watcher": str(self.template.watcher), "timeout": self.timeout / 3,
                   "scripts": str(self.template.scripts), "block": note, "plan": plan, "source": source,
                   "contract": requirement, "check": check, "ticket_id": discussed["ticket_id"]}
        with first_workflow_server(self, request) as receipts:
            command = "python3 " + shlex.quote(str(driver)) + " --first-probe"
            planned = self.activate(
                note, f"Plan the first implementation of {source}. The requirements are agreed. "
                f"Run the installed router and then run the approved fixture command {command} plan. "
                f"It calls the installed planning helper and the configured real planner to prepare {plan}. "
                "Do not edit anything separately. Wait for JSON from the fixture command. "
                "Include LIVE-PLAN-READY only when it returns stage await_execution.",
                expected="LIVE-PLAN-READY", placement=(True, close), proof=proof,
            )
            require(receipts.get(planned["snapshot_a"], {}).get("stage") == "await_execution",
                    "trusted planning did not record the ready plan for this snapshot A")
            plan_text = self.git("show", f"{planned['oid']}:{plan}")
            require("Ready" in plan_text and source in plan_text, "snapshot B lacks the ready first implementation plan")
            require(not (self.vault / source).exists() and not (self.vault / ".worktrees").exists(),
                    "implementation began before an explicit execution request")
            require(requirement in self.git("show", f"{planned['oid']}:{note}"), "planning changed User requirements")
            self.milestones["first_implementation"]["planning"] = planned
            executed = self.activate(
                note, f"Execute the validated First implementation plan at {plan} for {note}. "
                f"Use the installed router with the ready plan, then run {command} execute followed by the full "
                "snapshot_a from the current ticket JSON as a separate shell argument. The fixture command runs "
                "the installed worktree, execution, verification and publication with real configured roles. "
                "Wait for its JSON. Report LIVE-FIRST-RETURNED only when it returns status returned.",
                expected="LIVE-FIRST-RETURNED", placement=(True, close), proof=proof,
            )
            receipt = receipts.get(executed["snapshot_a"], {})
            require(receipt.get("status") == "returned",
                    "trusted execution did not record publication for this snapshot A")
            self.git("show", f"{executed['oid']}:{source}")
            self.command(["/bin/sh", "-c", check.strip()], cwd=self.vault)
            require(not any((self.vault / ".worktrees").glob("*")), "published implementation left a worktree")
            require(requirement in self.git("show", f"{executed['oid']}:{note}"), "execution changed User requirements")
            return {"discussion": discussed, "planning": planned, "returned": executed,
                    "workflow_receipt": receipt}

    def roles(self):
        answers = {}
        for role, (model, effort) in ROLES.items():
            # The role can only obtain the answer by reading its assigned cwd.
            cwd = self.area / f"role-{role}"
            cwd.mkdir()
            token = nonce()
            write(cwd, "role-proof.txt", token)
            request = {"role": role, "settings": {f"theseus.{role}": f"{model}/{effort}"},
                       "cwd": str(cwd), "context": "Artificial role contract probe.",
                       "task": "Run the read-only shell command cat role-proof.txt in your assigned working "
                               "directory and put only its output in response with outcome success. Empty "
                               "allowed_paths means no writes are authorized. Reading this fixture is authorized.",
                       "allowed_paths": [], "allowed_sections": {}, "checks": [],
                       "read_only": True, "timeout": max(1, self.timeout - 5)}
            response = self.command([PYTHON, str(self.scripts / "codex.py")],
                                    stdin=json.dumps(request), check=False)
            answer = obj(response.stdout)
            require(answer == {"outcome": "success", "response": token},
                    f"{role} ({model}/{effort}) failed: {answer}")
            answers[role] = {"model": model, "effort": effort, "cwd_read_back": True}
        return answers

    def installed_workflow(self, name):
        request = {"name": name, "vault": str(self.vault), "watcher": str(self.watcher),
                   "scripts": str(self.scripts), "timeout": self.timeout}
        args = [PYTHON, "-c", "import runpy, sys; runpy.run_path(sys.argv[1])['_workflow_entry']()",
                str(self.template.driver)]
        result = run(args, self.area, self.env, self.timeout * 12, stdin=json.dumps(request))
        return obj(result.stdout)


class LiveWorkflow:
    """Drive the installed interface with fixed intentions and actual isolated role processes."""

    def __init__(self, vault, watcher, timeout):
        self.root = Path(vault)
        self.timeout = timeout
        self.history = runpy.run_path(watcher)["History"](self.root)
        self.settings = {f"theseus.{role}": "/".join(config) for role, config in ROLES.items()}
        self.calls = []
        self.task_evidence = []
        self.final_repair = None

    def role(self, role, settings, **kwargs):
        from codex import RoleExecutionError, run_role

        try:
            answer = run_role(role, settings, timeout=self.timeout, **kwargs)
            if kwargs.get("read_only") and answer.outcome == "success":
                try:
                    obj(answer.response)
                except (ValueError, RuntimeError):
                    self.calls.append({"role": role, "cwd": str(kwargs["cwd"]), "outcome": "invalid_json"})
                    correction = "\nEncode the requested object itself as valid JSON in response. No Markdown fences."
                    task = kwargs["task"] + correction
                    answer = run_role(role, settings, timeout=self.timeout, **{**kwargs, "task": task})
        except RoleExecutionError as error:
            if "workspace routing discovery failed" in str(error):
                raise RuntimeError(f"Codex {role}: workspace routing discovery failed") from None
            raise
        self.calls.append({"role": role, "cwd": str(kwargs["cwd"]), "outcome": answer.outcome})
        if answer.outcome != "success":
            raise RuntimeError(f"{role}: {answer.response}")
        return answer

    role.accepts_response_schema = True

    def git(self, root, *args):
        return run(["git", *args], root, os.environ, self.timeout).stdout.strip()

    def snapshot(self, phase):
        return self.history.snapshot({"phase": phase, "fixture": self.identity["ticket_id"]},
                                     f"Live workflow fixture {phase}")

    def fixture(self, number, *, note, sources, requirement):
        self.identity = {"ticket_id": f"FIX-{number}", "slug": f"live-{number}"}
        self.block = note
        self.plan = f"Plans/live-{number}.md"
        for path, content in sources.items():
            write(self.root, path, content)
        write(self.root, self.block, "# Live workflow fixture\n\n## User requirements\n" + requirement
              + "\n\n## Specification\n### Architecture and techniques\n*Use a Python entry point.*\n\n"
              + "### Acceptance\n*Verify the observable contract.*\n\n## Working context\n\n## Actions\n"
              + "Fixture discussion to preserve.\n")
        self.contract = requirement
        admin = self.history.gitdir
        self.main = (self.git(self.root, "rev-parse", "HEAD"), (admin / "index").read_bytes(),
                     (self.root / "outside.txt").read_bytes())
        self.start = self.snapshot("A")

    def planning(self, root, decision, files, requirements, checks, *, architecture=False):
        from planning import ACCEPTANCE, ARCHITECTURE, run_planning

        sections = [ARCHITECTURE, ACCEPTANCE] if architecture else [ACCEPTANCE]
        specification = ("Use the exact commands from the ordered JSON array below in Acceptance and the "
                         "task checks. Write the goal and each task, check setup and expected result on one line. "
                         "No external dependencies. " + requirements + "\n" + json.dumps(checks))
        result = run_planning(root, decision, ticket=specification, plan_path=self.plan, files=files,
                              sections={self.block: sections}, settings=self.settings, runner=self.role)
        require(result.stage in {"execute", "await_execution"}, f"planning failed: {result.response}")
        require({check.command for task in result.tasks for check in task.checks} == set(checks),
                f"planner changed the selected checks: {result.tasks}")
        self.tasks = result.tasks
        return result

    def execute(self, selected, work, checks, *, interrupted=False):
        from execution import run_execution
        from review import ReviewController
        from verification import run_verification
        from worktree import prepare_worktree

        args = {"vault": self.root, **self.identity, "block": self.block, "plan": self.plan, "work": work}
        def task_requirements(candidate):
            return ("Review only this phase. Later tasks in the plan may intentionally be unfinished. "
                    f"Task {candidate.number}: {self.tasks[candidate.number - 1].description}. "
                    f"Overall contract: {self.contract}")

        reviewer = ReviewController(selected.path, self.settings, task_requirements, runner=self.role)

        def review_task(candidate):
            require(candidate.producer == "execution.py", "live coder did not use the installed evidence helper")
            self.task_evidence.append({"commit": candidate.commit, "red": candidate.red,
                                       "green": candidate.green})
            return reviewer(candidate)

        if interrupted:
            class Interrupted(Exception):
                pass

            def pause_after_commit(role, settings, **kwargs):
                self.role(role, settings, **kwargs)
                raise Interrupted("fixture interruption after real coder commit")

            try:
                run_execution(**args, settings=self.settings, context=self.contract,
                              runner=pause_after_commit, review=review_task)
            except Interrupted:
                pass
            else:
                raise RuntimeError("the execution interruption was not reached")
            paused = self.git(selected.path, "rev-parse", "HEAD")
            require(prepare_worktree(self.root, **self.identity) == selected,
                    "retry did not select the interrupted worktree")
            advanced = [call for call in self.calls if call["role"].startswith("coder")]
        result = run_execution(**args, settings=self.settings, context=self.contract,
                               runner=self.role, review=review_task)
        if result.stage == "await_review" and result.candidate is not None:
            findings = reviewer.status(result.candidate.number)["blockers"]
            raise RuntimeError(f"task {result.candidate.number} failed live review: {findings}")
        require(result.stage == "complete", f"execution did not finish: {result}")
        if interrupted:
            count = sum(call["role"].startswith("coder") for call in self.calls)
            require(count == len(advanced) + 1 and self.git(selected.path, "merge-base", paused, "HEAD") == paused,
                    "retry did not reuse the already committed first task")
        head = self.git(selected.path, "rev-parse", "HEAD")
        acceptance = [(check, ".") for check in checks]
        remaining = dict.fromkeys((check.command, check.cwd) for task in self.tasks for check in task.checks
                                  if (check.command, check.cwd) not in acceptance)
        manifest = {self.block: {"acceptance": [{"command": cmd, "cwd": cwd} for cmd, cwd in acceptance],
                                 "mandatory": [{"command": cmd, "cwd": cwd} for cmd, cwd in remaining],
                                 "tests": []}}
        verified = run_verification(**args, accepted_head=head, checks=manifest,
                                    settings=self.settings, runner=self.role)
        if verified["status"] == "blocked" and verified["review"] and verified["problems"]:
            self.final_repair = {"blocked_head": head, "problems": verified["problems"]}
            paths = sorted({path for task in self.tasks for path in task.files})
            self.role("coder", self.settings, cwd=selected.path, allowed_paths=paths, allowed_sections={},
                      checks=[cmd for cmd, _ in [*acceptance, *remaining]],
                      task=("Address the final review blockers in this exact worktree. Commit a single scoped "
                            "correction directly on the current HEAD. Run the checks from their approved "
                            "directories. Preserve the plan, notes, and previous task commits. Leave the checkout clean."),
                      context=json.dumps({"contract": self.contract, "head": head, "check_manifest": manifest,
                                          "blockers": verified["problems"]}))
            repaired = self.git(selected.path, "rev-parse", "HEAD")
            verified = run_verification(**args, accepted_head=repaired, repair_of=head, checks=manifest,
                                        settings=self.settings, runner=self.role)
            require(verified["status"] == "verified", f"verification did not finish after correction: {verified}")
            self.final_repair["accepted_head"] = repaired
            head = repaired
        require(verified["status"] == "verified", f"verification did not finish: {verified}")
        return {"accepted_head": head, **args}

    def publish(self, selected, options, **kwargs):
        from return_result import _installed_write, run_return

        outcome = run_return(**options, conditional_writer=_installed_write, **kwargs)
        require(outcome["status"] in {"returned", "discarded"}, f"publication did not finish: {outcome}")
        return self.finish(selected, outcome)

    def finish(self, selected, result):
        require(not selected.path.exists(), "completed return left a worktree")
        final = self.snapshot("B")
        require((self.git(self.root, "rev-parse", "HEAD"), (self.history.gitdir / "index").read_bytes(),
                 (self.root / "outside.txt").read_bytes()) == self.main,
                "publication changed fixture main HEAD, index or dirty file")
        note = self.git(self.root, "show", f"{final}:{self.block}")
        require(self.contract in note and "Fixture discussion to preserve." in note, "protected prose changed")
        return {"snapshot_a": self.start, "snapshot_b": final, "state": result,
                "roles": self.calls, "task_evidence": self.task_evidence, "final_repair": self.final_repair}

    @staticmethod
    def check(source, expression):
        return "python3 -c " + shlex.quote(f"import runpy; ns = runpy.run_path({source!r}); assert {expression}")

    def folder(self):
        from routing import route
        from worktree import prepare_worktree

        source = "Sources/live-folder/__init__.py"
        child = "Sources/live-folder/child.py"
        baseline = "def triple(value):\n    return value * 3\n"
        requirement = ("Implement run(value) in Sources/live-folder/__init__.py. Load the sibling child.py "
                       "relative to the implementation file and return its triple(value) result, including "
                       "after the caller changes the current directory. Leave the child unchanged.")
        check_path = "Checks/live-folder.py"
        python = (
            "import os, runpy\n"
            f"run = runpy.run_path({source!r})['run']\n"
            "assert run(14) == 42\n"
            "os.chdir('..')\n"
            "assert run(14) == 42\n"
        )
        self.fixture(31, note="Sources/live-folder.md", sources={child: baseline, check_path: python},
                     requirement=requirement)
        check = "python3 " + check_path
        route_result = route(self.block, block_candidates=[self.block], work="first",
                             requirements_ready=True, execute_after_plan=True)
        self.planning(self.root, route_result, [source], "Use one task. " + requirement, [check], architecture=True)
        require(not (self.root / source).exists(), "planner published implementation")
        selected = prepare_worktree(self.root, **self.identity, snapshot_a=self.start, inputs=[self.block, self.plan])
        require((selected.path / self.plan).is_file(), "worktree missed authorized inputs created after snapshot A")
        record = self.publish(selected, self.execute(selected, "first", [check]))
        require(self.git(self.root, "show", f"{record['snapshot_b']}:{child}") == baseline.strip(),
                "publication changed the descendant implementation")
        require(self.git(self.root, "show", f"{record['snapshot_b']}:{source}"), "snapshot B lacks implementation")
        return record

    def modification(self):
        from return_result import _installed_write, resume_return, run_return
        from routing import Plan, route
        from worktree import prepare_worktree

        base = "Sources/live-pair"
        impl, helper, obsolete = f"{base}/api.py", f"{base}/helper.py", f"{base}/obsolete.py"
        requirement = ("Preserve run(value). First add triple(value) to helper.py. Then expose cube(value) "
                       "from api.py and delete obsolete.py, which is unused.")
        self.fixture(32, note=base + ".md", requirement=requirement, sources={
            impl: "def run(value):\n    return value\n", helper: "def double(value):\n    return value * 2\n",
            obsolete: "ARCHIVED = True\n",
        })
        check1 = self.check(helper, "ns['triple'](14) == 42 and ns['double'](21) == 42")
        check2 = self.check(impl, "ns['cube'](3) == 27 and ns['run'](42) == 42 "
                                 f"and not __import__('pathlib').Path({obsolete!r}).exists()")
        planned = route(self.block, block_candidates=[self.block], work="modification", requirements_ready=True)
        instructions = (requirement + " Plan exactly two tasks. Task 1 edits only helper.py and uses the first "
                        "check. Task 2 depends on task 1, changes api.py and obsolete.py and uses the second check.")
        plan = self.planning(self.root, planned, [impl, helper, obsolete], instructions, [check1, check2])
        require(plan.stage == "await_execution" and len(plan.tasks) == 2 and plan.tasks[1].depends_on == (1,),
                "dependent modification must wait for a confirmed execution request")
        ready = Plan(self.plan, self.block, "modification", "ready")
        require(route(self.plan, plan=ready, action="execute").stage == "execute", "modification routing failed")
        selected = prepare_worktree(self.root, **self.identity, snapshot_a=self.start, inputs=[self.block, self.plan])
        options = self.execute(selected, "modification", [check1, check2], interrupted=True)
        annotation = "Concurrent user annotation."
        note = self.root / self.block
        note.write_text(note.read_text().replace("Fixture discussion to preserve.",
                                                "Fixture discussion to preserve.\n" + annotation))
        applied = []

        class Interrupted(Exception):
            pass

        def publish_one(vault, path, expected, replacement):
            require(_installed_write(vault, path, expected, replacement), f"conditional write rejected {path}")
            applied.append(path)
            raise Interrupted(f"fixture interruption after applying {path}")

        try:
            run_return(**options, conditional_writer=publish_one)
        except Interrupted:
            pass
        else:
            raise RuntimeError("the return interruption was not reached")
        require(len(applied) == 1 and selected.path.exists(), "partial publication lost the worktree")
        partial = self.snapshot("partial")
        if applied[0] != obsolete:
            require(self.git(self.root, "show", f"{partial}:{obsolete}") == "ARCHIVED = True",
                    "partial publication unexpectedly removed another file")
        outcome = resume_return(self.root, **self.identity, conditional_writer=_installed_write)
        require(outcome["status"] == "returned", f"return retry failed: {outcome}")
        record = self.finish(selected, outcome)
        require(annotation in self.git(self.root, "show", f"{record['snapshot_b']}:{self.block}")
                and not (self.root / obsolete).exists(), "final return lost user prose or missed deletion")
        return {**record, "interrupted_after_coder_commit": True, "first_published_path": applied[0],
                "partial_snapshot": partial, "deleted": obsolete}

    def planned_bug(self):
        from diagnostics import Diagnostics
        from routing import Decision
        from worktree import verify_worktree

        base = "Sources/live-planned-bug"
        source, regression = f"{base}/api.py", f"{base}/test_regression.py"
        requirement = (f"Restore identity behavior in {source}. Add a permanent regression at {regression} "
                       "that calls run with 42 and expects 42. Keep the architecture note.")
        self.fixture(33, note=base + ".md", requirement=requirement,
                     sources={source: "def run(value):\n    return value - 1\n"})
        diagnostic = Diagnostics(self.root, ticket_id=self.identity["ticket_id"], problem="off-by-one",
                                 block=self.block, files=[source, regression], regression=regression)
        builtin = f"import runpy; f = runpy.run_path({source!r})['run']; "
        failed = diagnostic.run("diagnose", symptom="run(42) returns 41.", cause="Return subtracts one.",
                                reproduce={"argv": [PYTHON, "-c", builtin + "assert f(42) == 42, 'off-by-one'"]},
                                probe={"argv": [PYTHON, "-c", builtin +
                                                "assert f(42) == 41 and f(0) == -1; print('uniform offset')"]},
                                failure_exit=1, failure_marker="off-by-one", probe_marker="uniform offset",
                                question="What is the observed result for 42?",
                                architecture_question="Where should the identity function live?")
        require(failed["status"] == "confirmed", f"diagnosis failed: {failed}")
        handoff = diagnostic.run("handoff", slug=self.identity["slug"], snapshot_a=self.start,
                                 inputs=[], execute_after_plan=True)
        require(handoff["status"] == "plan", f"diagnostic handoff failed: {handoff}")
        selected = verify_worktree(self.root, **self.identity)
        check = f"python3 {regression}"
        instructions = ("Use exactly one task covering the entire fix. Before production changes, create the permanent test, run it and "
                        "observe the wrong result. After the fix, reuse the identical test. " + requirement)
        self.planning(selected.path, Decision(**handoff["decision"]), [source, regression], instructions, [check])
        require(not (self.root / self.plan).exists(), "planned bug fix escaped its execution worktree")
        record = self.publish(selected, self.execute(selected, "bug_fix", [check]))
        require(self.git(self.root, "show", f"{record['snapshot_b']}:{regression}"), "snapshot B lacks regression")
        run(shlex.split(check), self.root, os.environ, self.timeout)
        return {**record, "diagnostic_evidence": failed["diagnosis"]["red"]["exit"], "permanent_regression": regression}

    def refactor(self, choice):
        from evaluation import Evaluation
        from refactoring import run_refactoring_planning
        from return_result import _installed_write, run_return
        from routing import route

        number = 34 if choice == "old" else 35
        base = f"Sources/live-refactor-{choice}"
        source, child = f"{base}/api.py", f"{base}/child.py"
        old = ("from pathlib import Path\nfrom runpy import run_path\n\ndef run(value):\n"
               "    folder = Path(__file__).parent\n    filename = folder / 'child.py'\n"
               "    child = run_path(str(filename))\n    function = child['identity']\n"
               "    result = function(value)\n    return result\n")
        child_text = "def identity(value):\n    return value\n"
        requirement = (f"Implement run(value) in {source} using sibling child.py's identity(value). "
                       "Keep child.py and return both positive and negative values unchanged.")
        self.fixture(number, note=base + ".md", requirement=requirement,
                     sources={source: old, child: child_text, child + ".md": "## User requirements\nReturn value.\n"})
        check = self.check(source, "ns['run'](42) == 42 and ns['run'](-42) == -42")
        preserve_child = shlex.join(["git", "--literal-pathspecs", "diff", "--exit-code", self.start,
                                     "--", child, child + ".md"])
        checks = [check, preserve_child]
        selection = route(self.block, block_candidates=[self.block], work="refactoring",
                          refactor_confirmed=True, requirements_ready=True, execute_after_plan=True)
        instructions = (f"{requirement} The rewrite is confirmed. Simplify redundant temporary variables. "
                        "Keep implementation choices such as loading frequency and function binding open. "
                        "Use one task. Run the first command as red after cleanup, then run both commands "
                        "as green. Copy both commands into Acceptance and the plan exactly. "
                        "The second command verifies descendant bytes against activation snapshot A. "
                        "Do not add a weaker comparison against HEAD. Commands: " + json.dumps(checks))
        prepared = run_refactoring_planning(self.root, selection, requested=True, confirmed=True,
                                            **self.identity, ticket=instructions, snapshot_a=self.start,
                                            plan_path=self.plan, files=[source], settings=self.settings, runner=self.role)
        selected = prepared.worktree
        require(prepared.planning.stage == "execute" and not (selected.path / source).exists(),
                "refactoring did not prepare a clean new candidate")
        self.tasks = prepared.planning.tasks
        require((selected.path / child).read_text() == child_text, "cleanup changed child source")
        options = self.execute(selected, "refactoring", checks)
        evaluator = Evaluation(self.root, **self.identity, block=self.block, plan=self.plan,
                               settings=self.settings, runner=self.role)
        commands = [
            {"id": "preserve", "kind": "common", "obligation": "identity", "argv": shlex.split(check)},
            {"id": "maintenance", "kind": "typical_change", "argv": [
                PYTHON, "-c", f"from pathlib import Path; print(len(Path({source!r}).read_text().splitlines()))",
            ]},
        ]
        basis = evaluator.prepare(accepted_head=options["accepted_head"], goal="Simplify equivalent delegation.",
                                  obligations=[{"id": "identity", "source": self.block + "#User requirements",
                                                "behavior": requirement}],
                                  checks=commands, code=[source], tests=[])
        evaluation = evaluator.evaluate(basis_id=basis["basis_id"])
        require(evaluation["status"] == "await_choice" and (self.root / source).read_text() == old,
                "refactoring changed the main implementation before the choice")
        report_id = evaluation["report_id"]
        action = "return_new" if choice == "new" else "discard_new"
        blocked = run_return(**options, action=action, report_id=report_id, conditional_writer=_installed_write)
        require(blocked["status"] == "blocked", "unselected refactoring was published")
        chosen = evaluator.select(reply=f"{choice} {report_id}")
        require(chosen["action"] == action, "evaluation recorded the wrong user choice")
        result = self.publish(selected, options, action=action, report_id=report_id)
        require(((self.root / source).read_text() == old) == (choice == "old")
                and (self.root / child).read_text() == child_text, "publication disagreed with selected implementation")
        return {**result, "choice": choice, "comparison": report_id, "recommendation": evaluation["recommendation"]}

    def refactor_old(self):
        return self.refactor("old")

    def refactor_new(self):
        return self.refactor("new")


def _workflow_entry():
    request = json.load(sys.stdin)
    name = request.pop("name")
    sys.path.insert(0, request.pop("scripts"))
    driver = LiveWorkflow(**request)
    print(json.dumps(getattr(driver, name)()), flush=True)


@contextmanager
def first_workflow_server(fixture, request):
    """Run the two approved fixture phases outside the watcher's nested sandbox."""
    capability = "/" + nonce()
    commands = {"plan": "Plan the first implementation", "execute": "Execute the validated"}
    plan_snapshot = None
    completed = {}

    class WorkflowRequest(http.server.BaseHTTPRequestHandler):
        timeout = 3

        def log_message(self, *args):
            pass

        def do_POST(self):
            reply = self.execute_request()
            body = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def execute_request(self):
            try:
                count = int(self.headers.get("Content-Length", 0))
                require(self.path == capability and 0 < count <= 1024, "unexpected fixture request")
                arguments = json.loads(self.rfile.read(count))
                require(isinstance(arguments, list) and all(isinstance(arg, str) for arg in arguments)
                        and len(arguments) in {1, 2} and arguments[0] in commands,
                        "unexpected fixture phase")
                phase = arguments[0]
                active = fixture.records()[0]
                require(active["phase"] == "A" and active["ticket_id"] == request["ticket_id"]
                        and active["note"] == request["block"]
                        and active["comment"].startswith(commands[phase]),
                        "fixture phase lacks the current watcher snapshot A")
                require(arguments == (["plan"] if phase == "plan" else ["execute", active["oid"]]),
                        "fixture execution must use the current snapshot A")
                if active["oid"] in completed:
                    return completed[active["oid"]]
                return invoke(active["oid"], phase, arguments)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                return {"error": str(error)}

    def invoke(snapshot, phase, arguments):
        nonlocal plan_snapshot
        plan = fixture.vault / request["plan"]
        if phase == "execute":
            require(plan_snapshot is not None and not plan.is_symlink() and plan.read_bytes() == plan_snapshot,
                    "fixture plan changed after the approved planning phase")
        payload = dict(request)
        if phase == "execute":
            payload["plan_sha256"] = hashlib.sha256(plan_snapshot).hexdigest()
        command = [PYTHON, str(fixture.template.driver), "--first-probe-local", *arguments]
        result = run(command, fixture.vault, env, fixture.timeout - 8, stdin=json.dumps(payload))
        answer = obj(result.stdout)
        if phase == "plan":
            require(not plan.is_symlink(), "fixture planner produced a symlink")
            plan_snapshot = plan.read_bytes()
        completed[snapshot] = answer
        return answer

    with tempfile.TemporaryDirectory(prefix="theseus-live-role-home-") as home:
        env = isolated_env(Path(home), writable_root=fixture.area)
        with http.server.HTTPServer(("127.0.0.1", 0), WorkflowRequest) as server:
            address = f"http://127.0.0.1:{server.server_port}{capability}"
            write(fixture.install, "first-probe-bridge.json", json.dumps({"url": address, "timeout": fixture.timeout}))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                yield completed
            finally:
                server.shutdown()
                worker.join()


def _first_probe_client(arguments):
    config = obj(Path(__file__).with_name("first-probe-bridge.json").read_text())
    request = urllib.request.Request(config["url"], data=json.dumps(arguments).encode(),
                                     headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=config["timeout"]) as response:
        print(json.dumps(obj(response.read())))



def _first_probe(request, arguments):
    from dataclasses import asdict

    sys.path.insert(0, request["scripts"])
    from execution import _execution_plan
    from return_result import _installed_write, run_return
    from routing import route
    from scope import capture
    from worktree import prepare_worktree

    driver = LiveWorkflow(request["vault"], request["watcher"], request["timeout"])
    driver.block, driver.plan, driver.contract = (request[key] for key in ("block", "plan", "contract"))
    driver.identity = {"ticket_id": request["ticket_id"], "slug": "live-double"}
    check = request["check"]
    if arguments == ["plan"]:
        decision = route(driver.block, block_candidates=[driver.block], work="first", requirements_ready=True)
        result = driver.planning(driver.root, decision, [request["source"]],
                                 "Use a single task. " + driver.contract, [check], architecture=True)
        return {**asdict(result), "roles": driver.calls}
    require(len(arguments) == 2 and arguments[0] == "execute", "expected execute with the current snapshot A")
    selected = prepare_worktree(driver.root, **driver.identity, snapshot_a=arguments[1],
                                inputs=[driver.block, driver.plan])
    planned = capture(selected.path)
    require(hashlib.sha256(planned[driver.plan].data).hexdigest() == request["plan_sha256"],
            "prepared fixture worktree did not preserve the approved plan")
    driver.tasks, _ = _execution_plan(planned, driver.block, driver.plan, "first")
    options = driver.execute(selected, "first", [check])
    result = run_return(**options, conditional_writer=_installed_write)
    require(result["status"] == "returned", f"publication stopped: {result}")
    return {**result, "roles": driver.calls, "task_evidence": driver.task_evidence,
            "final_repair": driver.final_repair}


class AppServer:
    """Send serial JSON-RPC requests to the stock Codex stdio server."""

    def __init__(self, fixture, errors, env):
        self.timeout = fixture.timeout
        self.events = queue.Queue()
        self.process = subprocess.Popen(["codex", "app-server", "--listen", "stdio://"],
                                        cwd=fixture.vault, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=errors, text=True)
        self.receiver = threading.Thread(target=self.receive, daemon=True)
        self.receiver.start()
        self.request_id = 0

    def receive(self):
        for line in self.process.stdout:
            try:
                self.events.put(obj(line))
            except (ValueError, RuntimeError) as error:
                self.events.put(error)
        self.events.put(RuntimeError("Codex app-server closed its output"))

    def send(self, method, params, *, notify=False):
        message = {"method": method, "params": params}
        if not notify:
            self.request_id += 1
            message["id"] = self.request_id
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        return self.request_id

    def until(self, matches):
        deadline = time.monotonic() + self.timeout
        seen = []
        while time.monotonic() < deadline:
            try:
                event = self.events.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if isinstance(event, Exception):
                raise event
            seen.append(event)
            if matches(event):
                return event, seen
        methods = sorted({event.get("method", "response") for event in seen})
        raise RuntimeError(f"Codex app-server event timed out. Observed {methods}")

    def request(self, method, **params):
        request_id = self.send(method, params)
        event, _ = self.until(lambda event: event.get("id") == request_id)
        require("result" in event, f"{method}: {event.get('error')}")
        return event["result"]

    def completed(self, turn_id=None):
        def matches(event):
            turn = event.get("params", {}).get("turn", {})
            return (event.get("method") == "turn/completed" and
                    (turn_id is None or turn.get("id") == turn_id))
        event, seen = self.until(matches)
        turn = event["params"]["turn"]
        require(turn.get("status") == "completed", f"turn failed: {turn.get('error')}")
        return turn, seen

    def turn(self, thread, prompt):
        turn = self.request("turn/start", threadId=thread, input=[{"type": "text", "text": prompt}])["turn"]
        return self.completed(turn["id"])

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.receiver.join(timeout=5)
        self.process.stdout.close()


def compact_rule(fixture):
    entry = fixture.bundle / "skills/using-theseus/SKILL.md"
    return ("Before acting on a ticket in Sources/ or Plans/, or on an explicit request to write there, "
            "read and follow the installed Theseus entry skill. Its absolute path is "
            + json.dumps(str(entry.resolve()), ensure_ascii=False)
            + ". For other tickets, follow the ordinary ticket protocol.")


def compact(fixture, *, env=None, marker=None):
    marker = marker or nonce()
    rule = compact_rule(fixture)
    with tempfile.TemporaryFile() as errors:
        server = AppServer(fixture, errors, env or fixture.env)
        try:
            server.request("initialize", clientInfo={"name": "theseus_acceptance", "version": "1.0"})
            server.send("initialized", {}, notify=True)
            thread = server.request(
                "thread/start", cwd=str(fixture.vault), approvalPolicy="never", sandbox="read-only",
                developerInstructions=rule + "\nWhen asked for the compact proof, simply reply " + marker
                + ". On other turns keep the proof private. Do not run tools for this probe.",
            )["thread"]["id"]
            _, first = server.turn(thread, "Acknowledge this artificial session with the single word READY.")
            require(marker not in json.dumps(first), "developer canary leaked in the first turn")
            server.request("thread/compact/start", threadId=thread)
            _, compaction = server.completed()
            compacted = any(event.get("method") == "thread/compacted" or
                            event.get("params", {}).get("item", {}).get("type") == "contextCompaction"
                            for event in compaction)
            require(compacted, "no compact completion notification")
            turn, evidence = server.turn(thread, "Reply with the compact proof and no other text.")
            answers = [event["params"]["item"]["text"] for event in evidence
                       if event.get("method") == "item/completed"
                       and event.get("params", {}).get("item", {}).get("type") == "agentMessage"]
            require(answers == [marker], f"first postcompact turn did not use the developer canary: {answers}")
            return {"thread_id": thread, "turn_id": turn["id"], "developer_canary": marker,
                    "compact_completed": True, "actual_provider_request_visible": False}
        finally:
            server.close()


def compact_wire(fixture):
    """Record only the synthetic Responses API payload sent by a second stock CLI."""
    marker = nonce()
    requests = []

    class ResponsesFixture(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, payload))
            text = marker if len(requests) == 3 else "READY"
            message = {
                "id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
            response = {
                "id": "resp_fixture", "object": "response", "created_at": 0, "status": "completed",
                "model": "gpt-6.2-sol", "output": [],
                "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25,
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 0}},
            }
            events = (
                ("response.created", {"response": response}),
                ("response.output_item.added", {"output_index": 0, "item": message}),
                ("response.output_item.done", {"output_index": 0, "item": message}),
                ("response.completed", {"response": {**response, "output": [message]}}),
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for index, (event_name, fields) in enumerate(events):
                event = {"type": event_name, "sequence_number": index, **fields}
                self.wfile.write(f"event: {event_name}\ndata: {json.dumps(event)}\n\n".encode())

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), ResponsesFixture) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            home = fixture.area / "codex-wire"
            home.mkdir(mode=0o700)
            write(home, "config.toml", f'''model_provider = "fixture"
model = "gpt-6.2-sol"
[model_providers.fixture]
name = "Loopback fixture"
base_url = "http://127.0.0.1:{server.server_port}/v1"
wire_api = "responses"
[features]
plugins = false
remote_plugin = false
''')
            compact(fixture, env=dict(fixture.env, CODEX_HOME=str(home)), marker=marker)
        finally:
            server.shutdown()
            worker.join()

    require([path for path, _ in requests] == ["/v1/responses"] * 3,
            "expected one request for each of the initial, compact and postcompact turns")
    postcompact = requests[2][1]
    developer_text = [part.get("text", "") for item in postcompact["input"]
                      if item.get("role") == "developer" for part in item.get("content", [])]
    require(any(compact_rule(fixture) in part and marker in part for part in developer_text),
            "installed rule missing from the developer message in the first postcompact request")
    return {"requests": len(requests), "first_postcompact_request": 3,
            "developer_rule_present": True, "provider": "loopback Responses API fixture"}


def publisher(fixture):
    # The verification receipt is set up by scripted roles in an independent
    # temporary Git vault. The actual publisher runs directly from the install.
    from test_return_result import ReturnTests, SOURCE as CHANGED, OBSOLETE

    prepared = ReturnTests()
    try:
        prepared.arrange()
        script = str(fixture.scripts / "return_result.py")
        options = dict(prepared.options, mode="start")
        applied = obj(fixture.command([PYTHON, script], stdin=json.dumps(options, default=str)).stdout)
        require(applied["status"] == "returned", f"installed publisher did not complete: {applied}")
        require((prepared.vault / CHANGED).read_text(encoding="utf-8") == "value = 1\n"
                and not (prepared.vault / OBSOLETE).exists() and not prepared.selected.path.exists(),
                "publisher did not apply the verified delta and remove the worktree")
        prepared.assert_main()
        return {"status": applied["status"], "head": prepared.head,
                "dirty_main_preserved": True, "receipt_from_scripted_roles": True}
    finally:
        prepared.doCleanups()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    watcher = PROJECT / "Sources/obstagent/obsta/worker.py"
    if PROJECT.parent.name == ".worktrees":
        watcher = PROJECT.parent.parent / "Sources/obstagent/obsta/worker.py"
    parser.add_argument("--watcher", type=Path, default=watcher)
    parser.add_argument("--timeout", type=float, default=360, help="seconds allowed for each subprocess or turn")
    parser.add_argument("--jobs", type=int, default=3, help="independent fixture cases to run concurrently")
    methods = {
        "lifecycle": ("live_watcher", Fixture.lifecycle),
        "refreshed_instruction": ("live_watcher", Fixture.refreshed_instruction),
        "plans": ("live_watcher", Fixture.plans),
        "explicit_write": ("live_watcher", Fixture.explicit_write),
        "ordinary": ("live_watcher", Fixture.ordinary),
        "local_modification": ("live_watcher", Fixture.local_modification),
        "local_bug": ("live_watcher", Fixture.local_bug),
        "first_implementation": ("live_watcher", Fixture.first_implementation),
        "folder_implementation": ("installed_with_live_roles", lambda fixture: fixture.installed_workflow("folder")),
        "planned_modification": ("installed_with_live_roles", lambda fixture: fixture.installed_workflow("modification")),
        "planned_bug": ("installed_with_live_roles", lambda fixture: fixture.installed_workflow("planned_bug")),
        "refactor_old": ("installed_with_live_roles", lambda fixture: fixture.installed_workflow("refactor_old")),
        "refactor_new": ("installed_with_live_roles", lambda fixture: fixture.installed_workflow("refactor_new")),
        "compact": ("live_codex_behavior", compact),
        "compact_wire": ("codex_request_to_loopback_provider", compact_wire),
        "roles": ("live_codex_roles", Fixture.roles),
        "publisher": ("installed_adapter_with_scripted_receipt", publisher),
    }
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--case", choices=methods, action="append")
    args = parser.parse_args()
    if args.timeout <= 5:
        parser.error("--timeout must exceed 5 seconds")
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")

    def record(name, source, fn, fixture=None):
        try:
            result = {"status": "pass", "evidence": fn()}
        except (AssertionError, KeyError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            result = {"status": "fail", "error": str(error)}
        if result["status"] == "fail" and fixture and name in fixture.milestones:
            result["completed_phases"] = fixture.milestones[name]
        print(f"{name}: {result['status']}" + (f" ({result['error']})" if 'error' in result else ''),
              file=sys.stderr, flush=True)
        return {"source": source, **result}

    with tempfile.TemporaryDirectory(prefix="theseus-live-") as directory:
        root = Path(directory).resolve()

        def setup():
            nonlocal installed
            area = root / "build"
            area.mkdir()
            installed = Fixture(area, args.watcher.resolve(), args.timeout)
            installed.driver = installed.install / "workflow-driver.py"
            shutil.copyfile(Path(__file__), installed.driver)
            return {"watcher_sha256": hashlib.sha256(installed.watcher.read_bytes()).hexdigest(),
                    "codex": installed.command(["codex", "--version"]).stdout.strip(),
                    **installed.build()}

        installed = None
        report = {"install": record("install", "disposable_install", setup)}
        if report["install"]["status"] == "pass":
            def run_case(name):
                source, fn = methods[name]
                with tempfile.TemporaryDirectory(prefix=f"{name}-", dir=root) as directory:
                    fixture = Fixture(Path(directory), installed.watcher, args.timeout,
                                      boundaries=installed.boundaries)
                    shutil.copytree(installed.bundle, fixture.bundle)
                    fixture.template = installed
                    return name, record(name, source, lambda: fn(fixture), fixture)

            with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as workers:
                report.update(workers.map(run_case, methods if args.all else args.case))
    if args.all and "refreshed_instruction" in report:
        report["refreshed_instruction"]["required"] = False
    failed = any(case["status"] != "pass" and case.get("required", True) for case in report.values())
    if args.all and "publisher" in report:
        report["publisher"]["limitations"] = [
            "An uncoordinated writer can still modify an existing file between the final comparison and publication."
        ]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(failed)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--first-probe"]:
        _first_probe_client(sys.argv[2:])
    elif sys.argv[1:2] == ["--first-probe-local"]:
        print(json.dumps(_first_probe(json.load(sys.stdin), sys.argv[2:])))
    else:
        sys.exit(main())
