import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
REPO = SOURCE.parents[1]
LOCK = json.loads((SOURCE / "upstream.lock").read_text(encoding="utf-8"))
ARCHIVE = REPO / "References" / ("superpowers-" + LOCK["commit"] + ".tar.gz")


def inventory(root):
    return {
        path.relative_to(root).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode & 0o777
        )
        for path in root.rglob("*") if path.is_file()
    }


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for path in ("build.py", "routing.py", "codex.py", "notes.py", "scope.py", "planning.py",
                     "worktree.py", "execution.py", "review.py", "diagnostics.py", "verification.py",
                     "refactoring.py", "evaluation.py", "return_result.py", "upstream.lock"):
            shutil.copy2(SOURCE / path, self.source)
        self.patches = self.source / "patches"
        self.patches.mkdir()
        self.series("")

    def series(self, content):
        (self.patches / "series").write_text(content, encoding="utf-8")

    def run_build(self, archive=ARCHIVE, output=None, error=None):
        output = output if output is not None else self.root / "installed"
        result = subprocess.run(
            [sys.executable, str(self.source / "build.py"), "--archive", str(archive),
             "--output", str(output)],
            cwd=self.root, capture_output=True, text=True, check=False,
        )
        if error is None:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn(error, result.stderr)
        return output

    def test_real_archive_build_is_reproducible_and_allowlisted(self):
        installed = self.run_build()
        second = self.run_build(output=self.root / "second")
        first_inventory = inventory(installed)
        self.assertEqual(first_inventory, inventory(second))
        self.assertTrue(all(path.startswith("skills/") or path in {"LICENSE", "manifest.json"}
                            for path in first_inventory))
        self.assertIn("MIT License", (installed / "LICENSE").read_text(encoding="utf-8"))
        self.assertIn("skills/using-theseus/SKILL.md", first_inventory)
        self.assertIn("skills/using-theseus/scripts/diagnostics.py", first_inventory)
        self.assertIn("skills/using-theseus/scripts/verification.py", first_inventory)
        self.assertIn("skills/using-theseus/scripts/refactoring.py", first_inventory)
        self.assertNotIn("skills/brainstorming/visual-companion.md", first_inventory)
        self.assertNotIn("skills/brainstorming/scripts/start-server.sh", first_inventory)
        self.assertNotIn("skills/using-theseus/references/claude-code-tools.md", first_inventory)
        manifest = json.loads((installed / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["upstream"], LOCK)
        self.assertEqual(len(manifest["skills"]), 12)
        self.assertFalse(manifest["skills"]["compare-implementations"]["included"])
        self.assertEqual(set(manifest["files"]), set(first_inventory) - {"manifest.json"})
        for relative, record in manifest["files"].items():
            self.assertEqual(record["sha256"], first_inventory[relative][0])
            self.assertEqual(record["mode"], first_inventory[relative][1])
        self.run_build()
        self.assertEqual(inventory(installed), first_inventory)

    def test_project_series_installs_compare_implementations(self):
        shutil.copytree(SOURCE / "patches", self.patches, dirs_exist_ok=True)
        installed = self.run_build()
        manifest = json.loads((installed / "manifest.json").read_text(encoding="utf-8"))
        skill = manifest["skills"]["compare-implementations"]
        self.assertTrue(skill["included"])
        self.assertIsNone(skill["upstream"])
        self.assertIn("validate_choice", (installed / skill["path"]).read_text())
        self.assertIn("skills/using-theseus/scripts/evaluation.py", manifest["files"])
        self.assertIn("skills/using-theseus/scripts/return_result.py", manifest["files"])
        finishing = manifest["skills"]["finishing-a-development-branch"]
        self.assertIn("conditional writer", (installed / finishing["path"]).read_text())
        debugging = installed / manifest["skills"]["systematic-debugging"]["path"]
        instructions = debugging.read_text(encoding="utf-8")
        for name in ("test-driven-development", "verification-before-completion"):
            target = f"../{name}/SKILL.md"
            self.assertIn(f"]({target})", instructions)
            self.assertNotIn(f"superpowers:{name}", instructions)
            self.assertTrue((debugging.parent / target).is_file())

    def test_hash_mismatch_preserves_existing_install(self):
        installed = self.run_build()
        baseline = inventory(installed)
        bad = self.root / "bad.tar.gz"
        bad.write_bytes(b"not the pinned archive")
        self.run_build(archive=bad, error="SHA-256 mismatch")
        self.assertEqual(inventory(installed), baseline)

    def test_invalid_archive_preserves_existing_install(self):
        installed = self.run_build()
        baseline = inventory(installed)
        bad = self.root / "bad.tar.gz"
        for name, member_type, error in (
            ("superpowers-" + LOCK["commit"] + "/../outside", tarfile.REGTYPE, "unsafe archive path"),
            ("/absolute", tarfile.REGTYPE, "unsafe archive path"),
            ("superpowers-" + LOCK["commit"] + "/skills/unsafe", tarfile.SYMTYPE, "unsupported archive entry"),
        ):
            with self.subTest(name=name, member_type=member_type):
                with tarfile.open(bad, "w:gz") as archive:
                    member = tarfile.TarInfo(name)
                    member.type = member_type
                    member.linkname = "../outside"
                    archive.addfile(member, io.BytesIO())
                updated = dict(LOCK, sha256=hashlib.sha256(bad.read_bytes()).hexdigest())
                (self.source / "upstream.lock").write_text(json.dumps(updated), encoding="utf-8")
                self.run_build(archive=bad, error=error)
                self.assertEqual(inventory(installed), baseline)
                self.assertFalse((self.root / "outside").exists())

    def test_patch_order_and_failed_patch_preserve_existing_install(self):
        installed = self.run_build()
        first = self.patches / "02-add.patch"
        first.write_text(
            "diff --git a/skills/using-theseus/extra.md b/skills/using-theseus/extra.md\n"
            "new file mode 100644\n--- /dev/null\n+++ b/skills/using-theseus/extra.md\n"
            "@@ -0,0 +1 @@\n+before\n",
            encoding="utf-8",
        )
        second = self.patches / "01-edit.patch"
        second.write_text(
            "diff --git a/skills/using-theseus/extra.md b/skills/using-theseus/extra.md\n"
            "--- a/skills/using-theseus/extra.md\n+++ b/skills/using-theseus/extra.md\n"
            "@@ -1 +1 @@\n-before\n+after\n",
            encoding="utf-8",
        )
        self.series("02-add.patch\n01-edit.patch\n")
        self.run_build()
        self.assertEqual((installed / "skills/using-theseus/extra.md").read_text(), "after\n")
        manifest = json.loads((installed / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([item["path"] for item in manifest["patches"]], [first.name, second.name])
        baseline = inventory(installed)
        self.series("01-edit.patch\n02-add.patch\n")
        self.run_build(error="patch failed: 01-edit.patch")
        self.assertEqual(inventory(installed), baseline)

    def test_unrelated_or_modified_output_is_preserved(self):
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        marker = unrelated / "leave-me"
        marker.write_text("user data", encoding="utf-8")
        self.run_build(output=unrelated, error="output is not a Theseus installation")
        self.assertEqual(marker.read_text(encoding="utf-8"), "user data")

        installed = self.run_build()
        empty_dir = installed / "empty"
        empty_dir.mkdir()
        self.run_build(output=installed, error="output has been modified")
        self.assertTrue(empty_dir.is_dir())
        empty_dir.rmdir()
        modified = installed / "leave-me"
        modified.write_text("user data", encoding="utf-8")
        self.run_build(output=installed, error="output has been modified")
        self.assertEqual(modified.read_text(encoding="utf-8"), "user data")


if __name__ == "__main__":
    unittest.main()
