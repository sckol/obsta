import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from obsta import assets, cli


class Arc67InitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / 'vault with spaces'
        self.root.mkdir()
        self.home = Path(directory.name).resolve() / 'home'

    def initialize(self):
        self.assertEqual(cli.main(['--home', str(self.home), 'arc67init', str(self.root)]), 0)

    def test_new_vault_and_idempotence(self):
        self.initialize()
        self.assertEqual(
            {path.name for path in self.root.iterdir() if path.is_dir()},
            {'Presentations', 'Guardrails', 'References', 'Plans', 'Sources',
             'Scenarios', 'Configs', 'Conventions', 'Decisions', 'Reports',
             'Todos', 'Glossary', '.obsidian'},
        )
        scaffold = (self.root / 'Architecture.md').read_text()
        self.assertIn('### Quality Goals', scaffold)
        self.assertIn('## Architecture Decisions', scaffold)
        self.assertIn('![[Glossary.base]]', scaffold)
        self.assertIn('file.inFolder("Glossary")', (self.root / 'Glossary.base').read_text())
        shipped = {path.name: path.read_bytes() for path in assets.template_source().glob('*.md')}
        self.assertIn('Building block.md', shipped)
        self.assertEqual(
            {path.name: path.read_bytes() for path in (self.root / 'Conventions/Templates').iterdir()},
            shipped,
        )
        before = {path: path.stat().st_mtime_ns for path in self.root.rglob('*') if path.is_file()}
        self.initialize()
        self.assertEqual(before, {path: path.stat().st_mtime_ns for path in before})
        self.assertFalse(self.home.exists())

    def test_existing_vault_and_template_refresh(self):
        architecture = self.root / 'Architecture.md'
        glossary = self.root / 'Glossary.base'
        architecture.write_text('My architecture')
        glossary.write_text('My glossary')
        (self.root / 'TODOs').mkdir()
        templates = self.root / 'Conventions/Templates'
        templates.mkdir(parents=True)
        (templates / 'Building block.md').write_text('Old template')
        (templates / 'Local.md').write_text('My template')
        settings = self.root / '.obsidian'
        settings.mkdir()
        options = settings / 'templates.json'
        options.write_text('{"folder": "Local", "dateFormat": "YYYY"}')
        plugins = settings / 'core-plugins.json'
        plugins.write_text('["daily-notes"]')
        with patch.object(assets.sys, 'platform', 'linux'):
            assets.install(self.root)
        self.assertEqual((templates / 'Building block.md').read_text(), 'Old template')
        self.assertEqual(json.loads(options.read_text())['folder'], 'Local')
        self.assertFalse((self.root / 'Glossary').exists())

        self.initialize()
        self.assertEqual((templates / 'Building block.md').read_bytes(),
                         (assets.template_source() / 'Building block.md').read_bytes())
        self.assertEqual((templates / 'Local.md').read_text(), 'My template')
        self.assertEqual(architecture.read_text(), 'My architecture')
        self.assertEqual(glossary.read_text(), 'My glossary')
        self.assertEqual(len([path for path in self.root.iterdir()
                              if path.name.casefold() == 'todos']), 1)
        self.assertEqual(json.loads(options.read_text()),
                         {'folder': 'Conventions/Templates', 'dateFormat': 'YYYY'})
        self.assertEqual(json.loads(plugins.read_text()), ['daily-notes', 'templates', 'bases'])

    def test_shared_skill_installs_without_replacing_user_skills(self):
        cli.prepare(self.home)
        skills = self.home / 'codex/skills'
        user_skill = skills / 'personal'
        user_skill.mkdir()
        with patch.object(assets, 'install_theseus'):
            assets.install_skills(self.home)
            installed = skills / 'arc67init/SKILL.md'
            self.assertEqual(installed.read_bytes(), assets.arc67init_skill().read_bytes())
            self.assertTrue(user_skill.is_dir())
            assets.install_skills(self.home)
            self.assertTrue(installed.is_file())
