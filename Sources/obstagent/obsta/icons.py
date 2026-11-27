"""Cycle the icon selected by the native Obsidian app setting."""
import fcntl
import json
import logging
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from .assets import DATA, read_json, replace, update_json

DEFAULT_ICONS = tuple(json.loads((DATA / 'obsta-icons.json').read_text(encoding='utf-8')))
STATE_NAME = 'obsta-icon-state.json'


def palette(root):
    path = root / 'Configs/obsta-icons.json'
    try:
        icons = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(icons, list) or not icons or any(
            not isinstance(icon, str) or not icon.strip() for icon in icons
        ):
            raise ValueError('Expected a non-empty JSON array of non-blank strings')
        return tuple(icons)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as error:
        logging.warning('Could not load %s: %s', path, error)
    return DEFAULT_ICONS


def state_path(root, workspace):
    return (workspace if workspace is not None else root / '.obsidian') / STATE_NAME


def advance(root, workspace, activation_id):
    if sys.platform != 'darwin':
        return
    path = state_path(root, workspace)
    if not path.parent.is_dir():
        return
    choices = palette(root)
    try:
        # Workers for different vaults publish to this shared cursor.
        with path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                previous = json.loads(path.read_text(encoding='utf-8'))
            except (FileNotFoundError, ValueError):
                previous = {}
            if not isinstance(previous, dict):
                previous = {}
            if previous.get('activation_id') == activation_id:
                return
            last = previous.get('emoji')
            index = previous.get('index')
            if type(index) is not int or not 0 <= index < len(choices) or choices[index] != last:
                index = choices.index(last) if last in choices else -1
            next_index = (index + 1) % len(choices)
            update_json(path, {'emoji': choices[next_index], 'index': next_index, 'activation_id': activation_id})
    except (OSError, ValueError) as error:
        logging.warning('Could not publish Obsidian icon: %s', error)


def native_icon(root, workspace):
    if sys.platform == 'darwin':
        profile = Path.home() / 'Library/Application Support/obsidian'
        return NativeIcon(state_path(root, workspace), profile)


def obsidian_running():
    status = subprocess.run(['/usr/bin/pgrep', '-x', 'Obsidian'],
                            stdout=subprocess.DEVNULL, timeout=5, check=False)
    if status.returncode not in (0, 1):
        raise RuntimeError('Could not determine whether Obsidian is running')
    return status.returncode == 0


class NativeIcon:
    filename = 'obsta-app-icon.png'

    def __init__(self, queue, profile):
        self.queue = queue
        self.settings = profile / 'obsidian.json'
        self.image = profile / self.filename
        self.journal = profile / 'obsta-app-icon.json'
        self.applied = None
        self.rendered = None
        self.restarting = False

    def refresh(self):
        try:
            state = read_json(self.queue)
            activation = state.get('activation_id')
            emoji = state.get('emoji')
            if isinstance(activation, str) and isinstance(emoji, str) and emoji and activation != self.applied:
                self.update(state)
            elif not state and self.journal.is_file() and not obsidian_running():
                self.update({})
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            logging.warning('Could not update Obsidian app icon: %s', error)

    def reset(self):
        try:
            self.queue.unlink(missing_ok=True)
            self.applied = None
            if self.journal.is_file():
                self.update({})
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            logging.warning('Could not restore Obsidian app icon: %s', error)

    def close(self):
        if not self.restarting:
            self.reset()

    def render(self, emoji):
        if self.rendered is None or self.rendered[0] != emoji:
            with TemporaryDirectory() as folder:
                image = Path(folder) / 'icon.png'
                subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', str(DATA / 'app-icon.js'),
                                str(image), emoji], check=True, capture_output=True, timeout=10)
                self.rendered = emoji, image.read_bytes()
        return self.rendered[1]

    def update(self, state):
        if not self.settings.is_file():
            return
        with self.journal.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            settings = read_json(self.settings)
            journal = read_json(self.journal)
            owner = journal.get('queue')
            if owner is not None and not isinstance(owner, str):
                raise ValueError('Invalid Obsidian app icon journal')
            if not state:
                if owner == str(self.queue):
                    self.restore(settings, journal)
                return
            if owner and owner != str(self.queue) and Path(owner).is_file():
                return

            selected = settings.get('icon')
            if selected == self.filename:
                if not owner:
                    return
            else:
                journal = {'original': selected}
            # Prepare the PNG while waiting, so only file replacement remains
            # when the first launch can safely be configured.
            image = self.render(state['emoji'])
            # Obsidian keeps its preferences in memory until the process exits.
            if selected != self.filename and (obsidian_running() or read_json(self.settings) != settings):
                return
            update_json(self.journal, {**journal, 'queue': str(self.queue)})
            replace(self.image, image)
            if selected != self.filename:
                if obsidian_running() or read_json(self.settings) != settings:
                    return
                update_json(self.settings, {**settings, 'icon': self.filename})
            self.applied = state['activation_id']

    def restore(self, settings, journal):
        if settings.get('icon') == self.filename:
            original = journal.get('original')
            if obsidian_running():
                # Prepare the next launch without rewriting the live preferences.
                if isinstance(original, str) and Path(original).name == original:
                    source = self.image.with_name(original)
                    if source.is_file() and source != self.image:
                        replace(self.image, source.read_bytes())
                        return
                self.image.unlink(missing_ok=True)
                return
            if read_json(self.settings) != settings or obsidian_running():
                return
            if original is None:
                settings.pop('icon')
            else:
                settings['icon'] = original
            update_json(self.settings, settings)
        self.image.unlink(missing_ok=True)
        self.journal.unlink(missing_ok=True)
