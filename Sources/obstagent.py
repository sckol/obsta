#!/usr/bin/env python3
"""Serial Obsidian watcher with recoverable A/B snapshots in obsta-history.

Run: python3 Sources/obstagent.py [--once | --self-test | --self-test-full]. Python 3.9+.
--self-test runs the restart gate; --self-test-full runs every regression test.
Snapshots use an independent index, commit-tree and conditional update-ref;
HEAD and the user's index are never changed. SQLite is neither read nor removed.
Activation metadata lives in Git commits and the local obsta-state journal;
new notes contain only ticket boundaries, requests and italic statuses/results.
Losing the journal before A requires explicit reactivation; no request is guessed.
Interior signals create child tickets via exec fork; child continuations use resume.
"""
import argparse
import hashlib
import fcntl
import html
import json
import logging
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

INSTRUCTION = Path('Conventions/Agent Instruction.md')
EXCLUDED = {'.git', '.obsidian', '.codex', '.agents', '__pycache__'}
RESTART_EXIT = 75
RESTART_TEST_TIMEOUT = 60
# Keep the restart gate bounded while retaining real Git, recovery and adapter checks.
# Fault matrices and exhaustive CLI scenarios remain in --self-test-full.
RESTART_TESTS = (
    'test_signals_and_boundaries',
    'test_config',
    'test_local_config',
    'test_orphan_same_tree_and_lost_index',
    'test_user_head_and_index_bytes',
    'test_ignore_force_and_service_exclusion',
    'test_full_cycle_and_restart_no_duplicate',
    'test_b_failure_recovery_only_saves',
    'test_unknown_a_does_not_execute',
    'test_multiline_result_and_recovery_without_result_marker',
    'test_inline_status_layout_and_recovery',
    'test_jsonl_adapter',
    'test_nested_fork_and_resume',
    'test_jsonl_fork',
    'test_jsonl_failure_paths',
    'test_restart_validation',
    'test_restart_timeout_and_changed_hash',
)
REF = 'refs/heads/obsta-history'
ID = r'(?:[A-Z][A-Z0-9]*-[1-9][0-9]*(?:/[1-9][0-9]*)*|[1-9][0-9]*(?:/[1-9][0-9]*)*)'
BOUNDARY = re.compile(r'(?<![\\\w])#([^\s#]+?)(🎬|🛑)')
TICKET = re.compile(r'#(' + ID + r')🎬')
EXTENSIONS = 'md base canvas avif bmp gif jpeg jpg png svg webp flac m4a mp3 ogg wav webm 3gp mkv mov mp4 ogv pdf'.split()


def git(root, *args, env=None, data=None, binary=False):
    """Run Git without shell expansion; every nonzero status is an error."""
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          env=env, input=data, capture_output=True,
                          text=not binary).stdout


def visible(text):
    result = [' '] * len(text)
    for offset, line in _lines(text):
        result[offset:offset + len(line)] = line
    masked = ''.join(result)
    masked = re.sub(r'(?<!`)(`+)(?!`)(.*?)\1(?!`)', lambda m: ' ' * len(m[0]), masked, flags=re.S)
    return re.sub(r'<!--.*?-->|%%.*?%%', lambda m: ' ' * len(m[0]), masked, flags=re.S)


def boundaries(text):
    """Validate complete, unique, properly nested boundaries outside code."""
    stack, result, used = [], [], set()
    for m in BOUNDARY.finditer(visible(text)):
        ident, mark = m.groups()
        if not re.fullmatch(ID, ident):
            raise RuntimeError('Invalid ticket ID: ' + ident)
        if mark == '🎬':
            if ident in used:
                raise RuntimeError('Duplicate ticket boundary: ' + ident)
            used.add(ident)
            stack.append((ident, m.start(), m.end()))
        else:
            if not stack or stack[-1][0] != ident:
                raise RuntimeError('Ambiguous ticket boundary: ' + ident)
            ident, start, content = stack.pop()
            result.append((ident, start, content, m.start(), m.end()))
    if stack:
        raise RuntimeError('Unclosed ticket: ' + stack[-1][0])
    return result


def find_commands(text):
    """Find ** signals and checked items; __ is ordinary bold formatting."""
    found = []
    document = visible(text)
    for offset, line in _lines(text):
        masked = document[offset:offset + len(line)]
        for m in re.finditer(r'(?<![\\*_])\*\*(?![*_\s])(?P<body>.+?)(?<!\s)\*\*(?![*_])', masked):
            if m['body'].strip() and not m['body'].startswith(('*', '_')):
                found.append((offset + m.start(), offset + m.end(), line[m.start()+2:m.end()-2], 'bold'))
        check = re.match(r'^(\s*)([-*+]\s+)\[x\](\s+)(.*?)(\r?\n)?$', line, re.I)
        if check and check[4].strip() and masked.strip():
            found.append((offset + len(check[1]), offset + len(line.rstrip('\r\n')), check[4], 'checklist'))
    return sorted(found)


def config(root):
    values = {}
    for name in ('obsta.md', 'obsta.local.md'):
        path = root / 'Configs' / name
        if name == 'obsta.local.md' and not path.exists():
            continue
        overrides = {}
        for line in path.read_text(encoding='utf-8').splitlines():
            m = re.fullmatch(r'`([^`]+)`\s*=\s*(.*?)\s*', line)
            if m:
                key, value = m.groups()
                if key in overrides:
                    raise RuntimeError('Duplicate setting in ' + name + ': ' + key)
                overrides[key] = value.strip('`')
        values.update(overrides)
    if not values.get('nickname'):
        raise RuntimeError('Set nickname in Configs/obsta.local.md or Configs/obsta.md; expected [A-Z][A-Z0-9]*')
    if not re.fullmatch(r'[A-Z][A-Z0-9]*', values.get('nickname', '')):
        raise RuntimeError('nickname must match [A-Z][A-Z0-9]*')
    if values.get('subagent') != 'codex':
        raise RuntimeError('Unsupported subagent: ' + values.get('subagent', '(missing)'))
    return values


def title(text):
    text = re.sub(r'[`*_#]', '', ' '.join(text.split()))
    return text[:200] or 'Результат отсутствует'


def service_marker(kind, key):
    return '%% obsta-' + kind + ':' + key + ' %%'


def marker_pattern(kind, key):
    payload = re.escape('obsta-' + kind + ':' + key)
    return r'(?:%% ' + payload + r' %%|<!-- ' + payload + r' -->)'


def italic(text, restore=False):
    # Keep block structure and code; italicize prose one line at a time.
    lines, fence = [], None
    for line in str(text).strip().splitlines():
        delimiter = re.match(r'^\s*(`{3,}|~{3,})', line)
        if delimiter and (fence is None or delimiter[1][0] == fence):
            fence = delimiter[1][0] if fence is None else None
            lines.append(line)
            continue
        if fence or not line.strip():
            lines.append(line)
            continue
        prefix = re.match(r'^(\s*(?:(?:#{1,6}|>|[-+*]|\d+[.)])\s+)?)(.*)$', line)
        protected = []
        def protect(match):
            protected.append(match[0])
            return '\x00' + str(len(protected) - 1) + '\x00'
        body = prefix[2]
        if restore and body.startswith('*') and body.endswith('*'):
            body = body[1:-1]
        prose = re.sub(r'`+[^`\n]*`+|\[[^]\n]+\]\([^\n)]*\)', protect, body)
        if not restore:
            prose = re.sub(r'(?<![\\\w])(\*{1,3}|_{1,3})(\S(?:.*?\S)?)\1(?!\w)', r'\2', prose)
        parts = re.split(r'(\x00\d+\x00)', prose)
        for i in range(0, len(parts), 2):
            parts[i] = (re.sub(r'\\([\\*_{}\[\]<>()#!|~%])', r'\1', parts[i]) if restore else
                        re.sub(r'([\\*_{}\[\]<>()#!|~%])', r'\\\1', parts[i]))
        for i in range(1, len(parts), 2):
            parts[i] = protected[int(parts[i][1:-1])]
        emphasis = '' if restore else '*'
        lines.append(prefix[1] + emphasis + ''.join(parts) + emphasis)
    return '\n'.join(lines)


def pending_marker(comment):
    # Legacy format, read only. New requests keep metadata outside Markdown.
    return service_marker('pending', comment.encode('utf-8').hex())


def request_tail(segment, activation):
    """Locate this request's status by an exact prefix fingerprint, not an icon."""
    length = activation.get('request_length')
    if not isinstance(length, int) or length < 0 or len(segment) < length:
        return None
    if hashlib.sha256(segment[:length].encode('utf-8')).hexdigest() != activation.get('request_hash'):
        return None
    return segment[length:].strip()


def process_identity(pid):
    try:
        return subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='],
                              check=True, capture_output=True, text=True).stdout.strip() or None
    except subprocess.CalledProcessError:
        return None


class History:
    def __init__(self, root):
        self.root = root
        self.gitdir = Path(git(root, 'rev-parse', '--absolute-git-dir').strip())
        self.common = (root / git(root, 'rev-parse', '--git-common-dir').strip()).resolve()
        self.state = self.gitdir / 'obsta-state'
        self.state.mkdir(exist_ok=True)
        self.index = self.state / 'index'
        self.env = dict(os.environ, GIT_INDEX_FILE=str(self.index))
        self.env.pop('GIT_LITERAL_PATHSPECS', None)

    def run(self, *args, **kwargs):
        return git(self.root, *args, env=self.env, **kwargs)

    def tip(self):
        # Missing branch is normal; corrupt refs or object reads are not.
        read = subprocess.run(['git', '-C', str(self.root), 'for-each-ref', '--format=%(refname) %(objectname) %(symref)', REF],
                              check=True, capture_output=True, text=True)
        if read.stderr.strip():
            raise RuntimeError('Cannot read history ref: ' + read.stderr.strip())
        refs = [line for line in read.stdout.splitlines() if line.split()[0] == REF]
        if not refs:
            return None
        fields = refs[0].split()
        if len(fields) != 2:
            raise RuntimeError('History must be a direct commit ref')
        oid = fields[1]
        git(self.root, 'cat-file', '-e', oid + '^{commit}')
        return oid

    def records(self):
        tip = self.tip()
        if not tip:
            return []
        result = []
        fields = git(self.root, 'log', '--format=%H%x00%B%x00', tip).split('\0')
        for i in range(0, len(fields) - 1, 2):
            oid, message = fields[i].strip(), fields[i + 1]
            subject, _, body = message.partition('\n\n')
            try:
                meta = json.loads(body)
            except ValueError:
                meta = {}
            if not isinstance(meta, dict) or meta.get('version') != 1:
                meta = {}
            if meta.get('phase') in ('A', 'B'):
                required = ('ticket_id', 'activation_id', 'note', 'outcome')
                if any(not isinstance(meta.get(k), str) for k in required):
                    raise RuntimeError('Malformed history metadata at ' + oid)
                if meta['phase'] == 'B' and not isinstance(meta.get('snapshot_a'), str):
                    raise RuntimeError('Missing snapshot_a at ' + oid)
            meta = dict(meta, oid=oid, subject=subject)
            result.append(meta)
        return result

    def ids(self):
        result = set()
        for path in notes(self.root):
            for ident, *_ in boundaries(path.read_text(encoding='utf-8')):
                if ident in result:
                    raise RuntimeError('Duplicate ticket across notes: ' + ident)
                result.add(ident)
        if not hasattr(self, '_blob_ids'):
            self._blob_ids = {}
            self._commit_ids = {}
        for record in self.records():
            if record['oid'] in self._commit_ids:
                result.update(self._commit_ids[record['oid']])
                continue
            historical_ids = set()
            # Read each historical Markdown blob once, including deleted notes.
            entries = git(self.root, 'ls-tree', '-rz', record['oid'], binary=True)
            for entry in filter(None, entries.split(b'\0')):
                info, name = entry.split(b'\t', 1)
                mode, kind, oid = info.split()
                if kind == b'blob' and mode != b'120000' and name.lower().endswith(b'.md'):
                    if oid not in self._blob_ids:
                        historic = git(self.root, 'cat-file', 'blob', oid.decode())
                        self._blob_ids[oid] = {m[1] for m in TICKET.finditer(visible(historic))}
                    historical_ids.update(self._blob_ids[oid])
                    result.update(self._blob_ids[oid])
            m = re.match(r'^#(' + ID + r')\s', record['subject'])
            if m:
                result.add(m[1])
                historical_ids.add(m[1])
            if record.get('ticket_id'):
                if not re.fullmatch(ID, str(record['ticket_id'])):
                    raise RuntimeError('Invalid ticket ID in history')
                result.add(record['ticket_id'])
                historical_ids.add(record['ticket_id'])
            self._commit_ids[record['oid']] = historical_ids
        return result

    def allocate(self, nickname, parent=None):
        prefix = parent + '/' if parent else nickname + '-'
        numbers = []
        for ident in self.ids():
            if ident.startswith(prefix):
                component = ident[len(prefix):].split('/')[0]
                numbers.append(int(component))
        return prefix + str(max(numbers, default=0) + 1)

    def allowed(self, name):
        p = Path(name)
        if p.is_absolute() or '..' in p.parts or '.git' in p.parts:
            return False
        if any(part.endswith('.obsta-tmp') for part in p.parts):
            return False
        full = self.root / p
        # Do not follow the final symlink: Git records the link itself.
        parent = full.parent.resolve()
        if parent != self.root and self.root not in parent.parents:
            return False
        if any(full == internal or internal in full.parents for internal in (self.gitdir, self.common)):
            return False
        return True

    def ignore_block(self):
        path = self.root / '.gitignore'
        if not path.exists():
            return
        before = path.read_bytes()
        begin, end = b'# BEGIN OBSTA DEFAULT', b'# END OBSTA DEFAULT'
        lines = before.splitlines(keepends=True)
        starts = [i for i, line in enumerate(lines) if line.strip() == begin]
        ends = [i for i, line in enumerate(lines) if line.strip() == end]
        if not starts and not ends:
            return
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise RuntimeError('Malformed OBSTA DEFAULT block')
        linked = set()
        for note in notes(self.root):
            sibling = note.with_suffix('')
            if sibling.is_file():
                linked.add(sibling.relative_to(self.root).as_posix())
            text = note.read_text(encoding='utf-8')
            # Sources supports inline YAML scalar/list and indented YAML lists.
            front = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.S)
            if not front:
                continue
            source_lines, collecting = [], False
            for line in front[1].splitlines():
                m = re.match(r'^Sources:\s*(.*)', line)
                if m:
                    collecting = True
                    source_lines.extend(re.findall(r'"[^"]*"|\x27[^\x27]*\x27|[^,\[\]]+', m[1]))
                elif collecting and re.match(r'^\s+-\s+', line):
                    source_lines.append(re.sub(r'^\s+-\s+', '', line))
                else:
                    collecting = False
            for value in source_lines:
                value = value.strip().strip('\"\x27')
                if not value:
                    continue
                target = (self.root / value.lstrip('/') if value.startswith('/') else note.parent / value)
                target = Path(os.path.abspath(target))
                if target != self.root and self.root not in target.parents:
                    raise RuntimeError('Sources path outside vault: ' + value)
                name = target.relative_to(self.root).as_posix()
                if not self.allowed(name):
                    raise RuntimeError('Forbidden Sources path: ' + value)
                linked.add(name)
        def escape(name):
            if '\n' in name or '\r' in name:
                raise RuntimeError('Sources filename cannot be represented in gitignore')
            return ''.join('\\' + c if c in '\\*?[]!# ' else c for c in name)
        rules = ['*', '!*/', '!.gitignore']
        rules += ['!*.' + ''.join('[' + c + c.upper() + ']' if c.isalpha() else c for c in ext) for ext in EXTENSIONS]
        rules += ['!/' + escape(name) for name in sorted(linked)]
        newline = b'\r\n' if lines[starts[0]].endswith(b'\r\n') else b'\n'
        generated = newline.join(rule.encode('utf-8') for rule in rules) + newline
        after = b''.join(lines[:starts[0]+1]) + generated + b''.join(lines[ends[0]:])
        if path.read_bytes() != before:
            raise RuntimeError('gitignore changed during snapshot preparation')
        if before != after:
            path.write_bytes(after)

    def check_worktrees(self):
        data = git(self.root, 'worktree', 'list', '--porcelain')
        if 'branch ' + REF in data.splitlines():
            raise RuntimeError('obsta-history is checked out in a worktree')

    def snapshot(self, metadata, message):
        for attempt in range(3):
            self.check_worktrees()
            old = self.tip()
            self.ignore_block()
            if not self.index.exists():
                self.run('read-tree', '--empty')
            ignored = self.run('ls-files', '--cached', '--ignored', '--exclude-standard', '-z', binary=True)
            if ignored:
                self.run('update-index', '--force-remove', '-z', '--stdin', data=ignored, binary=True)
            exclusions = [':(exclude,glob)**/.git', ':(exclude,glob)**/.git/**',
                          ':(exclude,glob)**/*.obsta-tmp']
            for internal in (self.gitdir, self.common):
                if self.root in internal.parents:
                    exclusions.append(':(exclude,literal)' + internal.relative_to(self.root).as_posix())
            self.run('add', '-A', '--', '.', *exclusions)
            ignore = self.root / '.gitignore'
            forced = set()
            if ignore.exists():
                for line in ignore.read_text(encoding='utf-8').splitlines():
                    if not line.startswith('# obsta-history:'):
                        continue
                    spec = line.partition(':')[2].strip()
                    if not spec:
                        continue
                    if spec.startswith('/'):
                        spec = ':(top)' + spec[1:]
                    matches = self.run('ls-files', '--cached', '--others', '-z', '--', spec, binary=True)
                    for raw in matches.split(b'\0'):
                        if raw:
                            name = os.fsdecode(raw)
                            path = self.root / name
                            if self.allowed(name) and (path.is_file() or path.is_symlink()):
                                forced.add(name)
            batch, size = [], 0
            for name in sorted(forced):
                if batch and size + len(os.fsencode(name)) > 24000:
                    self.run('--literal-pathspecs', 'add', '-f', '--', *batch)
                    batch, size = [], 0
                batch.append(name)
                size += len(os.fsencode(name)) + 1
            if batch:
                self.run('--literal-pathspecs', 'add', '-f', '--', *batch)
            cached = self.run('ls-files', '-z', binary=True)
            forbidden = [raw for raw in cached.split(b'\0') if raw and not self.allowed(os.fsdecode(raw))]
            if forbidden:
                self.run('update-index', '--force-remove', '-z', '--stdin', data=b'\0'.join(forbidden) + b'\0', binary=True)
            tree = self.run('write-tree').strip()
            parent = ['-p', old] if old else []
            commit = self.run('commit-tree', tree, *parent,
                              data=message + '\n\n' + json.dumps(metadata, ensure_ascii=False) + '\n').strip()
            self.check_worktrees()
            try:
                self.run('update-ref', REF, commit, old or '0' * len(commit))
                return commit
            except subprocess.CalledProcessError:
                if self.tip() == old:
                    raise
        raise RuntimeError('History ref conflicted three times')
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
                fence = (token[0], len(token))
            elif token[0] == fence[0] and len(token) >= fence[1] and not stripped[len(token):].strip():
                fence = None
            offset += len(line)
            continue
        if fence is None:
            yield offset, line
        offset += len(line)


def notes(root):
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not d.startswith('.')
                         and not (Path(base) / d).is_symlink())
        for name in sorted(files):
            path = Path(base) / name
            if (path.suffix == '.md' and not path.is_symlink()
                    and path not in {root / INSTRUCTION, Path(__file__).resolve()}):
                yield path


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




class Watcher:
    def __init__(self, root, settle=1.0, timeout=1800, interval=0.5, runner=None):
        self.root = root.resolve()
        self.settle, self.timeout, self.interval = settle, timeout, interval
        self.runner = runner or self.execute
        self.seen, self.cooldown = {}, {}
        self.history = History(self.root)
        if Path(git(self.root, 'rev-parse', '--show-toplevel').strip()).resolve() != self.root:
            raise RuntimeError('Vault must be the Git repository root')
        if not (self.root / INSTRUCTION).is_file():
            raise RuntimeError('Missing Agent Instruction.md')
        self.source = Path(__file__).resolve()
        self.source_hash = hashlib.sha256(self.source.read_bytes()).digest()
        self.rejected_hash = None
        self.saved_source_hash = None
        self.last_accounted = False
        self.recovered = False
        self.pending_b = {}
        self.prepared = {}
        self.journals = {}
        self.status_lines = {}
        for path in self.history.state.glob('activation-*.json'):
            try:
                cached = json.loads(path.read_text(encoding='utf-8'))
                self.journals[cached['activation_id']] = cached
            except (ValueError, KeyError):
                logging.error('Unreadable activation cache: %s', path)

    def close(self):
        pass

    def save_journal(self, activation):
        key = activation['activation_id']
        self.journals[key] = activation
        path = self.history.state / ('activation-' + key + '.json')
        temp = path.with_suffix('.obsta-tmp')
        temp.write_text(json.dumps(activation, ensure_ascii=False), encoding='utf-8')
        os.replace(temp, path)
        if activation['stage'] == 'prepared':
            self.prepared[key] = activation
        else:
            self.prepared.pop(key, None)

    def note_update(self, activation, status=None, outcome=None):
        """Edit only the current ticket; absent notes/markers stay absent."""
        path = self.root / activation['note']
        try:
            before = path.read_text(encoding='utf-8')
            matches = [x for x in boundaries(before) if x[0] == activation['ticket_id']]
            if len(matches) != 1:
                logging.error('Ticket boundary removed: %s', activation['ticket_id'])
                return False
            _, start, content, end, finish = matches[0]
            segment = before[content:end]
            if outcome:
                pending = '⏳ ' + activation['comment'] + ' ⏳'
                segment = segment.replace(pending, activation['comment'], 1)
                segment = segment.replace(pending_marker(activation['comment']), '', 1)
            if status is not None:
                # Track the exact fragment, including its separators; never
                # infer ownership from an icon or overwrite a human edit.
                status_key = (activation['note'], activation['ticket_id'], activation['activation_id'])
                previous = self.status_lines.get(status_key, activation.get('status_line'))
                status = str(status).strip()
                multiline = bool(re.search(r'\n\s*\n|(?m:^\s*(?:#{1,6}\s|[-+*]\s|\d+[.)]\s|>|`{3,}|~{3,}))', status))
                if not multiline:
                    status = ' '.join(status.splitlines())
                rendered = italic(status)
                line = ('\n\n' if multiline else ' ') + rendered + ('\n' if multiline else ' ')
                pattern = re.compile(marker_pattern('status', activation['activation_id']) + r'[^\n]*(?:\n|$)')
                previous_pattern = (('(?m)^' if previous and not previous[0].isspace() else '')
                                    + re.escape(previous or ''))
                if pattern.search(segment):
                    segment = pattern.sub(lambda _: line, segment, count=1)
                elif previous and len(list(re.finditer(previous_pattern, segment))) == 1:
                    segment = re.sub(previous_pattern, lambda _: line, segment, count=1)
                else:
                    segment = segment.rstrip() + line
            atomic_replace(path, before, before[:content] + segment + before[end:])
            if status is not None:
                self.status_lines[status_key] = line
                activation['status_line'] = line
            return True
        except (OSError, UnicodeError, RuntimeError):
            logging.exception('Could not write ticket status')
            return False

    def metadata(self, activation, phase):
        result = {key: activation.get(key) for key in
                  ('ticket_id', 'activation_id', 'note', 'parent_ticket_id', 'session_id', 'outcome')}
        result.update(version=1, phase=phase, comment=activation.get('comment'))
        for key in ('request_length', 'request_hash', 'fork_parent_session_id'):
            if key in activation:
                result[key] = activation[key]
        if phase == 'B':
            result['result'] = activation.get('result')
        if phase == 'B':
            result['snapshot_a'] = activation['snapshot_a']
            result['late'] = activation.get('late', False)
        if self.pending_b:
            result['includes_results_of'] = list(self.pending_b)
        return result

    def snapshot_b(self, activation):
        if activation.get('late'):
            self.note_update(activation, ('◇ ' if activation['outcome'] == 'success' else '❌ ') + activation['result'])
        try:
            existing = next((r for r in self.history.records() if r.get('phase') == 'B' and r.get('activation_id') == activation['activation_id']), None)
            oid = existing['oid'] if existing else self.history.snapshot(self.metadata(activation, 'B'),
                                  '#' + activation['ticket_id'] + ' 🤖 ' + title(re.split(r'(?<=[.!?])\s', activation['result'], maxsplit=1)[0]))
        except (OSError, subprocess.SubprocessError, RuntimeError):
            logging.exception('Snapshot B failed')
            activation['late'] = True
            self.pending_b[activation['activation_id']] = activation
            icon = '◇ ' if activation['outcome'] == 'success' else '❌ '
            self.note_update(activation, icon + activation['result'] + ' ❌ Не удалось сохранить Б; результат ожидает сохранения, исполнитель повторно не запускается.')
            try:
                self.save_journal(activation)
            except OSError:
                logging.exception('Result cache unavailable; recover through history and note')
            return False
        self.pending_b.pop(activation['activation_id'], None)
        activation['stage'] = 'done'
        try:
            self.save_journal(activation)
        except OSError:
            logging.exception('B saved; disposable cache update failed')
        self.last_accounted = True
        self.saved_source_hash = None
        try:
            if self.root in self.source.parents:
                saved_source = git(self.root, 'show', oid + ':' + self.source.relative_to(self.root).as_posix(), binary=True)
                source_hash = hashlib.sha256(self.source.read_bytes()).digest()
                if hashlib.sha256(saved_source).digest() == source_hash:
                    self.saved_source_hash = source_hash
        except (OSError, subprocess.SubprocessError):
            logging.exception('B saved; source could not be certified for restart')
        return True

    def recover(self):
        records = self.history.records()
        completed = {r.get('activation_id') for r in records if r.get('phase') == 'B'}
        starts = {r['activation_id']: r for r in records if r.get('phase') == 'A'}
        for key, record in starts.items():
            if key in completed:
                continue
            activation = self.journals.get(key, dict(record, snapshot_a=record['oid'], comment=record.get('comment') or ''))
            activation['snapshot_a'] = record['oid']
            if activation.get('pid') and activation.get('process_identity') and process_identity(activation['pid']) == activation['process_identity']:
                logging.error('Executor still alive for activation %s', key)
                return False
            # A note may have survived a crash before the result cache write.
            if activation.get('stage') != 'result':
                path = self.root / activation['note']
                if path.exists():
                    current = path.read_text(encoding='utf-8')
                    ranges = [r for r in boundaries(current) if r[0] == activation['ticket_id']]
                    if len(ranges) != 1:
                        continue
                    current = current[ranges[0][2]:ranges[0][3]]
                    token = marker_pattern('status', key)
                    m = re.search(token + r' \*([◇\u27a4❌]) (.*)\*(?:\n|$)', current)
                    outcome = re.search(marker_pattern('result', key + ':success'), current)
                    if not outcome:
                        outcome = re.search(marker_pattern('result', key + ':error'), current)
                    if not m and outcome:
                        # New results have a plain italic line immediately
                        # before their UUID outcome marker, inside this ticket.
                        stamp = re.search(marker_pattern('activation', key), current[:outcome.start()])
                        if stamp:
                            m = re.search(r'(?m)^\*([◇\u27a4❌]) (.*)\*\n\s*$', current[stamp.end():outcome.start()])
                    if m and outcome:
                        result = html.unescape(m[2]) if m[0].startswith('<!--') else re.sub(r'\\(.)', r'\1', m[2])
                        activation.update(stage='result', result=result,
                                          outcome='success' if ':success' in outcome[0] else 'error')
                        if not m[0].startswith(('<!--', '%%')):
                            activation['status_line'] = m[0].rstrip() + '\n'
                    elif not outcome:
                        # Final prose follows this activation's stamp. Pending
                        # data is removed only by the final note update, so a
                        # preparation error cannot masquerade as a result.
                        stamp = re.search(marker_pattern('activation', key), current)
                        pending = pending_marker(activation['comment']) in current or '⏳ ' + activation['comment'] + ' ⏳' in current
                        tail = current[stamp.end():].strip() if stamp else ''
                        if not stamp and record.get('request_hash'):
                            # A supplies the request anchor even if the local
                            # cache disappeared. A later activation in this
                            # ticket makes a marker-free tail ambiguous.
                            latest = next((r for r in records if r.get('phase') == 'A'
                                           and r.get('note') == record['note']
                                           and r.get('ticket_id') == record['ticket_id']), None)
                            tail = (request_tail(current, record) or '') if latest == record else ''
                        final = re.match(r'^\*([◇\u27a4❌]) ', tail)
                        diagnostic = any(word in tail for word in ('Исход прерванной активации неизвестен', 'Подготовка А прервана'))
                        if final and not pending and not diagnostic and not find_commands(tail):
                            result = italic(tail, restore=True)[2:]
                            fragment = (current[record['request_length']:]
                                        if record.get('request_hash') and not stamp else tail + '\n')
                            activation.update(stage='result', result=result,
                                              outcome='success' if final[1] in '◇\u27a4' else 'error',
                                              status_line=fragment)
            if activation.get('stage') == 'result':
                activation['late'] = True
                self.pending_b[key] = activation
            else:
                self.note_update(activation, '❌ Исход прерванной активации неизвестен; автоматического повторного запуска нет. Для продолжения добавьте явный сигнал в конце тикета.')
        for key, cached in self.journals.items():
            if cached.get('stage') == 'prepared' and key not in starts and key not in completed:
                if cached.get('request_hash') and any(
                        r.get('phase') == 'A' and r.get('note') == cached['note']
                        and r.get('ticket_id') == cached['ticket_id']
                        and r.get('request_hash') == cached['request_hash'] for r in records):
                    # A stale cache from a failed note write cannot claim the
                    # identical request subsequently accounted under another UUID.
                    continue
                path = self.root / cached['note']
                if path.exists():
                    text = path.read_text(encoding='utf-8')
                    ranges = [r for r in boundaries(text) if r[0] == cached['ticket_id']]
                    if len(ranges) == 1:
                        segment = text[ranges[0][2]:ranges[0][3]]
                        if (request_tail(segment, cached) is not None
                                or re.search(marker_pattern('activation', key), segment)):
                            self.prepared[key] = cached
        # Recover a pre-A request even when the disposable cache was lost.
        reserved = {a['ticket_id'] for a in self.prepared.values()}
        known = {r.get('ticket_id') for key, r in starts.items() if key not in completed}
        for path in notes(self.root):
            current = path.read_text(encoding='utf-8')
            for ident, _, content, close, _ in boundaries(current):
                segment = current[content:close]
                hidden = re.search(r'%% obsta-pending:([0-9a-f]+) %%', segment)
                pending = re.search(r'⏳ (.*?) ⏳', segment, re.S)
                comment = bytes.fromhex(hidden[1]).decode('utf-8') if hidden else pending[1] if pending else None
                completed_request = comment is not None and any(
                    r.get('phase') == 'B' and r.get('ticket_id') == ident and r.get('comment') == comment
                    for r in records)
                if completed_request:
                    # A failed note write must never turn an accounted result into another execution.
                    logging.warning('Pending marker belongs to an accounted request: %s; use an explicit new signal', ident)
                    continue
                if comment is not None and ident not in known and ident not in reserved:
                    activation = dict(ticket_id=ident, activation_id=str(uuid.uuid4()), note=str(path.relative_to(self.root)),
                                      parent_ticket_id=ident.rsplit('/', 1)[0] if '/' in ident else None, session_id=next((r.get('session_id') for r in records if r.get('ticket_id') == ident and r.get('session_id')), None),
                                      outcome='pending', comment=comment, stage='prepared')
                    self.save_journal(activation)
                    self.note_update(activation, '⏳ Подготовка снимка А после восстановления…')
                    reserved.add(ident)
        self.recovered = True
        return True

    def restart_requested(self):
        try:
            candidate = hashlib.sha256(self.source.read_bytes()).digest()
            if candidate in (self.source_hash, self.rejected_hash):
                return False
            if not self.last_accounted or self.pending_b or candidate != self.saved_source_hash:
                return False
            result = subprocess.run([sys.executable, str(self.source), '--self-test'],
                                    cwd=self.root, capture_output=True, text=True, timeout=RESTART_TEST_TIMEOUT)
            if result.returncode or hashlib.sha256(self.source.read_bytes()).digest() != candidate:
                raise RuntimeError('Updated source self-test failed or source changed: ' + (result.stderr or '')[-1500:])
            return True
        except (OSError, subprocess.SubprocessError, RuntimeError):
            self.rejected_hash = locals().get('candidate')
            logging.exception('Restart refused; loaded watcher continues')
            return False

    def scan(self):
        now = time.monotonic()
        try:
            if not self.recovered and not self.recover():
                return False
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
            logging.exception('History recovery unavailable; observation continues')
            return False
        # At most one delayed save per pass; remaining signals still get time.
        for activation in list(self.pending_b.values()):
            key = ('B', activation['activation_id'])
            if self.cooldown.get(key, 0) <= now:
                self.cooldown[key] = now + 30
                self.snapshot_b(activation)
                break
        for activation in list(self.prepared.values()):
            key = ('retry', activation['activation_id'])
            if self.cooldown.get(key, 0) <= now:
                self.cooldown[key] = now + 30
                self.run_activation(activation)
                break
        did_work = False
        for path in notes(self.root):
            try:
                text = path.read_text(encoding='utf-8')
                old, since = self.seen.get(path, (None, now))
                if old != text:
                    self.seen[path] = (text, now)
                    continue
                if now - since < self.settle:
                    continue
                for command in find_commands(text):
                    key = (path, command)
                    if self.cooldown.get(key, 0) > now:
                        continue
                    self.cooldown[key] = now + 30
                    if self.process(path, text, command):
                        did_work = True
                        break
                if did_work:
                    break
            except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
                logging.exception('Note processing failed; observation continues: %s', path)
        return did_work

    def process(self, path, text, command):
        activation = None
        try:
            settings = config(self.root)
            start, end, body, kind = command
            begin, finish = ticket_boundary(text, start, end, kind)
            ranges = boundaries(text)
            containing = [r for r in ranges if r[2] <= start < r[3]]
            enclosing = min(containing, key=lambda r: r[3] - r[2]) if containing else None
            session = None
            fork_parent = None
            nested = False
            if enclosing:
                self.history.ids()
                ident, _, _, close, _ = enclosing
                tail = re.sub(marker_pattern('status', 'nested') + r'[^\n]*|' + marker_pattern('status', 'resume') + r'[^\n]*', '', text[finish:close])
                for diagnostic_key in ('nested', 'resume'):
                    line = self.status_lines.get((str(path.relative_to(self.root)), ident, diagnostic_key))
                    if line:
                        tail = tail.replace(line, '', 1)
                nested = bool(tail.strip())
                previous = next((r for r in self.history.records() if r.get('ticket_id') == ident and r.get('session_id')), None)
                if not previous:
                    previous = next((r for r in reversed(list(self.journals.values())) if r.get('ticket_id') == ident and r.get('session_id')), None)
                if not previous:
                    self.note_update(dict(ticket_id=ident, note=str(path.relative_to(self.root)), activation_id='nested' if nested else 'resume'),
                                     '❌ Нет подтверждённой сессии для ' + ('fork' if nested else 'resume') + '; сигнал сохранён.')
                    return False
                if nested:
                    fork_parent = previous['session_id']
                    # Check availability before consuming the user's signal.
                    if self.runner == self.execute:
                        try:
                            self.executor_args(dict(session_id=None, fork_parent_session_id=fork_parent))
                        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
                            self.note_update(dict(ticket_id=ident, note=str(path.relative_to(self.root)), activation_id='nested'),
                                             '❌ Fork недоступен; сигнал сохранён: ' + str(error))
                            return False
                    ident = self.history.allocate(settings['nickname'], parent=ident)
                else:
                    session = previous['session_id']
            else:
                ident = self.history.allocate(settings['nickname'])
            if enclosing and not (enclosing[2] <= begin <= start < end <= finish <= enclosing[3]):
                raise RuntimeError('Command crosses enclosing ticket boundary')
            pending = body
            if kind == 'bold':
                replacement = '__' + pending + '__'
            else:
                replacement = text[begin:finish]
                replacement = re.sub(r'^(\s*[-*+]\s+)\[x\](\s+)', r'\1[ ]\2', replacement, count=1, flags=re.I)
                body_start = end - len(body) - begin
                replacement = replacement[:body_start] + pending + replacement[body_start + len(body):]
            if not enclosing or nested:
                replacement = f'#{ident}🎬 {replacement.rstrip()} #{ident}🛑'
            activation = dict(ticket_id=ident, activation_id=str(uuid.uuid4()), note=str(path.relative_to(self.root)),
                              parent_ticket_id=ident.rsplit('/', 1)[0] if '/' in ident else None, session_id=session, outcome='pending', comment=body, stage='prepared')
            if fork_parent:
                activation['fork_parent_session_id'] = fork_parent
            updated = text[:begin] + replacement + text[finish:]
            region = next(r for r in boundaries(updated) if r[0] == ident)
            prefix = updated[region[2]:region[3]].rstrip()
            activation.update(request_length=len(prefix), request_hash=hashlib.sha256(prefix.encode('utf-8')).hexdigest())
            # Write cache first. A missing note/boundary prevents execution on retry.
            self.save_journal(activation)
            atomic_replace(path, text, updated)
            self.note_update(activation, '⏳ Обрабатывается…')
            self.cooldown[('retry', activation['activation_id'])] = time.monotonic() + 30
            return self.run_activation(activation)
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as error:
            logging.exception('Activation failed; observation continues')
            if activation:
                self.note_update(activation, '❌ ' + str(error))
            return False

    def run_activation(self, activation):
        self.last_accounted = False
        try:
            config(self.root)
            prior = [r for r in self.history.records() if r.get('activation_id') == activation['activation_id']]
            if prior:
                self.prepared.pop(activation['activation_id'], None)
                self.recovered = False
                return False
            path = self.root / activation['note']
            current = path.read_text(encoding='utf-8')
            matches = [r for r in boundaries(current) if r[0] == activation['ticket_id']]
            segment = current[matches[0][2]:matches[0][3]] if len(matches) == 1 else ''
            tail = request_tail(segment, activation)
            legacy = (not activation.get('request_hash') and any(token in segment for token in
                       ('⏳ ' + activation['comment'] + ' ⏳', pending_marker(activation['comment']))))
            if len(matches) != 1 or not (legacy or (tail is not None and not find_commands(tail))):
                activation['stage'] = 'abandoned'
                self.save_journal(activation)
                return False
            instruction = (self.root / INSTRUCTION).read_text(encoding='utf-8')
            if not instruction.strip():
                raise RuntimeError('Empty agent instruction')
            activation['snapshot_a'] = self.history.snapshot(self.metadata(activation, 'A'),
                                                            '#' + activation['ticket_id'] + ' ✍️ ' + title(activation['comment']))
            activation['stage'] = 'A'
            self.save_journal(activation)
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            logging.exception('Snapshot A failed; executor was not started')
            if activation.get('snapshot_a'):
                activation['stage'] = 'unknown'
                self.prepared.pop(activation['activation_id'], None)
            self.note_update(activation, '❌ Подготовка А прервана; исполнитель не запущен: ' + str(error))
            return False
        try:
            _, start, _, _, finish = matches[0]
            context = {key: activation[key] for key in ('ticket_id', 'activation_id', 'snapshot_a', 'note', 'comment')}
            context.update(parent_ticket_id=activation.get('parent_ticket_id'),
                           fork_parent_session_id=activation.get('fork_parent_session_id'))
            context.update(ticket=current[start:finish], surrounding_text=current[:start] + '[[TICKET ' + activation['ticket_id'] + ']]' + current[finish:])
            activation['stage'] = 'running'
            self.save_journal(activation)
            response = self.runner(instruction + '\n\n' + json.dumps(context, ensure_ascii=False), activation,
                                   lambda message: self.note_update(activation, '💭 ' + message))
            activation['outcome'] = 'success'
            activation['result'] = response or 'Запрос выполнен; текст ответа отсутствует.'
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as error:
            activation['outcome'] = 'error'
            activation['result'] = str(error) or type(error).__name__
            logging.exception('Executor failed')
        self.note_update(activation, ('◇ ' if activation['outcome'] == 'success' else '❌ ') + activation['result'], activation['outcome'])
        activation['stage'] = 'result'
        try:
            self.save_journal(activation)
        except OSError:
            logging.exception('Result cache write failed; still attempting B')
        self.snapshot_b(activation)
        return True

    def executor_args(self, activation):
        args = ['codex', 'exec', '-c', 'approval_policy="never"']
        session = activation.get('session_id')
        parent = activation.get('fork_parent_session_id')
        mode = 'resume' if session else 'fork' if parent else None
        if mode:
            help_text = subprocess.run(['codex', 'exec', mode, '--help'], check=True,
                                       capture_output=True, text=True, timeout=10).stdout
            if '--json' not in help_text:
                raise RuntimeError('Installed executor does not support JSON ' + mode)
            args += [mode, '--json', session or parent, '-']
        else:
            args += ['--json', '-C', str(self.root), '-']
        return args

    def execute(self, prompt, activation, status):
        """Consume public JSONL events incrementally; never expose reasoning items."""
        args = self.executor_args(activation)
        session_started = False
        final, completed, event_error = '', False, None
        with tempfile.TemporaryFile() as errors, tempfile.TemporaryFile() as prompt_file:
            prompt_file.write(prompt.encode('utf-8'))
            prompt_file.seek(0)
            child = subprocess.Popen(args, cwd=self.root, stdin=prompt_file, stdout=subprocess.PIPE,
                                     stderr=errors, start_new_session=True)
            activation.update(pid=child.pid, process_identity=process_identity(child.pid))
            selector = selectors.DefaultSelector()
            selector.register(child.stdout, selectors.EVENT_READ)
            deadline, buffer = time.monotonic() + self.timeout, b''
            try:
                self.save_journal(activation)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(args, self.timeout)
                    for key, _ in selector.select(min(0.2, max(0, deadline - time.monotonic()))):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            if buffer.strip():
                                raise RuntimeError('Truncated executor JSON event')
                            continue
                        buffer += chunk
                        while b'\n' in buffer:
                            raw, buffer = buffer.split(b'\n', 1)
                            if not raw.strip():
                                continue
                            event = json.loads(raw)
                            if event.get('type') == 'thread.started':
                                session = event.get('thread_id')
                                if not session:
                                    raise RuntimeError('Missing executor session ID')
                                if activation['session_id'] and activation['session_id'] != session:
                                    raise RuntimeError('Executor resumed a different session')
                                if not activation['session_id'] and session == activation.get('fork_parent_session_id'):
                                    raise RuntimeError('Executor did not fork: parent session returned')
                                session_started = True
                                activation['session_id'] = session
                                self.save_journal(activation)
                            elif event.get('type') == 'item.completed' and event.get('item', {}).get('type') == 'agent_message':
                                message = event['item'].get('text', '')
                                final = message
                                status(message)
                            elif event.get('type') == 'turn.completed':
                                completed = True
                            elif event.get('type') in ('error', 'turn.failed'):
                                event_error = str(event.get('error') or event.get('message') or event)
                code = child.wait(timeout=max(0.01, deadline - time.monotonic()))
                if not session_started:
                    raise RuntimeError('Executor returned no session ID')
                if code or event_error or not completed:
                    errors.seek(max(0, errors.seek(0, 2) - 2000))
                    raise RuntimeError(event_error or 'Executor exit ' + str(code) + ': ' + errors.read().decode('utf-8', 'replace'))
                if not final:
                    raise RuntimeError('Executor returned no final result')
                envelope = json.loads(final)
                if not isinstance(envelope, dict) or envelope.get('outcome') not in ('success', 'error') or not isinstance(envelope.get('response'), str):
                    raise RuntimeError('Executor final result must contain outcome and response')
                if envelope['outcome'] == 'error':
                    raise RuntimeError(envelope['response'] or 'Executor reported an incomplete task')
                return envelope['response'] or 'Запрос выполнен; текст ответа отсутствует.'
            finally:
                selector.close()
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
                child.stdout.close()



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--interval', type=float, default=0.5)
    parser.add_argument('--settle', type=float, default=1.0)
    parser.add_argument('--timeout', type=float, default=1800)
    parser.add_argument('--once', action='store_true')
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument('--self-test', action='store_true', help='run the bounded restart gate')
    checks.add_argument('--self-test-full', action='store_true', help='run all regression tests')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.self_test or args.self_test_full:
        suite = self_test_suite(full=args.self_test_full)
        return 0 if unittest.TextTestRunner().run(suite).wasSuccessful() else 1
    if args.interval <= 0 or args.settle < 0 or args.timeout <= 0:
        parser.error('interval and timeout must be positive; settle must be nonnegative')
    watcher = None
    try:
        root = args.vault.resolve()
        history = History(root)
        # Keep the inode while unlocking: unlinking an flock file races waiters.
        with (history.common / 'obsta-watcher.lock').open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Watcher already running')
            # Interoperate with a still-loaded pre-history watcher during upgrade.
            legacy = Path(tempfile.gettempdir()) / ('obsta-' + hashlib.sha256(str(root).encode()).hexdigest()[:20] + '.lock/pid')
            if legacy.exists():
                pid = int(legacy.read_text(encoding='ascii'))
                if process_identity(pid):
                    raise RuntimeError('Legacy watcher already running with pid ' + str(pid))
            lock.seek(0)
            lock.truncate()
            lock.write(str(os.getpid()))
            lock.flush()
            watcher = Watcher(root, args.settle, args.timeout, args.interval)
            logging.info('Watcher started: pid=%s vault=%s protocol=obsta-history-v1', os.getpid(), root)
            passes = 0
            while True:
                try:
                    if watcher.restart_requested():
                        return RESTART_EXIT
                    watcher.scan()
                    if watcher.restart_requested():
                        return RESTART_EXIT
                except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
                    logging.exception('Request failed; loaded watcher continues')
                passes += 1
                if args.once and passes >= 2:
                    return 0
                time.sleep(max(args.interval, args.settle) if args.once else args.interval)
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
        logging.exception('Watcher startup failed')
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if watcher:
            watcher.close()
        logging.info('Watcher stopped: pid=%s', os.getpid())


def self_test_suite(full=False):
    if full:
        return unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
    return unittest.TestSuite(Tests(name) for name in RESTART_TESTS)


class Tests(unittest.TestCase):
    def test_inline_status_layout_and_recovery(self):
        self.path.write_text('Before **run** after.\n')
        def runner(prompt, activation, status):
            self.calls.append(dict(activation))
            activation['session_id'] = 'test-session'
            ticket = json.loads(prompt.split('\n\n', 1)[1])['ticket']
            self.assertEqual(ticket, '#KOL-1🎬 __run__ *⏳ Обрабатывается…* #KOL-1🛑')
            status('Первый шаг.\n\nВторой шаг.')
            self.assertIn('__run__\n\n*💭 Первый шаг.*', self.path.read_text())
            status('Почти готово.')
            self.assertEqual(self.path.read_text(),
                             'Before #KOL-1🎬 __run__ *💭 Почти готово.* #KOL-1🛑 after.\n')
            status('Один абзац\nс переносом в исходнике.')
            self.assertNotIn('\n', self.path.read_text().rstrip('\n'))
            return '*Готово.*'
        self.w.runner = runner
        with mock.patch.object(self.w, 'snapshot_b', return_value=False):
            self.assertTrue(self.run_ticket())
        expected = 'Before #KOL-1🎬 __run__ *◇ Готово.* #KOL-1🛑 after.\n'
        self.assertEqual(self.path.read_text(), expected)
        for path in self.h.state.glob('activation-*.json'):
            path.unlink()
        restarted = Watcher(self.root, settle=0, runner=runner)
        restarted.recover()
        recovered = next(iter(restarted.pending_b.values()))
        self.assertEqual(recovered['result'], 'Готово.')
        self.assertTrue(restarted.snapshot_b(recovered))
        self.assertEqual(self.path.read_text(), expected)
        self.assertEqual(len(self.calls), 1)

    def test_marker_free_pre_a_retry(self):
        with mock.patch.object(self.h, 'snapshot', side_effect=RuntimeError('offline')), self.assertLogs(level='ERROR'):
            self.run_ticket()
        self.assertEqual(self.calls, [])
        self.assertNotIn('obsta-', self.path.read_text())
        restarted = Watcher(self.root, settle=0, runner=self.w.runner)
        restarted.scan()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['ticket_id'], 'KOL-1')
        self.assertNotIn('obsta-', self.path.read_text())

    def test_marker_free_pre_a_cache_loss_does_not_guess(self):
        with mock.patch.object(self.h, 'snapshot', side_effect=RuntimeError('offline')), self.assertLogs(level='ERROR'):
            self.run_ticket()
        for path in self.h.state.glob('activation-*.json'):
            path.unlink()
        restarted = Watcher(self.root, settle=0, runner=self.w.runner)
        restarted.scan()
        restarted.scan()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.h.records(), [])

    def test_marker_free_recovery_rejects_changed_request(self):
        with mock.patch.object(self.w, 'snapshot_b', return_value=False):
            self.run_ticket()
        self.path.write_text(self.path.read_text().replace('🎬 __run__', '🎬 human edit'))
        for path in self.h.state.glob('activation-*.json'):
            path.unlink()
        restarted = Watcher(self.root, settle=0, runner=self.w.runner)
        restarted.recover()
        self.assertEqual(restarted.pending_b, {})
        self.assertIn('human edit', self.path.read_text())
        again = Watcher(self.root, settle=0, runner=self.w.runner)
        again.recover()
        self.assertEqual(again.pending_b, {})
        self.assertEqual(len(self.calls), 1)

    def test_self_test_selection(self):
        gate = [test._testMethodName for test in self_test_suite()]
        complete = [test._testMethodName for test in self_test_suite(full=True)]
        self.assertEqual(gate, list(RESTART_TESTS))
        self.assertEqual(len(gate), len(set(gate)))
        self.assertLess(len(gate), len(complete))
        self.assertTrue(set(gate).issubset(complete))
        self.assertIn('test_each_git_stage_failure_and_next_request', complete)
        self.assertIn('test_cli_full_cycle_and_resume', complete)

    def test_multiline_result_and_recovery_without_result_marker(self):
        response = '*Первый абзац.*\n\n## *Детали*\n\n- *Проверен `a_b`.*\n- *Второй пункт.*\n\n```python\nx = "**literal**"\n```'
        expected = '*◇ Первый абзац.*\n\n## *Детали*\n\n- *Проверен `a_b`.*\n- *Второй пункт.*\n\n```python\nx = "**literal**"\n```'
        def runner(prompt, activation, status):
            ticket = json.loads(prompt.split('\n\n', 1)[1])['ticket']
            self.assertNotIn('obsta-', ticket)
            self.assertIn('🎬 __run__', ticket)
            self.assertNotIn('⏳ run', ticket)
            self.assertIn('*⏳ Обрабатывается…*', ticket)
            status('*Шаг один.*\n\n*Шаг два.*')
            return response
        self.w.runner = runner
        with mock.patch.object(self.w, 'snapshot_b', return_value=False):
            self.run_ticket()
        saved = self.path.read_text()
        self.assertIn(expected, saved)
        self.assertIn('__run__\n\n' + expected + '\n#KOL-1🛑', saved)
        self.assertNotIn('obsta-result:', saved)
        self.assertNotIn('Шаг один', saved)
        self.assertEqual(find_commands(saved), [])
        for path in self.h.state.glob('activation-*.json'):
            path.unlink()
        restarted = Watcher(self.root, settle=0, runner=runner)
        restarted.recover()
        recovered = next(iter(restarted.pending_b.values()))
        self.assertEqual(recovered['outcome'], 'success')
        restarted.snapshot_b(recovered)
        self.assertIn(expected, self.path.read_text())
        self.assertEqual(self.path.read_text().count('◇'), 1)

    def test_hidden_pending_recovery_without_cache(self):
        with mock.patch.object(self.h, 'snapshot', side_effect=RuntimeError('offline')), self.assertLogs(level='ERROR'):
            self.run_ticket()
        self.assertNotIn('⏳ run', self.path.read_text())
        self.assertNotIn('obsta-', self.path.read_text())
        # Old vaults can still contain the former pending format.
        self.path.write_text(self.path.read_text().replace('🎬 __run__', '🎬 run' + pending_marker('run')))
        for path in self.h.state.glob('activation-*.json'):
            path.unlink()
        restarted = Watcher(self.root, settle=0, runner=self.w.runner)
        restarted.scan()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['ticket_id'], 'KOL-1')
        self.assertNotIn('obsta-pending:', self.path.read_text())

    def test_public_status_rendering(self):
        self.assertEqual(italic('💭 *Прочитаю заметки.*'), '*💭 Прочитаю заметки.*')
        self.assertEqual(italic('◇ *Готово.*'), '*◇ Готово.*')
        self.assertIn(r'a\_b\_c', italic('File a_b_c'))
        message = italic('💭 **run** `a_b` <tag> %% hidden %%')
        self.assertNotIn('&#', message)
        self.assertEqual(find_commands(message), [])
        self.assertIn('run', visible(message))

    def test_final_status_without_request_decorations(self):
        for outcome, icon in [('success', '◇'), ('error', '❌')]:
            with self.subTest(outcome=outcome):
                self.path.write_text('**run**\nOutside.\n')
                def runner(prompt, activation, status):
                    if outcome == 'error':
                        raise RuntimeError('*Ошибка.*')
                    return '*Готово.*'
                self.w.runner = runner
                self.assertTrue(self.run_ticket())
                text = self.path.read_text()
                body = 'Готово.' if outcome == 'success' else 'Ошибка.'
                self.assertIn(f'*{icon} {body}*', text)
                self.assertIn('🎬 __run__ ', text)
                self.assertNotIn('✅', text)
                self.assertNotIn('⏳', text)
                self.assertNotIn('&#42;', text)
                self.assertNotIn('obsta-status:', text)
                self.assertNotIn('obsta-result:', text)
                self.assertNotIn('obsta-pending:', text)
                self.assertNotIn('obsta-activation:', text)
                self.assertNotIn('Working', text)
                self.assertEqual(find_commands(text), [])

    def test_hidden_markers_and_legacy_status_update(self):
        self.run_ticket()
        text = self.path.read_text()
        self.assertNotIn('<!-- obsta-', text)
        self.assertNotIn('obsta-', visible(text))
        activation = self.calls[0]
        legacy = re.sub(r'%% (obsta-[^\n]*?) %%', r'<!-- \1 -->', text)
        legacy = legacy.replace('*◇ Готово. Подробности.*',
                                '<!-- obsta-status:' + activation['activation_id'] + ' --> *◇ Готово. Подробности.*')
        self.path.write_text(legacy)
        self.w.note_update(activation, '💭 *Обновлено.*')
        updated = self.path.read_text()
        self.assertNotIn('obsta-status:', updated)
        self.assertIn('*💭 Обновлено.*', updated)

    def test_status_preserves_human_edit_and_other_ticket(self):
        self.run_ticket()
        activation = self.calls[0]
        text = self.path.read_text().replace('*◇ Готово. Подробности.*', '*◇ Правка человека.*')
        other = '\n#KOL-2🎬 human\n*◇ Готово. Подробности.*\n#KOL-2🛑\n'
        self.path.write_text(text + other)
        self.w.note_update(activation, '💭 Новый статус')
        self.w.note_update(activation, '💭 Следующий статус')
        updated = self.path.read_text()
        self.assertIn('*◇ Правка человека.*', updated)
        self.assertIn(other, updated)
        self.assertNotIn('Новый статус', updated)
        self.assertIn('*💭 Следующий статус*', updated)
        self.assertNotIn('obsta-status:', updated)

    def test_result_recovery_both_marker_formats(self):
        with mock.patch.object(self.w, 'snapshot_b', return_value=False):
            self.run_ticket()
        saved = self.path.read_text()
        for path in self.h.state.glob('activation-*.json'):
            path.unlink()
        for icon in ('◇', '\u27a4'):
            for marker in (None, 'obsidian', 'html'):
                with self.subTest(marker=marker):
                    text = saved
                    if marker:
                        key = self.calls[0]['activation_id']
                        text = text.replace('*◇ Готово. Подробности.*',
                                            service_marker('status', key) + ' *◇ Готово. Подробности.*\n' + service_marker('result', key + ':success'))
                    if marker == 'html':
                        text = re.sub(r'%% (obsta-[^\n]*?) %%', r'<!-- \1 -->', text)
                    text = text.replace('◇', icon)
                    self.path.write_text(text)
                    restarted = Watcher(self.root, settle=0, runner=self.w.runner)
                    restarted.recover()
                    recovered = next(iter(restarted.pending_b.values()))
                    self.assertEqual(recovered['outcome'], 'success')
                    self.assertEqual(recovered['result'], 'Готово. Подробности.')
        self.assertEqual(len(self.calls), 1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        git(self.root, 'init', '-q')
        with (self.root / '.git/config').open('a') as config_file:
            config_file.write('\n[user]\n name = obsta test\n email = test@example.invalid\n[commit]\n gpgsign = false\n[core]\n hooksPath = /dev/null\n')
        (self.root / INSTRUCTION).parent.mkdir()
        (self.root / INSTRUCTION).write_text('Test instruction', encoding='utf-8')
        (self.root / 'Configs').mkdir()
        (self.root / 'Configs/obsta.md').write_text('`nickname` = `KOL`\n`subagent` = codex\n', encoding='utf-8')
        self.path = self.root / 'task.md'
        self.path.write_text('**run**\nOutside.\n', encoding='utf-8')
        self.calls = []
        def runner(prompt, activation, status):
            self.calls.append(dict(activation))
            activation['session_id'] = 'test-session'
            status('Working')
            return 'Готово. Подробности.'
        self.w = Watcher(self.root, settle=0, timeout=1, runner=runner)
        self.h = self.w.history

    def run_ticket(self, watcher=None):
        watcher = watcher or self.w
        text = self.path.read_text(encoding='utf-8')
        return watcher.process(self.path, text, find_commands(text)[0])

    def snap(self):
        return self.h.snapshot(dict(version=1, ticket_id='KOL-1', activation_id='test', phase='A',
                                    note='task.md', parent_ticket_id=None, session_id=None, outcome='pending'), '#KOL-1 ✍️ run')

    def tree_files(self):
        return set(git(self.root, 'ls-tree', '-rz', '--name-only', REF, binary=True).split(b'\0')) - {b''}

    def test_signals_and_boundaries(self):
        text = '---\nx: **no**\n---\n```\n#X-1🎬 **no**\n```\n`**no**` **yes**\n- [x] run\n'
        self.assertEqual([x[2] for x in find_commands(text)], ['yes', 'run'])
        self.assertEqual(find_commands('__ordinary bold__'), [])
        text = '__ordinary__ **Запрос с `**` и __жирным__** __текст__'
        command = find_commands(text)
        self.assertEqual(len(command), 1)
        self.assertEqual(command[0][2], 'Запрос с `**` и __жирным__')
        self.assertEqual(text[command[0][0]:command[0][1]], '**Запрос с `**` и __жирным__**')
        self.assertEqual(find_commands('***italic*** \\**escaped** ==later=='), [])
        self.assertEqual(find_commands('`multiline\n**not a command**\n`'), [])
        self.assertEqual(find_commands('````\n```\n**not a command**\n````'), [])
        text = '#KOL-1🎬\nhello\n#KOL-1/2🎬 child #KOL-1/2🛑\n**more**\n#KOL-1🛑'
        self.assertEqual(len(boundaries(text)), 2)
        self.assertEqual(find_commands(text)[0][2], 'more')
        for invalid in ['#KOL-01🎬 a #KOL-01🛑', '#1🎬 #2🛑', '#1🎬 #1🎬 #1🛑 #1🛑', '#1🎬']:
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                boundaries(invalid)

    def test_config(self):
        self.assertEqual(config(self.root)['nickname'], 'KOL')
        for text in ['`nickname` = kol\n`subagent` = codex', '`nickname` = KOL\n`nickname` = ABC\n`subagent` = codex',
                     '`nickname` = KOL\n`subagent` = unknown']:
            (self.root / 'Configs/obsta.md').write_text(text, encoding='utf-8')
            with self.assertRaises(RuntimeError):
                config(self.root)

    def test_local_config(self):
        shared = self.root / 'Configs/obsta.md'
        local = self.root / 'Configs/obsta.local.md'
        shared.write_text('`nickname` = ``\n`subagent` = codex\n', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'Set nickname'):
            config(self.root)
        local.write_text('`nickname` = ABC\n', encoding='utf-8')
        self.assertEqual(config(self.root), dict(nickname='ABC', subagent='codex'))
        for text in ('`nickname` = abc', '`nickname` = ``',
                     '`nickname` = ABC\n`nickname` = DEF',
                     '`nickname` = ABC\n`subagent` = unknown'):
            local.write_text(text, encoding='utf-8')
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                config(self.root)
        local.write_text('`nickname` = ABC\n', encoding='utf-8')
        (self.root / '.gitignore').write_text(
            '/Configs/obsta.local.md\n# obsta-history: /Configs/obsta.local.md\n', encoding='utf-8')
        self.assertEqual(git(self.root, 'ls-files', '--others', '--exclude-standard', '--',
                             'Configs/obsta.local.md'), '')
        self.assertTrue(self.run_ticket())
        self.assertEqual(self.calls[-1]['ticket_id'], 'ABC-1')
        self.assertIn(b'Configs/obsta.local.md', self.tree_files())
        local.unlink()
        shared.write_text('`nickname` = DEF\n`subagent` = codex\n', encoding='utf-8')
        self.assertEqual(config(self.root)['nickname'], 'DEF')

    def test_orphan_same_tree_and_lost_index(self):
        a = self.snap()
        self.assertEqual(git(self.root, 'show', '-s', '--format=%P', a).strip(), '')
        tree = git(self.root, 'rev-parse', a + '^{tree}')
        b = self.snap()
        self.assertNotEqual(a, b)
        self.assertEqual(git(self.root, 'rev-parse', b + '^{tree}'), tree)
        self.assertEqual(git(self.root, 'show', '-s', '--format=%P', b).strip(), a)
        self.h.index.unlink()
        self.snap()
        self.assertEqual(git(self.root, 'rev-parse', REF + '^{tree}'), tree)
        with self.assertRaises(subprocess.CalledProcessError):
            git(self.root, 'rev-parse', '--verify', 'HEAD')

    def test_user_head_and_index_bytes(self):
        (self.root / 'delete').write_text('old')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-qm', 'initial')
        (self.root / 'delete').unlink()
        self.path.write_text('staged')
        git(self.root, 'add', 'task.md')
        self.path.write_text('unstaged')
        (self.root / 'новый\n[*]?.md').write_text('new')
        head = git(self.root, 'rev-parse', 'HEAD')
        index = (self.h.gitdir / 'index').read_bytes()
        self.snap()
        self.assertEqual(index, (self.h.gitdir / 'index').read_bytes())
        self.assertEqual(head, git(self.root, 'rev-parse', 'HEAD'))
        self.assertEqual(git(self.root, 'show', REF + ':task.md'), 'unstaged')
        self.assertNotIn(b'delete', self.tree_files())
        self.assertIn('новый\n[*]?.md'.encode(), self.tree_files())

    def test_ignore_force_and_service_exclusion(self):
        (self.root / '.gitignore').write_text('*.secret\n# obsta-history: /*.secret\n# obsta-history: /.git/**\n# obsta-history: /*.obsta-tmp\n# obsta-history: /no-matches\n')
        for name in ['x.secret', 'пробел [*]?\n.secret', 'y.obsta-tmp']:
            (self.root / name).write_text('value')
        (self.root / 'link.secret').symlink_to('/nonexistent')
        (self.root / 'nested').mkdir()
        (self.root / 'nested/.gitignore').write_text('hidden\n')
        (self.root / 'nested/hidden').write_text('hidden')
        global_ignore = self.h.state / 'global-ignore'
        global_ignore.write_text('global-hidden\n')
        git(self.root, 'config', 'core.excludesFile', str(global_ignore))
        (self.root / 'global-hidden').write_text('hidden')
        (self.h.gitdir / 'info/exclude').write_text('info-hidden\n')
        (self.root / 'info-hidden').write_text('hidden')
        self.snap()
        files = self.tree_files()
        self.assertIn(b'x.secret', files)
        self.assertIn('пробел [*]?\n.secret'.encode(), files)
        self.assertIn(b'link.secret', files)
        for name in [b'y.obsta-tmp', b'nested/hidden', b'global-hidden', b'info-hidden']:
            self.assertNotIn(name, files)
        self.assertFalse(any(b'.git/' in name for name in files))
        (self.root / '.gitignore').write_text('*.secret\n')
        self.snap()
        self.assertNotIn(b'x.secret', self.tree_files())

    def test_generated_block_and_sources(self):
        prefix, suffix = b'# personal\r\n', b'# user\r\n/private\r\n'
        ignore = self.root / '.gitignore'
        ignore.write_bytes(prefix + b'# BEGIN OBSTA DEFAULT\r\nold\r\n# END OBSTA DEFAULT\r\n' + suffix)
        (self.root / 'pair.py.md').write_text('source')
        (self.root / 'pair.py').write_text('code')
        self.path.write_text('---\nSources:\n  - "special [*]?.py"\n  - /absolute.py\n---\ntext')
        for name in ['special [*]?.py', 'absolute.py', 'unlinked.py', 'private']:
            (self.root / name).write_text('code')
        self.snap()
        self.assertTrue(ignore.read_bytes().startswith(prefix))
        self.assertTrue(ignore.read_bytes().endswith(b'# END OBSTA DEFAULT\r\n' + suffix))
        for name in [b'pair.py', b'special [*]?.py', b'absolute.py']:
            self.assertIn(name, self.tree_files())
        self.assertNotIn(b'unlinked.py', self.tree_files())
        self.assertNotIn(b'private', self.tree_files())
        self.path.write_text('---\nSources: ../../outside.py\n---\n')
        with self.assertRaises(RuntimeError):
            self.snap()

    def test_absent_and_malformed_ignore_block(self):
        ignore = self.root / '.gitignore'
        original = b'# no block\r\n*.tmp'
        ignore.write_bytes(original)
        self.snap()
        self.assertEqual(ignore.read_bytes(), original)
        for data in ['# BEGIN OBSTA DEFAULT\n', '# END OBSTA DEFAULT\n',
                     '# BEGIN OBSTA DEFAULT\n# BEGIN OBSTA DEFAULT\n# END OBSTA DEFAULT\n']:
            ignore.write_text(data)
            with self.assertRaises(RuntimeError):
                self.snap()

    def test_selected_history_worktree(self):
        self.snap()
        git(self.root, 'add', '-A')
        git(self.root, 'checkout', '-q', 'obsta-history')
        with self.assertRaisesRegex(RuntimeError, 'checked out'):
            self.snap()

    def test_other_worktree(self):
        self.snap()
        with tempfile.TemporaryDirectory() as folder:
            other = Path(folder) / 'checkout'
            git(self.root, 'worktree', 'add', '-q', str(other), 'obsta-history')
            with self.assertRaisesRegex(RuntimeError, 'checked out'):
                self.snap()
            h = History(other)
            self.assertEqual(h.gitdir, Path(git(other, 'rev-parse', '--absolute-git-dir').strip()))
            git(self.root, 'worktree', 'remove', '--force', str(other))

    def test_conflict_ref_retry(self):
        original = self.h.run
        conflicts = []
        def racing(*args, **kw):
            if args[0] == 'update-ref' and not conflicts:
                conflicts.append(True)
                git(self.root, 'update-ref', REF, args[2])
            return original(*args, **kw)
        with mock.patch.object(self.h, 'run', side_effect=racing):
            self.snap()
        self.assertEqual(len(self.h.records()), 2)

    def test_three_ref_conflicts(self):
        original = self.h.run
        count = []
        def racing(*args, **kw):
            if args[0] == 'update-ref':
                count.append(1)
                git(self.root, 'update-ref', REF, args[2])
            return original(*args, **kw)
        with mock.patch.object(self.h, 'run', side_effect=racing), self.assertRaisesRegex(RuntimeError, 'three'):
            self.snap()
        self.assertEqual(len(count), 3)

    def test_ids_history_and_children(self):
        self.path.write_text('#90🎬 old #90🛑\n#KOL-9🎬 root #KOL-9🛑\n#KOL-1/20🎬 child #KOL-1/20🛑\n#ABC-100🎬 other #ABC-100🛑')
        self.snap()
        self.assertEqual(self.h.allocate('KOL'), 'KOL-10')
        # History subjects reserve IDs even after note deletion.
        self.h.snapshot({}, '#KOL-10 ✍️ deleted ticket')
        self.path.unlink()
        self.assertEqual(self.h.allocate('KOL'), 'KOL-11')
        # Max IDs in historical note trees are also reservations.
        self.assertEqual(self.h.allocate('KOL', 'KOL-1'), 'KOL-1/21')

    def test_history_error_is_not_empty(self):
        with mock.patch.object(self.h, 'tip', side_effect=RuntimeError('Git unavailable')):
            with self.assertRaises(RuntimeError):
                self.h.allocate('KOL')

    def test_full_cycle_and_restart_no_duplicate(self):
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-qm', 'user')
        head = git(self.root, 'rev-parse', 'HEAD')
        index = (self.h.gitdir / 'index').read_bytes()
        self.assertTrue(self.run_ticket())
        records = self.h.records()
        self.assertEqual([r['phase'] for r in records], ['B', 'A'])
        self.assertEqual(records[0]['snapshot_a'], records[1]['oid'])
        self.assertEqual(records[0]['outcome'], 'success')
        self.assertIn('#KOL-1🎬 __run__ *◇', self.path.read_text())
        self.assertIn('*◇ Готово. Подробности.*', self.path.read_text())
        self.assertEqual(index, (self.h.gitdir / 'index').read_bytes())
        self.assertEqual(head, git(self.root, 'rev-parse', 'HEAD'))
        watcher = Watcher(self.root, settle=0, runner=self.w.runner)
        watcher.scan()
        watcher.scan()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.h.records()), 2)
        self.assertNotIn('human_diff', self.calls[0])

    def test_repeat_question_resume_and_legacy(self):
        self.run_ticket()
        first = self.calls[0]['activation_id']
        self.path.write_text(self.path.read_text().replace('#KOL-1🛑', '**run**\n#KOL-1🛑'))
        self.run_ticket()
        self.assertEqual(self.calls[-1]['ticket_id'], 'KOL-1')
        self.assertEqual(self.calls[-1]['session_id'], 'test-session')
        self.assertNotEqual(first, self.calls[-1]['activation_id'])
        self.assertEqual(self.path.read_text().count('__run__'), 2)
        self.assertEqual(find_commands(self.path.read_text()), [])
        self.path.write_text('#90🎬 old\n**run**\n#90🛑')
        self.h.snapshot(dict(version=1, ticket_id='90', session_id='legacy-session'), '#90 🤖 legacy')
        self.run_ticket()
        self.assertEqual(self.calls[-1]['ticket_id'], '90')

    def test_nested_and_missing_resume(self):
        self.path.write_text('#KOL-1🎬\n**nested**\nHuman text.\n#KOL-1🛑\n**next**')
        self.assertFalse(self.run_ticket())
        self.assertIn('**nested**', self.path.read_text())
        text = self.path.read_text()
        self.assertTrue(self.w.process(self.path, text, find_commands(text)[-1]))
        self.assertEqual(self.calls[-1]['comment'], 'next')
        self.path.write_text('#90🎬\n**resume**\n#90🛑')
        self.assertFalse(self.run_ticket())
        self.assertIn('Нет подтверждённой', self.path.read_text())

    def test_nested_fork_and_resume(self):
        self.run_ticket()
        # An inline signal in an earlier answer forks the confirmed parent.
        self.path.write_text(self.path.read_text().replace('Подробности.', 'до **child** после.'))
        parent_before = self.path.read_text()
        self.assertTrue(self.run_ticket())
        child = self.calls[-1]
        self.assertEqual(child['ticket_id'], 'KOL-1/1')
        self.assertEqual(child['parent_ticket_id'], 'KOL-1')
        self.assertEqual(child['fork_parent_session_id'], 'test-session')
        self.assertIsNone(child['session_id'])
        text = self.path.read_text()
        region = next(r for r in boundaries(text) if r[0] == 'KOL-1/1')
        self.assertIn('__child__', text[region[2]:region[3]])
        self.assertEqual(text[:region[1]], parent_before.split('**child**')[0])
        self.assertEqual(text[region[4]:], parent_before.split('**child**')[1])
        records = self.h.records()
        self.assertEqual(records[0]['fork_parent_session_id'], 'test-session')
        self.assertEqual(records[1]['fork_parent_session_id'], 'test-session')
        # Restart and continue the child: use its session, with no second fork.
        self.path.write_text(text.replace('#KOL-1/1🛑', '**follow**\n#KOL-1/1🛑'))
        watcher = Watcher(self.root, runner=self.w.runner)
        self.assertTrue(self.run_ticket(watcher))
        self.assertEqual(self.calls[-1]['ticket_id'], 'KOL-1/1')
        self.assertEqual(self.calls[-1]['session_id'], 'test-session')
        self.assertNotIn('fork_parent_session_id', self.calls[-1])
        text = self.path.read_text()
        self.path.write_text(text.replace('#KOL-1🛑', '**parent follow**\n#KOL-1🛑'))
        self.assertTrue(self.run_ticket(watcher))
        self.assertEqual(self.calls[-1]['ticket_id'], 'KOL-1')
        self.assertNotIn('fork_parent_session_id', self.calls[-1])

    def test_fork_unavailable_preserves_signal(self):
        self.run_ticket()
        self.path.write_text(self.path.read_text().replace('Подробности.', '**child** tail.'))
        watcher = Watcher(self.root)
        with mock.patch.object(watcher, 'executor_args', side_effect=RuntimeError('unsupported')):
            self.assertFalse(self.run_ticket(watcher))
        self.assertIn('**child**', self.path.read_text())
        self.assertNotIn('#KOL-1/1🎬', self.path.read_text())
        self.assertEqual(len(self.h.records()), 2)

    def test_checklist(self):
        self.path.write_text('- [x] parent\n  - child\n    detail\n- next\n')
        self.run_ticket()
        text = self.path.read_text()
        self.assertIn('- [ ] parent\n  - child\n    detail', text)
        self.assertTrue(text.endswith('- next\n'))

    def test_each_git_stage_failure_and_next_request(self):
        for stage in ['read-tree', 'ls-files', 'update-index', 'add', 'write-tree', 'commit-tree', 'update-ref']:
            with self.subTest(stage=stage):
                self.path.write_text('**first**\n**second**')
                self.h.index.unlink(missing_ok=True)
                original = self.h.run
                def failed(*args, **kw):
                    if args[0] == stage:
                        raise RuntimeError('injected ' + stage)
                    return original(*args, **kw)
                # update-index is reached when an already cached path becomes ignored.
                if stage == 'update-index':
                    (self.root / 'ignored').write_text('x')
                    self.snap()
                    (self.root / '.gitignore').write_text('ignored\n')
                before = len(self.calls)
                with mock.patch.object(self.h, 'run', side_effect=failed), self.assertLogs(level='ERROR'):
                    self.assertFalse(self.run_ticket())
                self.assertEqual(len(self.calls), before)
                text = self.path.read_text()
                self.assertTrue(self.w.process(self.path, text, find_commands(text)[0]))
                self.assertEqual(len(self.calls), before + 1)
                (self.root / '.gitignore').unlink(missing_ok=True)
                self.w.prepared.clear()

    def test_b_failure_recovery_only_saves(self):
        original = self.h.snapshot
        def fail_b(meta, message):
            if meta['phase'] == 'B':
                raise RuntimeError('injected B failure')
            return original(meta, message)
        with mock.patch.object(self.h, 'snapshot', side_effect=fail_b), self.assertLogs(level='ERROR'):
            self.run_ticket()
        self.assertEqual(len(self.calls), 1)
        self.assertIn('Готово', self.path.read_text())
        watcher = Watcher(self.root, settle=0, runner=self.w.runner)
        watcher.scan()
        self.assertEqual(len(self.calls), 1)
        records = self.h.records()
        self.assertEqual([r['phase'] for r in records], ['B', 'A'])
        self.assertTrue(records[0]['late'])

    def test_unknown_a_does_not_execute(self):
        self.path.write_text('#KOL-1🎬 ⏳ run ⏳ #KOL-1🛑')
        self.snap()
        watcher = Watcher(self.root, settle=0, runner=self.w.runner)
        watcher.scan()
        watcher.scan()
        self.assertEqual(self.calls, [])
        self.assertIn('неизвестен', self.path.read_text())

    def test_live_executor_blocks_serial_queue(self):
        self.path.write_text('#KOL-1🎬 ⏳ run ⏳ #KOL-1🛑')
        self.snap()
        self.w.save_journal(dict(activation_id='test', stage='running', ticket_id='KOL-1', note='task.md',
                                 pid=os.getpid(), process_identity=process_identity(os.getpid())))
        with self.assertLogs(level='ERROR'):
            self.assertFalse(self.w.recover())

    def test_executor_error_timeout_and_deleted_marker(self):
        for case in ['error', 'timeout', 'marker', 'note', 'concurrent']:
            with self.subTest(case=case):
                self.path.write_text('**run**\nOutside.\n')
                def runner(prompt, activation, status):
                    if case == 'error':
                        raise RuntimeError('runner failed')
                    if case == 'timeout':
                        raise subprocess.TimeoutExpired('fake', 1)
                    if case == 'note':
                        self.path.unlink()
                    elif case == 'marker':
                        self.path.write_text('Human replacement')
                    elif case == 'concurrent':
                        self.path.write_text(self.path.read_text() + 'Concurrent human edit\n')
                    return 'Done'
                self.w.runner = runner
                if case in ('error', 'timeout', 'marker', 'note'):
                    with self.assertLogs(level='ERROR'):
                        self.run_ticket()
                else:
                    self.run_ticket()
                self.assertEqual(self.h.records()[0]['phase'], 'B')
                self.assertEqual(self.h.records()[0]['outcome'], 'error' if case in ('error', 'timeout') else 'success')
                if case == 'note':
                    self.assertFalse(self.path.exists())
                elif case == 'marker':
                    self.assertEqual(self.path.read_text(), 'Human replacement')
                elif case == 'concurrent':
                    self.assertTrue(self.path.read_text().endswith('Concurrent human edit\n'))

    def test_concurrent_atomic_replace(self):
        self.path.write_text('new human text')
        with self.assertRaises(RuntimeError):
            atomic_replace(self.path, 'old', 'agent text')
        self.assertEqual(self.path.read_text(), 'new human text')

    def test_pre_a_cache_loss_keeps_id(self):
        self.path.write_text('#KOL-7🎬 ⏳ run ⏳ #KOL-7🛑')
        self.w.scan()
        self.assertEqual(self.calls[0]['ticket_id'], 'KOL-7')

    def test_restart_validation(self):
        source = self.root / 'source.py'
        source.write_text('# old')
        self.w.source = source
        self.w.source_hash = hashlib.sha256(source.read_bytes()).digest()
        self.assertFalse(self.w.restart_requested())
        source.write_text('import sys; assert sys.argv[1:] == ["--self-test"]')
        self.assertFalse(self.w.restart_requested())
        self.w.last_accounted = True
        self.w.saved_source_hash = hashlib.sha256(source.read_bytes()).digest()
        self.assertTrue(self.w.restart_requested())
        source.write_text('raise SystemExit(1)')
        self.w.saved_source_hash = hashlib.sha256(source.read_bytes()).digest()
        with self.assertLogs(level='ERROR'):
            self.assertFalse(self.w.restart_requested())
        with mock.patch('subprocess.run') as run:
            self.assertFalse(self.w.restart_requested())
            run.assert_not_called()

    def test_crash_boundaries(self):
        with mock.patch(__name__ + '.atomic_replace', side_effect=RuntimeError('concurrent note edit')), self.assertLogs(level='ERROR'):
            self.assertFalse(self.run_ticket())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.path.read_text(), '**run**\nOutside.\n')
        original = self.w.save_journal
        def fail_after_a(activation):
            if activation['stage'] == 'A':
                raise OSError('crash after A')
            return original(activation)
        with mock.patch.object(self.w, 'save_journal', side_effect=fail_after_a), self.assertLogs(level='ERROR'):
            self.assertFalse(self.run_ticket())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.h.records()[0]['phase'], 'A')
        restarted = Watcher(self.root, settle=0, runner=self.w.runner)
        restarted.scan()
        self.assertEqual(self.calls, [])
        self.assertIn('неизвестен', self.path.read_text())

    def test_post_b_cache_failure_no_duplicate(self):
        original = self.w.save_journal
        def fail_after_b(activation):
            if activation['stage'] == 'done':
                raise OSError('crash after B')
            return original(activation)
        with mock.patch.object(self.w, 'save_journal', side_effect=fail_after_b), self.assertLogs(level='ERROR'):
            self.run_ticket()
        self.assertEqual([r['phase'] for r in self.h.records()], ['B', 'A'])
        restarted = Watcher(self.root, settle=0, runner=self.w.runner)
        restarted.scan()
        restarted.scan()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.h.records()), 2)

    def test_completed_b_with_pending_note_never_reexecutes(self):
        original = self.w.note_update
        def fail_final(activation, status=None, outcome=None):
            return False if outcome else original(activation, status, outcome)
        with mock.patch.object(self.w, 'note_update', side_effect=fail_final):
            self.run_ticket()
        self.assertIn('*💭 Working*', self.path.read_text())
        self.assertNotIn('obsta-', self.path.read_text())
        for path in self.h.state.glob('activation-*.json'):
            path.unlink()
        restarted = Watcher(self.root, settle=0, runner=self.w.runner)
        restarted.scan()
        restarted.scan()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.h.records()), 2)

    def test_restart_timeout_and_changed_hash(self):
        source = self.root / 'candidate.py'
        source.write_text('candidate')
        self.w.source, self.w.source_hash = source, b'old'
        self.w.last_accounted = True
        self.w.saved_source_hash = hashlib.sha256(source.read_bytes()).digest()
        with mock.patch('subprocess.run', side_effect=subprocess.TimeoutExpired('test', 60)), self.assertLogs(level='ERROR'):
            self.assertFalse(self.w.restart_requested())
        self.w.rejected_hash = None
        def changing(*args, **kwargs):
            source.write_text('changed during test')
            return subprocess.CompletedProcess(args[0], 0, '', '')
        with mock.patch('subprocess.run', side_effect=changing), self.assertLogs(level='ERROR'):
            self.assertFalse(self.w.restart_requested())

    def fake_codex(self, mode='success'):
        folder = self.h.state / 'bin'
        folder.mkdir(exist_ok=True)
        executable = folder / 'codex'
        script = r'''import json, sys, time
from pathlib import Path
if '--help' in sys.argv:
    print('--json')
    raise SystemExit(0)
sys.stdin.read()
state = Path('.git/obsta-state')
count = state / 'runner-count'
count.write_text(str(int(count.read_text()) + 1) if count.exists() else '1')
with (state / 'runner-argv').open('a') as f:
    f.write(json.dumps(sys.argv) + '\n')
def emit(event):
    print(json.dumps(event), flush=True)
session = 'child-session' if 'fork' in sys.argv else sys.argv[sys.argv.index('--json') + 1] if 'resume' in sys.argv else 'test-session'
if MODE == 'same-parent':
    session = sys.argv[sys.argv.index('--json') + 1]
if MODE != 'no-session':
    emit({'type': 'thread.started', 'thread_id': session})
emit({'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'PRIVATE'}})
emit({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Public progress'}})
if MODE == 'timeout':
    time.sleep(5)
if MODE == 'exit':
    raise SystemExit(3)
if MODE == 'malformed':
    print('not json', flush=True)
    raise SystemExit(0)
response = json.dumps({'outcome': 'error' if MODE == 'blocked' else 'success', 'response': 'Fixture result'})
emit({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': response}})
emit({'type': 'turn.completed'})
'''
        executable.write_text('#!' + sys.executable + '\nMODE = ' + repr(mode) + '\n' + script)
        executable.chmod(0o755)
        return dict(os.environ, PATH=str(folder) + os.pathsep + os.environ['PATH'])

    def test_jsonl_adapter(self):
        # This checks event handling; allow time for interpreter startup on macOS.
        self.w.timeout = 5
        activation = dict(activation_id=str(uuid.uuid4()), ticket_id='KOL-1', session_id=None, stage='running')
        statuses = []
        with mock.patch.dict(os.environ, self.fake_codex()):
            self.assertEqual(self.w.execute('prompt', activation, statuses.append), 'Fixture result')
            self.assertEqual(activation['session_id'], 'test-session')
            self.w.execute('resume prompt', activation, statuses.append)
        self.assertNotIn('PRIVATE', statuses)
        self.assertIn('Public progress', statuses)
        self.assertIn('resume', (self.h.state / 'runner-argv').read_text())

    def test_jsonl_fork(self):
        self.w.timeout = 5
        activation = dict(activation_id=str(uuid.uuid4()), ticket_id='KOL-1/1',
                          session_id=None, fork_parent_session_id='parent-session', stage='running')
        with mock.patch.dict(os.environ, self.fake_codex()):
            self.w.execute('fork prompt', activation, lambda _: None)
            self.assertEqual(activation['session_id'], 'child-session')
            self.w.execute('child resume', activation, lambda _: None)
        calls = [json.loads(line) for line in (self.h.state / 'runner-argv').read_text().splitlines()]
        self.assertEqual(calls[0][-4:], ['fork', '--json', 'parent-session', '-'])
        self.assertEqual(calls[1][-4:], ['resume', '--json', 'child-session', '-'])
        for mode in ('same-parent', 'no-session'):
            activation.update(session_id=None)
            with mock.patch.dict(os.environ, self.fake_codex(mode)), self.assertRaises(RuntimeError):
                self.w.execute('bad fork', activation, lambda _: None)

    def test_cli_full_cycle_and_fork(self):
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-qm', 'user head')
        before = (git(self.root, 'rev-parse', 'HEAD'), (self.h.gitdir / 'index').read_bytes())
        args = [sys.executable, str(Path(__file__).resolve()), '--vault', str(self.root),
                '--once', '--settle', '0', '--interval', '0.01']
        env = self.fake_codex()
        def run():
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
        run()
        self.path.write_text(self.path.read_text().replace('Fixture result', 'before **child** after', 1))
        run()
        records = self.h.records()
        self.assertEqual(records[0]['ticket_id'], 'KOL-1/1')
        self.assertEqual(records[0]['session_id'], 'child-session')
        self.assertEqual(records[0]['fork_parent_session_id'], 'test-session')
        # A fresh watcher resumes each discussion independently.
        self.path.write_text(self.path.read_text().replace('#KOL-1/1🛑', '**child follow**\n#KOL-1/1🛑'))
        run()
        self.assertEqual(self.h.records()[0]['session_id'], 'child-session')
        self.path.write_text(self.path.read_text().replace('#KOL-1🛑', '**parent follow**\n#KOL-1🛑'))
        run()
        self.assertEqual(self.h.records()[0]['session_id'], 'test-session')
        run()
        self.assertEqual((self.h.state / 'runner-count').read_text(), '4')
        self.assertEqual(len(self.h.records()), 8)
        self.assertEqual(before, (git(self.root, 'rev-parse', 'HEAD'), (self.h.gitdir / 'index').read_bytes()))

    def test_jsonl_failure_paths(self):
        for mode in ('timeout', 'exit', 'malformed', 'blocked'):
            with self.subTest(mode=mode):
                activation = dict(activation_id=str(uuid.uuid4()), ticket_id='KOL-1', session_id=None, stage='running')
                with mock.patch.dict(os.environ, self.fake_codex(mode)), self.assertRaises((RuntimeError, ValueError, subprocess.TimeoutExpired)):
                    self.w.execute('prompt', activation, lambda _: None)
                self.assertIsNone(process_identity(activation['pid']))

    def test_cli_full_cycle_and_resume(self):
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-qm', 'user head')
        before = (git(self.root, 'rev-parse', 'HEAD'), (self.h.gitdir / 'index').read_bytes())
        args = [sys.executable, str(Path(__file__).resolve()), '--vault', str(self.root), '--once', '--settle', '0', '--interval', '0.01']
        env = self.fake_codex()
        for _ in range(2):
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.h.state / 'runner-count').read_text(), '1')
        self.assertEqual([r['phase'] for r in self.h.records()], ['B', 'A'])
        self.path.write_text(self.path.read_text().replace('#KOL-1🛑', '**follow up**\n#KOL-1🛑'))
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.h.state / 'runner-count').read_text(), '2')
        self.assertEqual([r['phase'] for r in self.h.records()], ['B', 'A', 'B', 'A'])
        self.assertEqual(before, (git(self.root, 'rev-parse', 'HEAD'), (self.h.gitdir / 'index').read_bytes()))

    def test_invalid_pathspec_cancels_snapshot(self):
        (self.root / '.gitignore').write_text('# obsta-history: :(unknown)bad\n')
        with self.assertRaises(subprocess.CalledProcessError):
            self.snap()
        self.assertIsNone(self.h.tip())

    def test_second_signal_after_scan_error(self):
        other = self.root / 'another.md'
        other.write_text('**other**')
        self.w.recovered = True
        self.w.seen = {p: (p.read_text(), 0) for p in notes(self.root)}
        original = self.w.process
        def fail(path, text, command):
            if path == other:
                raise RuntimeError('injected failure')
            return original(path, text, command)
        with mock.patch.object(self.w, 'process', side_effect=fail), self.assertLogs(level='ERROR'):
            self.assertTrue(self.w.scan())
        self.assertEqual(self.calls[-1]['comment'], 'run')

    def test_b_cache_loss_and_note_recovery(self):
        original = self.h.snapshot
        def fail_b(meta, message):
            if meta['phase'] == 'B':
                raise RuntimeError('injected failure')
            return original(meta, message)
        with mock.patch.object(self.h, 'snapshot', side_effect=fail_b), self.assertLogs(level='ERROR'):
            self.run_ticket()
        for path in self.h.state.glob('activation-*.json'):
            path.unlink()
        watcher = Watcher(self.root, settle=0, runner=self.w.runner)
        watcher.scan()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.h.records()[0]['phase'], 'B')
        self.assertTrue(self.h.records()[0]['late'])


if __name__ == '__main__':
    raise SystemExit(main())
