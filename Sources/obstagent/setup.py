import json
from pathlib import Path
from shutil import copyfile, copytree
import subprocess
import sys

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.editable_wheel import editable_wheel
from setuptools.command.sdist import sdist

PROJECT = Path(__file__).resolve().parent
VAULT = PROJECT.parent.parent
THESEUS = Path('obsta/data/theseus')


def include_templates(destination):
    source = VAULT / 'Conventions' / 'Templates'
    if source.is_dir():
        target = destination / 'obsta/data/templates'
        target.mkdir(parents=True, exist_ok=True)
        for template in source.glob('*.md'):
            copyfile(template, target / template.name)
    elif not (PROJECT / 'obsta/data/templates').is_dir():
        raise RuntimeError('Obsidian templates are missing from the source distribution')


def include_arc67init(destination):
    relative = Path('obsta/data/arc67init/SKILL.md')
    source = VAULT / 'Sources/arc67init/SKILL.md'
    if not source.is_file():
        source = PROJECT / relative
    if not source.is_file():
        raise RuntimeError('arc67init skill is missing from the source distribution')
    target = destination / relative
    if source != target.resolve():
        target.parent.mkdir(parents=True, exist_ok=True)
        copyfile(source, target)


def include_theseus(destination):
    builder = VAULT / 'Sources/theseus/build.py'
    target = destination.resolve() / THESEUS
    if builder.is_file():
        pin = json.loads(builder.with_name('upstream.lock').read_text(encoding='utf-8'))
        archive = VAULT / 'References' / f"superpowers-{pin['commit']}.tar.gz"
        subprocess.run([sys.executable, str(builder), '--archive', str(archive),
                        '--output', str(target)], check=True)
    elif (PROJECT / THESEUS / 'manifest.json').is_file():
        if target != PROJECT / THESEUS:
            copytree(PROJECT / THESEUS, target, dirs_exist_ok=True)
    else:
        raise RuntimeError('Theseus skill bundle is missing from the source distribution')


class Build(build_py):
    def run(self):
        super().run()
        destination = Path(self.build_lib)
        include_templates(destination)
        include_arc67init(destination)
        include_theseus(destination)


class SourceDistribution(sdist):
    def make_release_tree(self, base_dir, files):
        super().make_release_tree(base_dir, files)
        destination = Path(base_dir)
        include_templates(destination)
        include_arc67init(destination)
        include_theseus(destination)


class EditableWheel(editable_wheel):
    def run(self):
        include_theseus(PROJECT)
        super().run()


setup(cmdclass={'build_py': Build, 'sdist': SourceDistribution, 'editable_wheel': EditableWheel})
