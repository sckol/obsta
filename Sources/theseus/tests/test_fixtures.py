import shutil
import tempfile
import unittest
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "vault"


class VaultFixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name) / "vault"
        shutil.copytree(FIXTURE, self.vault)

    def test_file_block_has_exact_note_pair_and_literal_markers(self):
        implementation = self.vault / "Sources/file-block.py"
        note = self.vault / "Sources/file-block.py.md"
        self.assertTrue(implementation.is_file())
        self.assertTrue(note.is_file())
        content = note.read_text()
        self.assertIn("## User requirements", content)
        self.assertIn("## Specification", content)
        self.assertIn("## Working context", content)
        self.assertIn("#KOL-41🍏", content)
        self.assertIn("#KOL-41🛑", content)
        self.assertIn("#KOL-42🍏 Keep this marker literal #KOL-42🛑", content)
        self.assertIn("*⏳ Обрабатывается…*", content)

    def test_folder_block_includes_descendant_pair(self):
        parent = self.vault / "Sources/folder-block.md"
        child = self.vault / "Sources/folder-block/child.py"
        child_note = self.vault / "Sources/folder-block/child.py.md"
        self.assertTrue(parent.is_file())
        self.assertTrue(child.is_file())
        self.assertTrue(child_note.is_file())
        self.assertIn("## User requirements", parent.read_text())
        self.assertIn("#KOL-52🍏", child_note.read_text())

    def test_current_and_archived_plans_are_distinct(self):
        current = self.vault / "Plans/current-plan.md"
        archived = self.vault / "Plans/archived-plan.md"
        self.assertTrue(current.is_file())
        self.assertTrue(archived.is_file())
        self.assertIn("Status: In progress", current.read_text())
        self.assertIn("Status: Done", archived.read_text())
        self.assertIn("## *Tasks*", current.read_text())
        for plan in (current, archived):
            self.assertIn('Building block: "[[Sources/file-block.py]]"', plan.read_text())


if __name__ == "__main__":
    unittest.main()
