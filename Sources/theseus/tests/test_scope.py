import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notes import changed_sections, clear_section, read_section, replace_section
from scope import authorize, block_scope, capture


NOTE = b"""---
Template: Building block
---
## User requirements
Keep this.
````md
## Working context
```
## Specification
#KOL-99U0001F3AC A literal ticket
````
## Specification
### Acceptance
*Run tests.*
## Working context
*Existing notes.*
"""
BLOCK = "Sources/example.md"
CHILD = "Sources/example/sub/child.py.md"
SOURCE = "Sources/example/main.py"
CHILD_SOURCE = CHILD[:-3]
PLAN = "Plans/current [1].md"
PLAN_NOTE = b"## Context\n### Goal\nGoal\n### Plan status\nDraft\n## *Tasks*\nWork\n## Outcome\nChecks\n"


class ScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for path, data in (
            (BLOCK, NOTE), (CHILD, NOTE), (SOURCE, b"old"), (CHILD_SOURCE, b"child"),
            ("Sources/file.py.md", NOTE), ("Sources/file.py", b"old"),
            ("Sources/example-other.py", b"other"), (PLAN, PLAN_NOTE),
            ("Plans/archived.md", PLAN_NOTE),
        ):
            self.write(path, data)
        self.before = capture(self.root)

    def write(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def check(self, note=BLOCK, role="coder", stage="execute", work="modification", **keywords):
        return authorize(self.before, capture(self.root), note, role=role, stage=stage, work=work,
                         **keywords)

    def test_file_folder_and_nested_ownership(self):
        self.assertEqual(block_scope(self.before, BLOCK).implementation, "Sources/example")
        self.assertEqual(block_scope(self.before, BLOCK).descendants, (CHILD,))
        self.assertEqual(block_scope(self.before, "Sources/file.py.md").kind, "file")
        self.write(CHILD_SOURCE, b"new")
        self.write(SOURCE, b"new")
        self.assertEqual({change.path for change in self.check()}, {CHILD_SOURCE, SOURCE})
        with self.assertRaisesRegex(ValueError, CHILD_SOURCE):
            self.check(work="refactoring")
        with self.assertRaises(ValueError):
            self.check(note="Sources/file.py.md")
        with self.assertRaisesRegex(ValueError, CHILD_SOURCE):
            self.check(files=(SOURCE,))

    def test_file_block_cannot_claim_sibling_paths(self):
        self.write("Sources/file.py", b"update")
        self.assertEqual(self.check(note="Sources/file.py.md")[0].path, "Sources/file.py")
        self.write("Sources/file.py.extra", b"neighbor")
        with self.assertRaisesRegex(ValueError, "file.py.extra"):
            self.check(note="Sources/file.py.md")

    def test_rename_checks_both_endpoints_in_both_directions(self):
        for start, end in ((SOURCE, "Sources/outside.py"),
                           ("Sources/example-other.py", "Sources/example/other.py")):
            with self.subTest(start=start):
                (self.root / start).rename(self.root / end)
                with self.assertRaisesRegex(ValueError, "not writable"):
                    self.check(files=(SOURCE, end))
                (self.root / end).rename(self.root / start)
        (self.root / SOURCE).rename(self.root / "Sources/example/renamed.py")
        self.assertEqual({change.path for change in self.check()},
                         {SOURCE, "Sources/example/renamed.py"})

    def test_symlink_escapes_in_initial_and_final_state(self):
        alias = self.root / "Sources/example/sub"
        alias.rename(self.root / "sub")
        alias.symlink_to(self.root / "sub", target_is_directory=True)
        self.assertNotIn(CHILD_SOURCE, capture(self.root))
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.check()
        self.before = capture(self.root)
        alias.unlink()
        (self.root / "sub").rename(alias)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.check()

    def test_hardlink_to_file_outside_vault_is_not_writable(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "other.py"
            target.write_bytes(b"initial")
            alias = self.root / "Sources/example/linked.py"
            os.link(target, alias)
            self.before = capture(self.root)
            with self.assertRaisesRegex(ValueError, "hardlink"):
                self.check(files=("Sources/example/linked.py",))
            alias.write_bytes(b"escaped")
            with self.assertRaisesRegex(ValueError, "hardlink"):
                self.check()

    def test_explicit_targets_are_checked_even_if_no_files_change(self):
        alias = self.root / "Sources/example/alias"
        alias.symlink_to(self.root / "Plans", target_is_directory=True)
        (self.root / "Plans/alias.md").symlink_to(self.root / PLAN)
        (self.root / ".obsta-experiments").symlink_to(self.root / "Plans", target_is_directory=True)
        self.before = capture(self.root)
        for role, parameters in (
            ("coder", {"files": ("Sources/example/alias/current [1].md",)}),
            ("orchestrator", {"plan": "Plans/alias.md"}),
            ("orchestrator", {"experiments": ".obsta-experiments/KOL-5"}),
        ):
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, "symlink"):
                self.check(role=role, **parameters)
        with self.assertRaisesRegex(ValueError, "not writable"):
            self.check(files=("Sources/example-other.py",))

    def test_existing_plan_status_is_user_owned(self):
        original = b"---\nStatus: In progress\n---\n" + PLAN_NOTE
        self.write(PLAN, original)
        self.before = capture(self.root)
        for edited in (original.replace(b"In progress", b"Done"),
                       original.replace(b"Status: In progress", b"Status: In progress\n'Status': Done")):
            self.write(PLAN, edited)
            for role, stage in (("planner", "plan"), ("obstagent", "return")):
                with self.subTest(role=role), self.assertRaisesRegex(ValueError, "Status"):
                    self.check(role=role, stage=stage, plan=PLAN)

    def test_role_stage_and_plan_limits(self):
        edited = replace_section(NOTE, ("Specification", "Acceptance"), b"*Run new tests.*\n")
        self.write(BLOCK, edited)
        changes = self.check(role="planner", stage="plan",
                             sections={BLOCK: (("Specification", "Acceptance"),)})
        self.assertEqual(changes[0].sections, (("Specification", "Acceptance"),))
        for role, stage in (("coder", "execute"), ("orchestrator", "execute"),
                            ("planner", "execute"), ("reviewer", "review"),
                            ("obstagent", "return")):
            with self.subTest(role=role, stage=stage), self.assertRaises(ValueError):
                self.check(role=role, stage=stage)
        self.assertEqual(self.check(role="obstagent", stage="return", work="bug_fix")[0].path, BLOCK)
        self.write(BLOCK, NOTE)
        self.write(PLAN, PLAN_NOTE.replace(b"Draft", b"In progress"))
        self.assertEqual(self.check(role="orchestrator", plan=PLAN)[0].path, PLAN)
        with self.assertRaises(ValueError):
            self.check(role="orchestrator", plan="Plans/archived.md")
        self.write(PLAN, PLAN_NOTE.replace(b"Goal\n", b"Other goal\n"))
        with self.assertRaises(ValueError):
            self.check(role="orchestrator", plan=PLAN)

    def test_cannot_write_requirements_or_shadow_section(self):
        for body in (
            NOTE.replace(b"Keep this.", b"Tampered."),
            NOTE.replace(b"Keep this.", b"### Specification\n*Tampered.*"),
            NOTE + b"## Specification\nAmbiguous\n",
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.write(BLOCK, body)
                self.check(role="planner", stage="plan")

    def test_refactoring_descendant_detected_from_initial_and_final_state(self):
        (self.root / CHILD).unlink()
        self.write(CHILD_SOURCE, b"new")
        with self.assertRaises(ValueError):
            self.check(work="refactoring")
        self.before = capture(self.root)
        self.write(CHILD, NOTE)
        self.write(CHILD_SOURCE, b"new again")
        with self.assertRaises(ValueError):
            self.check(work="refactoring")

    def test_refactoring_cleanup_and_result_return(self):
        (self.root / SOURCE).unlink()
        (self.root / "Plans/archived.md").unlink()
        self.write(BLOCK, clear_section(clear_section(NOTE, "Specification"), "Working context"))
        changes = self.check(role="obstagent", stage="cleanup", work="refactoring")
        self.assertEqual({change.path for change in changes}, {SOURCE, BLOCK, "Plans/archived.md"})
        with self.assertRaisesRegex(ValueError, "archived.md"):
            self.check(role="obstagent", stage="return", work="refactoring", plan=PLAN)


class NotesTests(unittest.TestCase):
    def test_exact_sections_and_ticket_bytes(self):
        nested = (
            "#KOL-5🍏 Human text\n## Working context\n"
            "#KOL-5/1🍏 Inner reply #KOL-5/1🛑\n"
            "*⏳ Обрабатывается…*\n#KOL-5🛑\n"
        ).encode()
        status = b"%% obsta-pending:1234 %%\n"
        ticket = "- [x] User action #KOL-6🍏 *◇ Done* #KOL-6🛑\n".encode()
        content = NOTE + nested + status + ticket + b"## Later\nunchanged\n"
        content = content.replace(b"\n", b"\r\n")
        old = read_section(content, "Working context")
        new = replace_section(content, "Working context", old.replace(b"Existing", b"New"))
        self.assertEqual(new, content.replace(b"Existing", b"New"))
        self.assertEqual(changed_sections(content, new, ("Working context",)), (("Working context",),))
        self.assertEqual(clear_section(content, "Working context"),
                         content.replace(b"*Existing notes.*\r\n", b""))
        for tampered in (old.replace(nested.replace(b"\n", b"\r\n"), b""),
                         old.replace(status.replace(b"\n", b"\r\n"), b""),
                         old.replace(b"User action", b"Changed action")):
            with self.assertRaises(ValueError):
                replace_section(content, "Working context", tampered)

    def test_existing_fences_in_writable_sections_are_preserved(self):
        fence = b"````py\n# original\n````\n"
        data = NOTE + fence
        body = read_section(data, "Working context")
        with self.assertRaisesRegex(ValueError, "fenced"):
            replace_section(data, "Working context", body.replace(b"original", b"edited"))
        extra = b"~~~md\n## Literal\n~~~\n"
        self.assertEqual(replace_section(data, "Working context", body + extra), data + extra)
        self.assertEqual(clear_section(data, "Working context"),
                         NOTE.removesuffix(b"*Existing notes.*\n"))

    def test_indented_code_and_inline_delimiters_do_not_hide_headings(self):
        for body in (b"    ```md\n", b"Old ` unmatched delimiter\n"):
            content = b"## Working context\n" + body + b"## User requirements\nKeep ` this.\n"
            updated = content.replace(b"Keep", b"Overwrite")
            with self.subTest(body=body):
                self.assertEqual(read_section(content, "Working context"), body)
                with self.assertRaisesRegex(ValueError, "outside"):
                    changed_sections(content, updated, ("Working context",))

    def test_ambiguous_heading_and_unclosed_ticket_rejected(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            read_section(NOTE + b"## *Specification* ##\n", "Specification")
        with self.assertRaisesRegex(ValueError, "unclosed ticket"):
            replace_section(NOTE, "Working context", "#KOL-5🍏 unfinished\n".encode())

    def test_replacement_cannot_modify_neighboring_section_structure(self):
        with self.assertRaises(ValueError):
            replace_section(NOTE, "Specification", b"Title\n=====\nother content\n")


if __name__ == "__main__":
    unittest.main()
