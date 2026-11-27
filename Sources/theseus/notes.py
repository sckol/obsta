"""Locate and update Markdown sections without rewriting surrounding bytes."""

from dataclasses import dataclass
from difflib import SequenceMatcher
import re


_FENCE = re.compile(rb"^ {0,3}(`{3,}|~{3,})([^\r\n]*)")
_HEADING = re.compile(rb"^ {0,3}(#{1,6})(?:[ \t]+(.*)|$)")
_SETEXT = re.compile(rb"^ {0,3}(=+|-+)[ \t]*$")
_CODE_OR_COMMENT = re.compile(
    rb"(?P<comment><!--.*?-->|%%.*?%%)|(?<!`)(?P<delimiter>`+)(?!`)"
    rb"(?:(?!\r?\n[ \t]*(?:\r?\n|#{1,6}(?:[ \t\r\n]|$))).)*?"
    rb"(?P=delimiter)(?!`)", re.S
)
_TICKET_END = "🛑".encode()
_TICKET = re.compile(rb"(?<![\\\w])#([^\s#]+?)(" + "🍏|\U0001f3ac|🛑".encode() + rb")")
_TICKET_ID = re.compile(rb"(?:[A-Z][A-Z0-9]*-)?[1-9][0-9]*(?:/[1-9][0-9]*)*")
_SERVICE = re.compile(
    rb"<!--[ \t]*obsta-[\w-]+:.*?-->|%%[ \t]*obsta-[\w-]+:.*?%%", re.S
)
_STATUS = re.compile(rb"^[ \t]*(?:>[ \t]*)?[*_]?[ \t]*(?:" + "⏳|💭|◇|❌".encode() + rb")")


@dataclass(frozen=True)
class _Section:
    titles: tuple
    levels: tuple
    body: int
    end: int


def _body_line(line):
    return line.rstrip(b"\r\n")


def _mask(data):
    return re.sub(rb"[^\r\n]", b" ", data)


def _lines(data, fences, offset=0):
    """Return physical lines with offsets and identify blocks excluded from parsing."""
    frontmatter = data.startswith((b"---\n", b"---\r\n"))
    fence = None
    for index, line in enumerate(data.splitlines(keepends=True)):
        active = not frontmatter and fence is None
        if frontmatter:
            if index and line.strip() in (b"---", b"..."):
                frontmatter = False
        else:
            match = _FENCE.match(line)
            if match:
                token, tail = match.groups()
                if fence is None and (token[:1] != b"`" or b"`" not in tail):
                    fence = token
                    fence_start = offset
                    active = False
                elif (fence is not None and token[:1] == fence[:1]
                      and len(token) >= len(fence) and not tail.strip()):
                    fence = None
                    fences.append((fence_start, offset + len(line)))
        yield offset, line, active
        offset += len(line)
    if frontmatter:
        raise ValueError("unclosed Markdown frontmatter")
    if fence is not None:
        fences.append((fence_start, offset))


def _protected(data, visible, lines):
    """Expand each outer ticket and service marker to its occupied physical lines."""
    spans, stack, seen = [], [], set()
    marks = tuple(_CODE_OR_COMMENT.finditer(visible))
    masked = _CODE_OR_COMMENT.sub(lambda m: _mask(m[0]), visible)
    for match in _TICKET.finditer(masked):
        ident, marker = match.groups()
        label = ident.decode("utf-8", errors="replace")
        if not _TICKET_ID.fullmatch(ident):
            raise ValueError(f"invalid ticket ID {label}")
        if marker != _TICKET_END:
            if ident in seen:
                raise ValueError(f"ambiguous ticket {label}")
            seen.add(ident)
            stack.append((ident, match.start()))
        else:
            if not stack or stack[-1][0] != ident:
                raise ValueError(f"ambiguous ticket boundary {label}")
            _, start = stack.pop()
            if not stack:
                spans.append((start, match.end()))
    if stack:
        raise ValueError(f"unclosed ticket {stack[-1][0].decode('ascii')}")
    spans.extend((match.start(), match.end()) for match in marks
                 if match["comment"] and _SERVICE.fullmatch(match[0]))
    spans.extend((start, start + len(line)) for start, line, active in lines
                 if active and _STATUS.match(line))
    result = []
    for start, end in sorted(spans):
        # Obsidian's ticket and service formats have single-line or multiline text.
        start = max(data.rfind(b"\n", 0, start), data.rfind(b"\r", 0, start)) + 1
        line_end = min((pos for pos in (data.find(b"\n", end), data.find(b"\r", end))
                        if pos >= 0), default=len(data))
        if end and data[end - 1:end] in (b"\r", b"\n"):
            line_end = end
        elif data[line_end:line_end + 2] == b"\r\n":
            line_end += 2
        elif line_end < len(data):
            line_end += 1
        if result and start < result[-1][1]:
            result[-1] = (result[-1][0], max(line_end, result[-1][1]))
        else:
            result.append((start, line_end))
    headings = _CODE_OR_COMMENT.sub(lambda m: _mask(m[0]) if m["comment"] else m[0], visible)
    return tuple(result), headings


def _title(raw):
    raw = raw.strip()
    for marker in (b"***", b"___", b"**", b"__", b"*", b"_"):
        if len(raw) > len(marker) * 2 and raw.startswith(marker) and raw.endswith(marker):
            raw = raw[len(marker):-len(marker)].strip()
            break
    return raw.decode("utf-8")


def _headings(lines, masked, protected):
    previous = None
    for start, line, active in lines:
        end = start + len(line)
        candidate = _body_line(masked[start:end])
        if not active or any(left < end and start < right for left, right in protected):
            previous = None
            continue
        atx = _HEADING.fullmatch(candidate)
        underline = _SETEXT.fullmatch(candidate)
        if atx:
            # Use the original bytes for the title, including inline Markdown.
            raw_heading = _body_line(line)[atx.start(2):] if atx[2] is not None else b""
            title = _title(re.sub(rb"(?:^|[ \t]+)#+[ \t]*$", b"", raw_heading))
            yield len(atx[1]), title, start, end
            previous = None
        elif underline and previous:
            prev_start, raw_title = previous
            yield 1 if underline[1].startswith(b"=") else 2, _title(raw_title), prev_start, end
            previous = None
        else:
            previous = (start, _body_line(line)) if candidate.strip() and not candidate.startswith(
                (b">", b"- ", b"* ", b"+ ", b"    ", b"\t")
            ) else None


class _Document:
    def __init__(self, data):
        if not isinstance(data, bytes):
            raise ValueError("Markdown must be supplied as bytes")
        offset = 3 if data.startswith(b"\xef\xbb\xbf") else 0
        self.fences = []
        lines = list(_lines(data[offset:], self.fences, offset))
        self.data = data
        visible = data[:offset]
        visible += b"".join(line if active else _mask(line) for _, line, active in lines)
        self.protected, masked = _protected(data, visible, lines)
        self.sections = []
        stack = []
        for level, title, start, body in _headings(lines, masked, self.protected):
            while stack and stack[-1][0] >= level:
                _, titles, levels, offset = stack.pop()
                self.sections.append(_Section(titles, levels, offset, start))
            parents = stack[-1] if stack else (0, (), (), 0)
            stack.append((level, parents[1] + (title,), parents[2] + (level,), body))
        for _, titles, levels, offset in stack:
            self.sections.append(_Section(titles, levels, offset, len(data)))

    def section(self, selector, root_level=None, optional=False):
        path = _selector(selector)
        matches = [
            section for section in self.sections
            if section.titles[-len(path):] == path
            and (root_level is None or section.levels[-len(path)] == root_level)
        ]
        if len(matches) > 1:
            raise ValueError(f"ambiguous Markdown section {' / '.join(path)}")
        if not matches and not optional:
            raise ValueError(f"missing Markdown section {' / '.join(path)}")
        return matches[0] if matches else None

    def protected_bytes(self, start=0, end=None):
        return self.span_bytes(self.protected, start, end)

    def span_bytes(self, spans, start=0, end=None):
        end = len(self.data) if end is None else end
        return tuple(self.data[left:right] for left, right in spans if left < end and start < right)


def _selector(value):
    if isinstance(value, str):
        value = (value,)
    if (not isinstance(value, tuple) or not value
            or any(not isinstance(title, str) or not title for title in value)):
        raise ValueError("expected a heading name or tuple of heading names")
    return value


def read_section(data, section, *, root_level=None, optional=False):
    """Read body bytes, excluding the heading, or return None for an optional missing section."""
    selected = _Document(data).section(section, root_level, optional)
    return None if selected is None else data[selected.body:selected.end]


def _preserve_fences(old, new, start_a=0, end_a=None, start_b=0, end_b=None):
    remaining = iter(new.span_bytes(new.fences, start_b, end_b))
    for fence in old.span_bytes(old.fences, start_a, end_a):
        if not any(candidate == fence for candidate in remaining):
            raise ValueError("existing fenced code bytes changed")


def _preserve_protected(old, new):
    if old.protected_bytes() != new.protected_bytes():
        raise ValueError("ticket or service line bytes changed")


def preserve_protected(before, after):
    """Reject changes to existing tickets, status lines and fenced code."""
    old, new = _Document(before), _Document(after)
    _preserve_protected(old, new)
    _preserve_fences(old, new)


def changed_sections(before, after, sections, *, root_level=None, optional=False,
                     preserve_fences=True):
    """Return changed heading selectors; reject changes outside permitted bodies."""
    old, new = _Document(before), _Document(after)
    _preserve_protected(old, new)
    pairs = []
    for selector in sections:
        selector = _selector(selector)
        a, b = old.section(selector, root_level, optional), new.section(selector, root_level, optional)
        if a is None and b is None:
            continue
        if a is None or b is None:
            raise ValueError(f"missing Markdown section {' / '.join(selector)}")
        pairs.append((a.body, b.body, a.end, b.end, selector))
    changed, previous_a, previous_b = [], 0, 0
    for start_a, start_b, end_a, end_b, selector in sorted(pairs):
        if (previous_a > start_a or previous_b > start_b
                or before[previous_a:start_a] != after[previous_b:start_b]):
            raise ValueError("bytes outside writable Markdown sections changed")
        if old.protected_bytes(start_a, end_a) != new.protected_bytes(start_b, end_b):
            raise ValueError("ticket or service line moved between Markdown sections")
        if before[start_a:end_a] != after[start_b:end_b]:
            if preserve_fences:
                _preserve_fences(old, new, start_a, end_a, start_b, end_b)
            changed.append(selector)
        previous_a, previous_b = end_a, end_b
    if before[previous_a:] != after[previous_b:]:
        raise ValueError("bytes outside writable Markdown sections changed")
    return tuple(changed)


def _replace_section(data, section, body, root_level, preserve_fences):
    selected = _Document(data).section(section, root_level)
    if not isinstance(body, bytes):
        raise ValueError("replacement body must be supplied as bytes")
    edited = data[:selected.body] + body + data[selected.end:]
    changed_sections(data, edited, (section,), root_level=root_level, preserve_fences=preserve_fences)
    return edited


def replace_section(data, section, body, *, root_level=None):
    """Build an updated note, preserving all protected bytes and other sections."""
    return _replace_section(data, section, body, root_level, preserve_fences=True)


def clear_section(data, section, *, root_level=None):
    """Remove editable section content while keeping its ticket and service lines."""
    document = _Document(data)
    selected = document.section(section, root_level)
    body = b"".join(document.protected_bytes(selected.body, selected.end))
    return _replace_section(data, section, body, root_level, preserve_fences=False)

def _section_content(document, section):
    """Split the editable lines from the protected lines and their positions."""
    lines, protected = [], []
    cursor = section.body
    for start, end in document.protected:
        if start < section.end and section.body < end:
            if start < section.body or section.end < end:
                raise ValueError("ticket or service line crosses the section boundary")
            lines.extend(document.data[cursor:start].splitlines(keepends=True))
            protected.append((len(lines), document.data[start:end]))
            cursor = end
    lines.extend(document.data[cursor:section.end].splitlines(keepends=True))
    return lines, protected


def _edits(before, after):
    return [(start, end, after[left:right]) for tag, start, end, left, right
            in SequenceMatcher(None, before, after, autojunk=False).get_opcodes() if tag != "equal"]


def _merge_edits(base, current, candidate):
    live_edits, new_edits = _edits(base, current), _edits(base, candidate)
    combined = list(live_edits)
    for start, end, lines in new_edits:
        if (start, end, lines) in live_edits:
            continue
        if any((start == end == left == right) or max(start, left) < min(end, right)
               or start == end and left < start < right
               or left == right and start < left < end for left, right, _ in live_edits):
            raise ValueError("the changed part of Specification was also edited in the live note")
        combined.append((start, end, lines))
    result = list(base)
    for start, end, lines in sorted(combined, key=lambda edit: edit[:2], reverse=True):
        result[start:end] = lines
    return result


def _restore_protected(current, replacement, protected):
    result = list(replacement)
    edits = _edits(current, replacement)
    for position, data in reversed(protected):
        offset = 0
        for start, end, lines in edits:
            if position <= start:
                break
            if position < end:
                offset += len(lines) - (position - start)
                break
            offset += len(lines) - (end - start)
        result.insert(position + offset, data)
    return b"".join(result)


def editable_section(data, section):
    """Read the editable section lines, excluding tickets and service lines."""
    document = _Document(data)
    return _section_content(document, document.section(section, root_level=2))[0]


def merge_return_section(before, candidate, live, section, *, partial=False, preserve_fences=True):
    """Return the selected section while keeping the latest ticket and service bytes."""
    documents = tuple(map(_Document, (before, candidate, live)))
    contents = [_section_content(doc, doc.section(section, root_level=2)) for doc in documents]
    (base, _), (new, _), (current, protected) = contents
    returned = _merge_edits(base, current, new) if partial else new
    body = _restore_protected(current, returned, protected)
    return _replace_section(live, section, body, 2, preserve_fences)
