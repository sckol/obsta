#!/usr/bin/env python3
"""Poll an Obsidian vault and dispatch one Markdown ticket at a time.

Run: python3 Sources/obsta.py [--once | --self-test]. Python 3.9+.
"""
import argparse
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

INSTRUCTION = Path('Conventions/Agent Instruction.md')
EXCLUDED = {'.git', '.obsidian', '.codex', '.agents', '__pycache__'}
RESTART_EXIT = 75
RESTART_TEST_TIMEOUT = 60
TICKET = re.compile(r'#(\d+)🎬')


def _mask_code(line):
    return re.sub(r'(`+).*?\1', lambda m: ' ' * len(m[0]), line)


def _lines(text):
    """Yield lines with offsets, excluding YAML frontmatter and fenced code."""
    offset = 0
    fence = None
    frontmatter = text.startswith('---\n')
    for number, line in enumerate(text.splitlines(keepends=True)):
        stripped = line.lstrip()
        if frontmatter:
            if number and stripped.strip() == '---':
                frontmatter = False
            offset += len(line)
            continue
        fence_match = re.match(r'(`{3,}|~{3,})', stripped)
        if fence_match:
            token = fence_match.group(1)
            if fence is None:
                fence = token[0]
            elif token[0] == fence and len(token) >= 3:
                fence = None
            offset += len(line)
            continue
        if fence is None:
            yield offset, line
        offset += len(line)


def find_commands(text):
    """Return actionable bold spans and checked list items in document order."""
    found = []
    for offset, line in _lines(text):
        masked = _mask_code(line)
        # Don't activate text within existing tickets or italic agent messages.
        ticket_ranges = [(m.start(), line.find('#' + m.group(1) + '🛑', m.end()))
                         for m in TICKET.finditer(line)]
        ticket_ranges = [(a, b if b >= 0 else len(line)) for a, b in ticket_ranges]
        def blocked(pos):
            return any(a <= pos < b for a, b in ticket_ranges)
        for m in re.finditer(r'(?<![\\*_])(?P<mark>\*\*|__)(?![*_\s])(?P<body>.+?)(?<!\s)(?P=mark)(?![*_])', masked):
            if m['body'].strip() and not m['body'].startswith(('*', '_')) and not blocked(m.start()):
                found.append((offset + m.start(), offset + m.end(), line[m.start()+2:m.end()-2], 'bold'))
        check = re.match(r'^(\s*)([-*+]\s+)\[x\](\s+)(.*?)(\r?\n)?$', line, re.I)
        if check and check[4].strip() and not blocked(0):
            start = offset + len(check[1])
            found.append((start, offset + len(line.rstrip('\r\n')), check[4], 'checklist'))
    return sorted(found)


def notes(root):
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not d.startswith('.')
                         and not (Path(base) / d).is_symlink())
        for name in sorted(files):
            path = Path(base) / name
            if (path.suffix == '.md' and not path.is_symlink()
                    and path not in {root / INSTRUCTION, Path(__file__).resolve()}):
                yield path


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True).stdout


def atomic_replace(path, before, after):
    if path.read_text(encoding='utf-8') != before:
        raise RuntimeError('Note changed before ticket activation')
    temp = path.with_name(path.name + '.obsta-tmp')
    try:
        temp.write_text(after, encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def ticket_boundary(text, start, end, kind):
    """Select inline bold text or a checklist item and its indented descendants."""
    lines = text.splitlines(keepends=True)
    offset = 0
    first = last = None
    for i, line in enumerate(lines):
        next_offset = offset + len(line)
        if offset <= start < next_offset:
            first = i
        if offset < end <= next_offset or end == offset == next_offset:
            last = i
            break
        offset = next_offset
    if first is None:
        raise RuntimeError('Could not locate command in note')
    if kind != 'checklist':
        return start, end
    parent_indent = len(lines[first]) - len(lines[first].lstrip())
    last = first
    for i in range(first + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            last = i
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= parent_indent:
            break
        last = i
    start_offset = sum(map(len, lines[:first]))
    end_offset = sum(map(len, lines[:last + 1]))
    return start_offset, end_offset


def allocate_id(db):
    row = db.execute('INSERT INTO tickets(created_at) VALUES (?) RETURNING id',
                     (time.time(),)).fetchone()
    db.commit()
    return row[0]


class Watcher:
    def __init__(self, root, settle=1.0, timeout=1800, interval=0.5, runner=subprocess.run):
        self.root = root.resolve()
        self.settle, self.timeout, self.interval = settle, timeout, interval
        self.runner = runner
        self.seen = {}
        self.source = Path(__file__).resolve()
        self.source_hash = hashlib.sha256(self.source.read_bytes()).digest()
        self.last_ticket_success = False
        self.verified_head = None
        self.result_paths = set()
        top = Path(git(self.root, 'rev-parse', '--show-toplevel').strip()).resolve()
        if top != self.root:
            raise RuntimeError('Vault must be the Git repository root')
        if not (self.root / INSTRUCTION).is_file():
            raise RuntimeError('Missing Agent Instruction.md')
        key = hashlib.sha256(str(self.root).encode()).hexdigest()[:20]
        self.db_path = Path(tempfile.gettempdir()) / ('obsta-' + key + '.sqlite3')
        self.db = sqlite3.connect(self.db_path)
        self.db.execute('CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL)')
        self.db.commit()

    def close(self):
        self.db.close()

    def _check_worktree(self):
        unfinished = []
        dirty = git(self.root, 'status', '--porcelain', '-z', '--no-renames')
        for entry in filter(None, dirty.split('\0')):
            name = entry[3:]
            if (entry[:2] == ' M' and name.endswith('.md')
                    and name not in self.result_paths):
                logging.warning('Concurrent note edit left untouched: %s', name)
            else:
                unfinished.append(entry)
        if unfinished:
            raise RuntimeError('uncommitted changes: ' + '; '.join(unfinished))

    def restart_requested(self):
        """Validate changed source before requesting a restart of this process."""
        candidate = hashlib.sha256(self.source.read_bytes()).digest()
        if candidate == self.source_hash:
            return False
        if not self.last_ticket_success:
            raise RuntimeError('Source changed without a successfully verified ticket')
        if git(self.root, 'rev-parse', 'HEAD').strip() != self.verified_head:
            raise RuntimeError('HEAD changed after ticket verification')
        self._check_worktree()
        result = subprocess.run([sys.executable, str(self.source), '--self-test'],
                                cwd=self.root, capture_output=True, text=True,
                                timeout=RESTART_TEST_TIMEOUT)
        if result.returncode:
            raise RuntimeError('Updated source self-test failed: '
                               + (result.stderr or result.stdout or '')[-2000:])
        if hashlib.sha256(self.source.read_bytes()).digest() != candidate:
            raise RuntimeError('Source changed during restart self-test')
        if git(self.root, 'rev-parse', 'HEAD').strip() != self.verified_head:
            raise RuntimeError('HEAD changed during restart self-test')
        self._check_worktree()
        logging.info('Updated source passed self-test; requesting restart (exit %s)',
                     RESTART_EXIT)
        return True

    def scan(self):
        now = time.monotonic()
        available = set()
        for path in notes(self.root):
            available.add(path)
            try:
                text = path.read_text(encoding='utf-8')
            except (OSError, UnicodeError):
                continue
            old, since = self.seen.get(path, (None, now))
            if old != text:
                self.seen[path] = (text, now)
                continue
            if now - since < self.settle:
                continue
            commands = find_commands(text)
            if commands:
                if not self.process(path, text, commands[0]):
                    raise RuntimeError('Ticket failed; watcher stopped')
                self.seen.clear()
                return True
        self.seen = {p: v for p, v in self.seen.items() if p in available}
        return False

    def process(self, path, text, command):
        self.last_ticket_success = False
        start, end, body, kind = command
        begin, finish = ticket_boundary(text, start, end, kind)
        ticket_id = allocate_id(self.db)
        ticket_body = text[begin:finish]
        if kind == 'bold':
            ticket_body = re.sub(r'^(\*\*|__)(.*)\1$', r'\2', ticket_body, count=1, flags=re.S)
        else:
            ticket_body = re.sub(r'^(\s*[-*+]\s+)\[x\](\s+)', r'\1[ ]\2', ticket_body, count=1)
        pending = f'⏳ {body} ⏳'
        replacement = f'#{ticket_id}🎬 {ticket_body.rstrip()} {pending} #{ticket_id}🛑'
        marked = text[:begin] + replacement + text[finish:]
        surrounding = marked.replace(replacement, f'[[TICKET {ticket_id}]]', 1)
        human_diff = git(self.root, 'diff', 'HEAD', '--', '.')
        atomic_replace(path, text, marked)
        # Commit all current human edits, including ticket activation.
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-m', f'(human) ticket {ticket_id}: {body[:60]}')
        head = git(self.root, 'rev-parse', 'HEAD').strip()
        instruction = (self.root / INSTRUCTION).read_text(encoding='utf-8')
        if not instruction.strip():
            raise RuntimeError('Empty agent instructions')
        context = {'ticket_id': ticket_id, 'note': str(path.relative_to(self.root)),
                   'ticket': replacement, 'comment': body,
                   'surrounding_text': surrounding,
                   'human_diff': human_diff,
                   'formatting': 'Plain text must be preserved. Agent prose must be italic.\n'
                                 'Only rewrite struck-through text and remove addressed %% comments.\n'
                                 'Append new prose only within the active ticket. Do not cross into another ticket.',
                   'environment': 'Work serially; do not create nested tickets.'}
        try:
            result = self.runner(['codex', 'exec', '-C', str(self.root),
                                  '-c', 'approval_policy="never"', '-'],
                                 input=instruction + '\n\n' + json.dumps(context, ensure_ascii=False),
                                 text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=self.timeout)
            if result.returncode:
                raise RuntimeError('Codex exited with code ' + str(result.returncode) + ': '
                                   + (result.stderr or result.stdout or '')[-600:])
            current = path.read_text(encoding='utf-8')
            subjects = git(self.root, 'log', '--reverse', '--format=%s', head + '..HEAD').splitlines()
            failures = []
            if pending in current:
                failures.append('pending marker remains')
            if f'✅ {body} ✅' not in current:
                failures.append('completion marker missing')
            if not subjects or not subjects[-1].startswith('(agent)'):
                failures.append('agent commit missing')
            # Other existing notes may be edited in Obsidian while Codex runs.
            # Git cannot identify the writer; only tolerate unstaged Markdown
            # modifications outside the active note and committed result paths.
            self.result_paths = set(git(self.root, 'diff-tree', '--no-commit-id',
                                   '--name-only', '--no-renames', '-r', '-z',
                                   head + '..HEAD').split('\0'))
            self.result_paths.add(str(path.relative_to(self.root)))
            try:
                self._check_worktree()
            except RuntimeError as error:
                failures.append(str(error))
            if failures:
                raise RuntimeError('; '.join(failures))
            self.verified_head = git(self.root, 'rev-parse', 'HEAD').strip()
            self.last_ticket_success = True
            return True
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            current = path.read_text(encoding='utf-8')
            before = current
            failed = f'❌ {body} ❌'
            if pending in current:
                current = current.replace(pending, failed, 1)
            detail = html.escape(' '.join(str(error).splitlines())).replace('*', '&#42;')
            closing = f'#{ticket_id}🛑'
            position = current.find(closing, current.find(f'#{ticket_id}🎬'))
            if position >= 0:
                message = f'\n*obsta: {type(error).__name__}: {detail}; task incomplete.*\n'
                updated = current[:position] + message + current[position:]
                atomic_replace(path, before, updated)
            logging.exception('Ticket %s failed; watcher will stop', ticket_id)
            return False


class Tests(unittest.TestCase):
    def test_signal_detection(self):
        text = '---\nx: **no**\n---\n```\n**no**\n```\n`**no**` **yes**\n- [x] run\n'
        self.assertEqual([x[2] for x in find_commands(text)], ['yes', 'run'])
        self.assertEqual(find_commands('***italic only*** and ~~strike~~'), [])

    def test_commands_inside_ticket_are_ignored(self):
        text = '#4🎬 **already assigned** #4🛑\n**new command**\n'
        commands = find_commands(text)
        self.assertEqual([command[2] for command in commands], ['new command'])

    def test_checklist_extent(self):
        note = '- [x] parent\n  - child\n    detail\n- next\n'
        cmd = find_commands(note)[0]
        a, b = ticket_boundary(note, *cmd[:2], cmd[3])
        self.assertEqual(note[a:b], '- [x] parent\n  - child\n    detail\n')

    def test_process_verification_with_concurrent_edits(self):
        cases = ('clean', 'other_note', 'active_note', 'result_note', 'code',
                 'untracked', 'deleted_note', 'missing_commit', 'pending', 'exit_error')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                git(root, 'init', '-q')
                git(root, 'config', 'user.name', 'obsta test')
                git(root, 'config', 'user.email', 'obsta-test@example.invalid')
                git(root, 'config', 'commit.gpgsign', 'false')
                git(root, 'config', 'core.hooksPath', '/dev/null')
                (root / INSTRUCTION).parent.mkdir()
                (root / INSTRUCTION).write_text('Test instructions', encoding='utf-8')
                path = root / 'task.md'
                path.write_text('**run**\nOutside ticket.\n', encoding='utf-8')
                other = root / 'Другая заметка.md'
                result_note = root / 'result.md'
                code = root / 'source.py'
                for target in (other, result_note, code):
                    target.write_text('original\n', encoding='utf-8')
                git(root, 'add', '-A')
                git(root, 'commit', '-qm', 'initial')

                def runner(*args, **kwargs):
                    context = json.loads(kwargs['input'].split('\n\n', 1)[1])
                    ticket_id = context['ticket_id']
                    if case != 'pending':
                        path.write_text(path.read_text(encoding='utf-8').replace(
                            '⏳ run ⏳', '✅ run ✅'), encoding='utf-8')
                    result_note.write_text('committed result\n', encoding='utf-8')
                    if case != 'missing_commit':
                        git(root, 'add', 'task.md', 'result.md')
                        git(root, 'commit', '-qm', f'(agent) ticket {ticket_id}: run')
                    target = {'other_note': other, 'active_note': path,
                              'result_note': result_note, 'code': code,
                              'untracked': root / 'new.md'}.get(case)
                    if target is not None:
                        with target.open('a', encoding='utf-8') as stream:
                            stream.write('concurrent edit\n')
                    if case == 'deleted_note':
                        other.unlink()
                    return subprocess.CompletedProcess(args[0],
                                                       1 if case == 'exit_error' else 0,
                                                       '', 'runner failure')

                # Use an in-memory ticket database; never touch the real watcher.
                watcher = Watcher.__new__(Watcher)
                watcher.root, watcher.runner, watcher.timeout = root, runner, 5
                watcher.db = sqlite3.connect(':memory:')
                watcher.db.execute('CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL)')
                try:
                    text = path.read_text(encoding='utf-8')
                    if case == 'clean':
                        outcome = watcher.process(path, text, find_commands(text)[0])
                    else:
                        level = 'WARNING' if case == 'other_note' else 'ERROR'
                        with self.assertLogs(level=level):
                            outcome = watcher.process(path, text, find_commands(text)[0])
                    current = path.read_text(encoding='utf-8')
                    success = case in ('clean', 'other_note')
                    self.assertEqual(outcome, success)
                    self.assertEqual(watcher.last_ticket_success, success)
                    self.assertEqual('task incomplete' not in current, success)
                    if success:
                        self.assertIn('✅ run ✅', current)
                        self.assertTrue(git(root, 'log', '-1', '--format=%s').startswith('(agent)'))
                    else:
                        self.assertLess(current.index('task incomplete'), current.index('#1🛑'))
                    self.assertTrue(current.endswith('Outside ticket.\n')
                                    or current.endswith('Outside ticket.\nconcurrent edit\n'))
                    if case == 'other_note':
                        self.assertEqual(other.read_text(encoding='utf-8'),
                                         'original\nconcurrent edit\n')
                        self.assertEqual(git(root, 'show', 'HEAD:Другая заметка.md'), 'original\n')
                        self.assertIn('Другая заметка.md', git(root, 'diff', '--name-only', '-z'))
                    if case == 'pending':
                        self.assertIn('❌ run ❌', current)
                finally:
                    watcher.close()

    def test_restart_validation(self):
        cases = ('unchanged', 'markdown', 'success', 'failed_ticket', 'dirty',
                 'failed_test', 'timeout', 'changed_during_test', 'changed_head',
                 'head_during_test', 'dirty_during_test', 'missing_source')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                git(root, 'init', '-q')
                git(root, 'config', 'user.name', 'obsta test')
                git(root, 'config', 'user.email', 'obsta-test@example.invalid')
                git(root, 'config', 'commit.gpgsign', 'false')
                git(root, 'config', 'core.hooksPath', '/dev/null')
                source = root / 'watcher.py'
                source.write_text('# original', encoding='utf-8')
                note = root / 'other.md'
                note.write_text('original', encoding='utf-8')
                git(root, 'add', '-A')
                git(root, 'commit', '-qm', 'initial')
                watcher = Watcher.__new__(Watcher)
                watcher.root, watcher.source = root, source
                watcher.source_hash = hashlib.sha256(source.read_bytes()).digest()
                watcher.last_ticket_success = case != 'failed_ticket'
                watcher.result_paths = {'watcher.py', 'task.md'}
                program = 'import sys; assert sys.argv[1:] == ["--self-test"]\n'
                if case == 'failed_test':
                    program += 'raise SystemExit(1)\n'
                elif case == 'timeout':
                    program += 'import time; time.sleep(5)\n'
                elif case == 'changed_during_test':
                    program += 'from pathlib import Path; Path(__file__).write_text("# changed")\n'
                elif case == 'head_during_test':
                    program += 'import subprocess; subprocess.run(["git", "commit", "--allow-empty", "-qm", "changed"], check=True)\n'
                elif case == 'dirty_during_test':
                    program += 'from pathlib import Path; Path("new.py").write_text("# dirty")\n'
                if case not in ('unchanged', 'markdown'):
                    source.write_text(program, encoding='utf-8')
                    git(root, 'add', 'watcher.py')
                    git(root, 'commit', '-qm', '(agent) ticket 1: update')
                watcher.verified_head = git(root, 'rev-parse', 'HEAD').strip()
                if case == 'changed_head':
                    git(root, 'commit', '--allow-empty', '-qm', 'later')
                if case == 'dirty':
                    source.write_text(program + '# uncommitted', encoding='utf-8')
                if case == 'missing_source':
                    source.unlink()
                if case in ('markdown', 'success'):
                    note.write_text('concurrent edit', encoding='utf-8')
                with mock.patch(__name__ + '.RESTART_TEST_TIMEOUT', 0.1 if case == 'timeout' else 10):
                    if case in ('unchanged', 'markdown'):
                        self.assertFalse(watcher.restart_requested())
                    elif case == 'success':
                        with self.assertLogs(level='WARNING'):
                            self.assertTrue(watcher.restart_requested())
                    else:
                        with self.assertRaises((RuntimeError, OSError, subprocess.TimeoutExpired)):
                            watcher.restart_requested()

    def test_main_restart_and_cleanup(self):
        cases = ('before_scan', 'after_scan', 'once_second_scan', 'once_before_second', 'failure', 'normal')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder:
                watcher = mock.Mock()
                watcher.restart_requested.side_effect = {
                    'before_scan': [True], 'after_scan': [False, True],
                    'once_second_scan': [False, False, False, True],
                    'once_before_second': [False, False, True],
                    'failure': [False], 'normal': [False, False, False, False],
                }[case]
                watcher.scan.return_value = False
                if case == 'failure':
                    watcher.scan.side_effect = RuntimeError('ticket failed')
                with mock.patch(__name__ + '.Watcher', return_value=watcher), \
                     mock.patch('tempfile.gettempdir', return_value=folder), \
                     mock.patch.object(sys, 'argv', ['obsta.py', '--vault', folder, '--once', '--settle', '0']):
                    if case == 'failure':
                        with self.assertLogs(level='ERROR'):
                            status = main()
                    else:
                        status = main()
                self.assertEqual(status, 1 if case == 'failure' else 0 if case == 'normal' else RESTART_EXIT)
                watcher.close.assert_called_once_with()
                self.assertEqual(list(Path(folder).iterdir()), [])
                self.assertEqual(watcher.scan.call_count, 0 if case == 'before_scan' else
                                 2 if case in ('once_second_scan', 'normal') else 1)

    def test_scan_stops_on_ticket_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'task.md'
            text = '**run**'
            path.write_text(text, encoding='utf-8')
            watcher = Watcher.__new__(Watcher)
            watcher.root, watcher.settle = root, 0
            watcher.seen = {path: (text, 0)}
            watcher.process = mock.Mock(return_value=False)
            with self.assertRaisesRegex(RuntimeError, 'Ticket failed'):
                watcher.scan()
            watcher.process.assert_called_once()

    def test_ticket_ids_monotonic(self):
        with tempfile.TemporaryDirectory() as folder:
            db = sqlite3.connect(Path(folder) / 'ids.db')
            db.execute('CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL)')
            self.assertEqual(allocate_id(db), 1)
            self.assertEqual(allocate_id(db), 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--interval', type=float, default=0.5)
    parser.add_argument('--settle', type=float, default=1.0)
    parser.add_argument('--timeout', type=float, default=1800)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
        return 0 if unittest.TextTestRunner().run(suite).wasSuccessful() else 1
    if args.interval <= 0 or args.settle < 0 or args.timeout <= 0:
        parser.error('interval and timeout must be positive; settle must be nonnegative')
    root = args.vault.resolve()
    lock = Path(tempfile.gettempdir()) / ('obsta-' + hashlib.sha256(str(root).encode()).hexdigest()[:20] + '.lock')
    try:
        lock.mkdir()
    except FileExistsError:
        try:
            pid = int((lock / 'pid').read_text(encoding='ascii').strip())
            os.kill(pid, 0)
        except (ProcessLookupError, ValueError):
            # Reclaim a lock only when its owner is verifiably absent.
            if lock.exists():
                for child in lock.iterdir():
                    child.unlink()
                lock.rmdir()
            else:
                parser.exit(1, 'Watcher lock changed while checking: ' + str(lock) + '\n')
            try:
                lock.mkdir()
            except FileExistsError:
                parser.exit(1, 'Watcher lock exists: ' + str(lock) + '\n')
        except PermissionError:
            parser.exit(1, 'Watcher lock owner cannot be checked: ' + str(lock) + '\n')
        else:
            parser.exit(1, 'Watcher already running with pid ' + str(pid) + ': ' + str(lock) + '\n')
    (lock / 'pid').write_text(str(os.getpid()), encoding='ascii')
    watcher = None
    try:
        watcher = Watcher(root, args.settle, args.timeout, args.interval)
        logging.info('Watcher started: pid=%s vault=%s source=%s', os.getpid(), root, Path(__file__).resolve())
        while True:
            if watcher.restart_requested():
                return RESTART_EXIT
            did_work = watcher.scan()
            if watcher.restart_requested():
                return RESTART_EXIT
            if args.once:
                if not did_work:
                    time.sleep(args.settle)
                    if watcher.restart_requested():
                        return RESTART_EXIT
                    watcher.scan()
                    if watcher.restart_requested():
                        return RESTART_EXIT
                return 0
            time.sleep(args.interval)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        logging.exception('Watcher stopped due to an error; restart refused')
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if watcher:
            watcher.close()
        (lock / 'pid').unlink(missing_ok=True)
        lock.rmdir()
        logging.info('Watcher stopped: pid=%s', os.getpid())


if __name__ == '__main__':
    raise SystemExit(main())
