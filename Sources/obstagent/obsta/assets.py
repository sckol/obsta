"""Install bundled resources without clobbering vault customizations."""
import hashlib
import json
import logging
from pathlib import Path
from shutil import copytree
from tempfile import NamedTemporaryFile, TemporaryDirectory

DATA = Path(__file__).resolve().parent / 'data'
TEMPLATES = 'Conventions/Templates'
THESEUS_BUNDLE = DATA / 'theseus'
MANAGED_SKILLS = Path('codex/skills/obsta-theseus')
ARC67INIT = 'arc67init'


def arc67init_skill():
    source = DATA.parents[2] / ARC67INIT / 'SKILL.md'
    return source if source.is_file() else DATA / ARC67INIT / 'SKILL.md'


def template_source():
    for parent in DATA.parents:
        if parent.name == 'Sources' and (parent.parent / TEMPLATES).is_dir():
            return parent.parent / TEMPLATES
    return DATA / 'templates'


def replace(path, data):
    with NamedTemporaryFile(dir=path.parent, prefix='.' + path.name + '-', delete=False) as temp:
        temp_path = Path(temp.name)
        try:
            temp.write(data)
            temp.close()
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)


def update_json(path, value):
    replace(path, (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode())


def read_json(path):
    if path.is_symlink() or path.exists() and not path.is_file():
        raise RuntimeError(f'Expected a regular file: {path}')
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise RuntimeError(f'Expected a JSON object: {path}')
    return value


def checked_folder(root, relative):
    for part in Path(relative).parts:
        root /= part
        if root.is_symlink():
            raise RuntimeError(f'Will not install resources through a symlink: {root}')
        root.mkdir(exist_ok=True)
    return root


def install_skills(home):
    install_theseus(home)
    source = arc67init_skill()
    if not source.is_file():
        raise RuntimeError('arc67init skill is missing; reinstall the obsta package')
    data = source.read_bytes()
    store = checked_folder(home, 'codex/.obsta-arc67init')
    target = checked_folder(store, hashlib.sha256(data).hexdigest())
    link = home / 'codex/skills/arc67init'
    check_managed_link(link, store)
    skill = target / 'SKILL.md'
    if not skill.is_file() or skill.read_bytes() != data:
        replace(skill, data)
    publish_skill_link(link, target)


def install_theseus(home):
    manifest = THESEUS_BUNDLE / 'manifest.json'
    entry = Path('skills/using-theseus/SKILL.md')
    if not manifest.is_file() or not (THESEUS_BUNDLE / entry).is_file():
        raise RuntimeError('Theseus skill bundle is missing; reinstall the obsta package')
    metadata = manifest.read_bytes()
    revision = hashlib.sha256(metadata).hexdigest()
    store = checked_folder(home, 'codex/.obsta-theseus')
    bundle = store / revision
    link = home / MANAGED_SKILLS
    target = bundle / 'skills'
    if link.is_symlink():
        previous = link.resolve()
        if previous.name != 'skills' or previous.parent.parent != store:
            raise RuntimeError(f'Skill directory is already in use: {link}')
        if previous == target and (bundle / entry).is_file():
            return
    elif link.exists():
        raise RuntimeError(f'Skill directory is already in use: {link}')

    if not (bundle / entry).is_file():
        with TemporaryDirectory(dir=store) as directory:
            staged = Path(directory) / 'bundle'
            copytree(THESEUS_BUNDLE, staged)
            files = json.loads(metadata)['files']
            if ((staged / 'manifest.json').read_bytes() != metadata
                    or any(hashlib.sha256((staged / path).read_bytes()).hexdigest() != info['sha256']
                           for path, info in files.items())):
                raise RuntimeError('Theseus skill bundle changed during installation')
            staged.rename(bundle)

    publish_skill_link(link, target)


def check_managed_link(link, store):
    if link.is_symlink():
        if link.resolve().parent == store:
            return
    elif not link.exists():
        return
    raise RuntimeError(f'Skill directory is already in use: {link}')


def publish_skill_link(link, target):
    if link.is_symlink() and link.resolve() == target:
        return
    with TemporaryDirectory(dir=link.parent) as directory:
        staged = Path(directory) / 'skills'
        staged.symlink_to(target, target_is_directory=True)
        staged.replace(link)


def install_templates(root, *, explicit=False):
    target = checked_folder(root, TEMPLATES)
    settings = checked_folder(root, '.obsidian')
    source_dir = template_source()
    sources = sorted(source_dir.glob('*.md'))
    if explicit and not sources:
        raise RuntimeError(f'No arc67 templates found in {source_dir}')
    manifest = settings / 'obsta-assets.json'
    previous = read_json(manifest)
    installed = {}
    for source in sources:
        path = target / source.name
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if path.is_symlink() or (path.exists() and not path.is_file()):
            if explicit:
                raise RuntimeError(f'Expected a regular template file: {path}')
            logging.warning('Custom template preserved: %s', path)
            continue
        if not path.exists():
            replace(path, data)
        else:
            current = hashlib.sha256(path.read_bytes()).hexdigest()
            if current != digest:
                if not explicit and current != previous.get(source.name):
                    logging.warning('Custom template preserved: %s', path)
                    continue
                replace(path, data)
        installed[source.name] = digest

    templates = settings / 'templates.json'
    options = read_json(templates)
    if not options.get('folder') or (explicit and options['folder'] != TEMPLATES):
        options['folder'] = TEMPLATES
        update_json(templates, options)
    elif options['folder'] != TEMPLATES:
        logging.warning('Custom Obsidian templates folder preserved in %s: %s', root, options['folder'])

    plugins = settings / 'core-plugins.json'
    if explicit or plugins.exists() or plugins.is_symlink():
        enable_plugins(plugins, ('templates', 'bases') if explicit else ('templates',))
    if previous != installed:
        update_json(manifest, installed)


def enable_plugins(path, names):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(f'Expected a regular file: {path}')
    raw = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if isinstance(raw, dict):
        updated = {**raw, **dict.fromkeys(names, True)}
    elif isinstance(raw, list):
        updated = [*raw, *(name for name in names if name not in raw)]
    else:
        raise RuntimeError(f'Unsupported Obsidian settings: {path}')
    if updated != raw:
        update_json(path, updated)
