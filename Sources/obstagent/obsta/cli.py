"""Shared registry and supervisor for independent vault workers."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
from importlib.metadata import PackageNotFoundError, distribution
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import tomllib

from . import assets, icons, worker

MODULE = Path(__file__).resolve().parent
SHELL = MODULE / 'obstagent.sh'
POLL_INTERVAL = 0.5
RETRY_DELAY = 5


def read_config(home):
    path = home / 'config.toml'
    if not path.exists():
        return [], {'subagent': 'codex'}
    with path.open('rb') as stream:
        value = tomllib.load(stream)
    vaults = value.get('vaults', [])
    defaults = value.get('defaults', {})
    registered = value.get('registered', {})
    if not isinstance(registered, dict) or any(
            not isinstance(item, dict) or not isinstance(item.get('path'), str)
            or not Path(item['path']).is_absolute() for item in registered.values()):
        raise ValueError('config.toml: each registered vault needs an absolute path')
    if not isinstance(vaults, list) or any(not isinstance(item, str) or not Path(item).is_absolute() for item in vaults):
        raise ValueError('config.toml: vaults must be an array of absolute paths')
    if not isinstance(defaults, dict) or any(not isinstance(item, str) for item in defaults.values()):
        raise ValueError('config.toml: defaults must be a table of strings')
    paths = [*vaults, *(item['path'] for item in registered.values())]
    return list(dict.fromkeys(Path(path).resolve() for path in paths)), {'subagent': 'codex', **defaults}


@contextmanager
def locked(path, non_blocking=False):
    with path.open('a') as lock:
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if non_blocking else 0)
        try:
            fcntl.flock(lock, flags)
        except BlockingIOError as error:
            raise RuntimeError('obsta is already running in this workspace') from error
        yield


def git_common(root):
    if Path(worker.git(root, 'rev-parse', '--show-toplevel').strip()).resolve() != root:
        raise ValueError(f'Vault must be a Git repository root: {root}')
    return (root / worker.git(root, 'rev-parse', '--git-common-dir').strip()).resolve()


def prepare(home):
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    codex = home / 'codex'
    codex.mkdir(mode=0o700, exist_ok=True)
    for path in (codex / 'skills', home / 'shared'):
        path.mkdir(exist_ok=True)
    with locked(home / 'config.lock'):
        config = home / 'config.toml'
        if not config.exists():
            assets.replace(config, b'vaults = []\n\n[defaults]\nsubagent = "codex"\n')


def add(home, root):
    with locked(home / 'config.lock'):
        roots, _ = read_config(home)
        common = git_common(root)
        if root in roots:
            assets.install_templates(root)
            return
        for other in roots:
            try:
                other_common = git_common(other)
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
            if common == other_common:
                raise ValueError(f'Vault shares a Git repository with {other}: {root}')
        assets.install_templates(root)
        config = home / 'config.toml'
        # A separate TOML table per path lets registration preserve comments and defaults.
        key = hashlib.sha256(os.fsencode(root)).hexdigest()
        entry = f'\n\n[registered.{key}]\npath = {json.dumps(str(root), ensure_ascii=False)}\n'
        assets.replace(config, config.read_bytes() + entry.encode())


def fingerprint():
    files = [*MODULE.glob('*.py'), SHELL, *MODULE.joinpath('data').glob('*'),
             *assets.template_source().glob('*.md'), assets.THESEUS_BUNDLE / 'manifest.json',
             assets.arc67init_skill()]
    digest = hashlib.sha256()
    metadata = MODULE.parent / 'pyproject.toml'
    if metadata.is_file():
        files.extend([metadata, MODULE.parent / 'setup.py'])
    else:
        try:
            installed = distribution('obsta').read_text('METADATA')
        except PackageNotFoundError:
            pass
        else:
            if installed:
                digest.update(installed.encode())
    for path in sorted(set(files)):
        if path.is_file():
            digest.update(os.fsencode(path) + b'\0' + hashlib.sha256(path.read_bytes()).digest())
    return digest.digest()


def python_command():
    return [sys.executable, '-P', '-B', '-X', f'pycache_prefix={MODULE / ".obsta-no-cache"}']


class Supervisor:
    def __init__(self, home, interval, settle, timeout):
        self.home = home
        self.workers = {}
        self.common_dirs = {}
        self.failures = {}
        self.options = ['--interval', str(interval), '--settle', str(settle)]
        if timeout is not None:
            self.options += ['--timeout', str(timeout)]
        self.stopping = False

    def shutdown(self, *_):
        self.stopping = True

    def sync(self, start_workers=True):
        with locked(self.home / 'config.lock'):
            roots, _ = read_config(self.home)
            for root, child in list(self.workers.items()):
                code = child.poll()
                if code is not None:
                    self.workers.pop(root)
                    self.common_dirs.pop(root)
                    if root in roots:
                        logging.warning('Vault worker exited (%s): %s', code, root)
                        self.failures[root] = time.monotonic() + RETRY_DELAY
                elif root not in roots and root not in self.failures:
                    child.terminate()
                    self.failures[root] = time.monotonic() + RETRY_DELAY
            for root in set(self.failures) - set(roots) - self.workers.keys():
                self.failures.pop(root)
            if not start_workers:
                return
            commons = set(self.common_dirs.values())
            for root in roots:
                if root in self.workers or self.failures.get(root, 0) > time.monotonic():
                    continue
                try:
                    common = git_common(root)
                    if common in commons:
                        raise ValueError(f'Another vault uses the same Git repository: {root}')
                    assets.install_templates(root)
                    command = [*python_command(), str(MODULE / 'worker.py'), '--vault', str(root),
                               '--workspace', str(self.home), *self.options]
                    self.workers[root] = subprocess.Popen(command, cwd=self.home, start_new_session=True)
                    self.failures.pop(root, None)
                    self.common_dirs[root] = common
                    commons.add(common)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    logging.error('Could not start vault %s: %s', root, error)
                    self.failures[root] = time.monotonic() + RETRY_DELAY

    def validate(self, candidate):
        try:
            if not (assets.THESEUS_BUNDLE / 'manifest.json').is_file():
                raise RuntimeError('Theseus skill bundle is missing from the update')
            if not assets.arc67init_skill().is_file():
                raise RuntimeError('arc67init skill is missing from the update')
            subprocess.run(['/bin/sh', '-n', str(SHELL)], check=True, capture_output=True, text=True, timeout=5)
            test = subprocess.run([*python_command(), '-m', 'obsta', '--self-test'],
                                  cwd=self.home, capture_output=True, text=True,
                                  timeout=worker.RESTART_TEST_TIMEOUT)
            if test.returncode or fingerprint() != candidate:
                raise RuntimeError((test.stderr or 'Package changed during self-test')[-2500:])
            return True
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            logging.error('Update rejected: %s', error)
            return False

    def drain(self):
        for child in self.workers.values():
            if child.poll() is None:
                child.terminate()
        for child in self.workers.values():
            child.wait()
        self.workers.clear()
        self.common_dirs.clear()

    def run(self):
        loaded = fingerprint()
        rejected = None
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)
        app_icon = icons.native_icon(None, self.home)
        try:
            while not self.stopping:
                try:
                    candidate = fingerprint()
                    if candidate not in (loaded, rejected):
                        if self.validate(candidate) and not self.stopping:
                            logging.info('Update validated; waiting for vault workers to finish')
                            self.drain()
                            if self.stopping:
                                return 0
                            if fingerprint() == candidate:
                                if app_icon:
                                    app_icon.restarting = True
                                return worker.RESTART_EXIT
                            continue
                        rejected = candidate
                    self.sync(start_workers=candidate == loaded)
                    if app_icon:
                        app_icon.refresh()
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    logging.error('Supervisor could not read workspace state: %s', error)
                time.sleep(POLL_INTERVAL)
            return 0
        finally:
            try:
                self.drain()
            finally:
                if app_icon:
                    app_icon.close()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('OBSTA_HOME', '~/.obsta')))
    check = parser.add_mutually_exclusive_group()
    check.add_argument('--self-test', action='store_true')
    check.add_argument('--self-test-full', action='store_true')
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('init', help='initialize the common workspace')
    register = commands.add_parser('add', help='register a Git vault')
    register.add_argument('vault', type=Path)
    arc67 = commands.add_parser('arc67init', help='initialize the arc67 structure in a vault')
    arc67.add_argument('vault', type=Path)
    commands.add_parser('list', help='list registered vaults')
    start = commands.add_parser('start', help='watch all registered vaults')
    start.add_argument('--interval', type=float, default=0.5)
    start.add_argument('--settle', type=float, default=1.0)
    start.add_argument('--timeout', type=float)
    args = parser.parse_args(argv)
    if args.self_test or args.self_test_full:
        return worker.main(['--self-test-full' if args.self_test_full else '--self-test'])
    if not args.command:
        parser.print_help()
        return 0
    home = args.home.expanduser().resolve()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        if args.command == 'arc67init':
            from .arc67init import initialize

            initialize(args.vault.expanduser().resolve())
            return 0
        if args.command == 'start' and os.environ.get('OBSTA_SUPERVISED') != '1':
            env = dict(os.environ, OBSTA_PYTHON=sys.executable)
            os.execve('/bin/sh', ['/bin/sh', str(SHELL), *argv], env)
        prepare(home)
        if args.command == 'init':
            with locked(home / 'config.lock'):
                assets.install_skills(home)
        elif args.command == 'add':
            add(home, args.vault.expanduser().resolve())
        elif args.command == 'list':
            for root in read_config(home)[0]:
                print(root)
        elif args.command == 'start':
            if args.interval <= 0 or args.settle < 0 or (args.timeout is not None and args.timeout <= 0):
                parser.error('interval and timeout must be positive; settle must be nonnegative')
            with locked(home / 'supervisor.lock', non_blocking=True):
                with locked(home / 'config.lock'):
                    assets.install_skills(home)
                return Supervisor(home, args.interval, args.settle, args.timeout).run()
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        logging.error('%s', error)
        return 1
