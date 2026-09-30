# SPDX-FileCopyrightText: 2025 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Centralised YAML I/O — round-trip mode everywhere.

Re-export of the shared [`terok_util.yaml`][terok_util.yaml] facade under
terok's own ``terok.lib.util.yaml`` import path.

The facade hands out a fresh ``ruamel.yaml`` instance per call: a shared
module-level instance carries composer/parser/scanner state and races
under concurrent ``load`` / ``dump`` from Textual worker threads (symptom
``'MappingEndEvent' object has no attribute 'anchor'``).

On top of it sit the few helpers an *in-place* edit of a file the operator
wrote needs — appending one entry to ``project.yml`` without the rest of
the file noticing.  Round-trip mode alone keeps comments and flow style, but
a plain ``dump`` still re-indents every block sequence to ruamel's default,
re-wraps lines past 80 columns, and lets an appended entry land *below*
whatever comment lines trailed the old last one (ruamel hangs them on that
entry).  [`dump_like`][terok.lib.util.yaml.dump_like] keeps the source's
indentation and line lengths;
[`append_entry`][terok.lib.util.yaml.append_entry] and
[`put_key`][terok.lib.util.yaml.put_key] put a new entry directly after
the old last one, with the trailing comment lines moved below it, and
[`set_entries`][terok.lib.util.yaml.set_entries] refills a list without
losing its style or the comments around it.

Even a layout-aware dump re-emits the whole file in one layout, and spells
every scalar its own way (``true`` for ``True``, nothing for ``~``).
[`edit_in_place`][terok.lib.util.yaml.edit_in_place] therefore writes no
dump at all: it carries just the lines an edit changed into the operator's
text, so a write changes what the edit changed and nothing else;
[`append_section`][terok.lib.util.yaml.append_section] does the same for a
section the file does not have yet.
"""

from __future__ import annotations

import copy
import re
from bisect import bisect_left
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from io import StringIO
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import FoldedScalarString, LiteralScalarString
from terok_util.yaml import YAMLError, dump, load  # noqa: F401 — re-exported

__all__ = [
    "load",
    "dump",
    "YAMLError",
    "dump_like",
    "edit_in_place",
    "append_section",
    "append_entry",
    "put_key",
    "set_entries",
]

_NO_WRAP_WIDTH = 1 << 16
"""Line width that never folds an existing long line on re-emission."""

_DOC_START_PREFIX = re.compile(r"(?:[ \t]*(?:#[^\n]*)?\n)*---[ \t]*(?:#[^\n]*)?(?:\n|$)")
"""Comment/blank lines up to an explicit ``---`` — ruamel drops what precedes it."""

_COMMENT_START = re.compile(r"\s#")
_BLOCK_SCALAR_HEADER = re.compile(r"(?:^|:\s+)[|>][-+0-9]*$")

_DOCUMENT_HEADER_LINE = re.compile(r"\s*(?:#.*|%.*|---(?:\s.*)?)?")
"""A line before a document's content: blank, a comment, a ``%`` directive, ``---``."""

_DOCUMENT_END_LINE = re.compile(r"\.\.\.(?:\s.*)?")
"""The explicit end-of-document marker, ``...``."""


@dataclass(frozen=True, slots=True)
class _Layout:
    """Block indentation in ruamel's terms, relative to the parent key's column.

    ``mapping`` — a nested key's indent; ``sequence`` — a block-sequence
    item's content indent; ``offset`` — where its dash sits.  The defaults
    are the layout terok's own docs and examples use (``allow:`` then
    ``    - host``).
    """

    mapping: int = 2
    sequence: int = 4
    offset: int = 2


_DEFAULT_LAYOUT = _Layout()


def _guess_layout(source: str, fallback: _Layout | None = None) -> _Layout:
    """Read the block indentation *source* already uses.

    The first nested mapping key sets ``mapping``; the first block-sequence
    item sets ``sequence`` and ``offset``.  Comment lines and block-scalar
    bodies are skipped — a snippet's ``RUN …`` lines are not structure.
    What *source* does not show comes from *fallback* when one is given — a
    section with no block sequence of its own follows the file's — else a
    file with no block sequence gets dashes indented like nested keys;
    anything inconsistent falls back to *fallback*, or the defaults.
    """
    mapping = sequence = offset = None
    for col, body, parent in _structure_lines(source):
        dash = _is_sequence_item(body)
        if parent is not None and dash and col >= parent and offset is None:
            rest = body[1:]
            offset = col - parent
            sequence = offset + 1 + len(rest) - len(rest.lstrip(" "))
        elif parent is not None and not dash and col > parent and mapping is None:
            mapping = col - parent
        if mapping is not None and offset is not None:
            break
    return _completed_layout(mapping, sequence, offset, fallback)


def _structure_lines(source: str) -> Iterator[tuple[int, str, int | None]]:
    """The lines of *source* that are structure, as ``(column, text, parent)``.

    ``text`` is the line stripped; ``parent`` is the column of the key that
    just opened a block — the key the line may nest under — else ``None``.
    Blank and comment lines are no structure, nor is a block scalar's body:
    the lines indented past the one that opened it.
    """
    parent: int | None = None  # column of the key that just opened a block
    scalar: int | None = None  # column of the line that opened a block scalar
    for raw in source.splitlines():
        body = raw.strip()
        if not body or body.startswith("#"):
            continue
        col = len(raw) - len(raw.lstrip(" "))
        if scalar is not None and col > scalar:
            continue
        yield col, body, parent
        scalar, parent = _opened_by(col, body)


def _opened_by(col: int, body: str) -> tuple[int | None, int | None]:
    """What the structure line *body*, at column *col*, opens: ``(scalar, parent)``.

    ``scalar`` is *col* when the line opens a block scalar; ``parent`` is the
    column of its key — past the dash, on a sequence item — when that key
    opens a block.  Each is ``None`` otherwise.
    """
    text, key_col = _without_comment(body), col
    if _is_sequence_item(body):
        inner = text[1:].lstrip(" ")
        key_col, text = col + len(text) - len(inner), inner
    if _BLOCK_SCALAR_HEADER.search(text):
        return col, None
    return None, (key_col if text.endswith(":") and text != ":" else None)


def _without_comment(line: str) -> str:
    """*line* without its end-of-line comment, or the whitespace that sets it off.

    Cut at the first whitespace right before a ``#``, then strip the rest of
    that run: linear, where a single pattern for the run and the comment
    backtracks quadratically over a long run that no ``#`` follows.
    """
    match = _COMMENT_START.search(line)
    return line[: match.start()].rstrip() if match else line


def _is_sequence_item(body: str) -> bool:
    """Whether the stripped line *body* is a block-sequence item: ``-``, alone or before a space."""
    return body == "-" or body.startswith("- ")


def _completed_layout(
    mapping: int | None, sequence: int | None, offset: int | None, fallback: _Layout | None
) -> _Layout:
    """The indents a source showed, ``None`` for each it did not, completed into a layout.

    A missing *mapping* comes from *fallback*, else the defaults; a missing
    *sequence* and *offset* from *fallback*, else dashes go where nested keys
    do.  A layout with its dash left of the key, or no room for ``- ``, gives
    way to *fallback*, or the defaults.
    """
    mapping = mapping or (fallback or _DEFAULT_LAYOUT).mapping
    if offset is None or sequence is None:
        if fallback is not None:
            offset, sequence = fallback.offset, fallback.sequence
        else:
            offset, sequence = mapping, mapping + 2
    if offset < 0 or sequence < offset + 2:
        return fallback or _DEFAULT_LAYOUT
    return _Layout(mapping, sequence, offset)


def dump_like(data: Any, source: str) -> str:
    """Dump *data* round-trip, laid out the way *source* — the text it was loaded from — is.

    Keeps what a plain [`dump`][terok_util.yaml.dump] would rewrite:
    block indentation (guessed from *source*), long lines (never re-wrapped),
    and a leading ``---`` with any comments above it.  Comments, quotes,
    key order and flow style survive as round-trip mode always keeps them.
    """
    return _dump_laid_out(data, source, _guess_layout(source))


def edit_in_place(source: str, edited: Any, *, section: str | None = None) -> str:
    """*source* with the one change that turns its data into *edited*, and nothing else.

    *edited* is the document loaded from *source* and then changed in place
    by the helpers here.  The unchanged and the changed document are rendered
    alike, and only the lines that differ between those two renderings — the
    change itself — are carried into *source*, each placed by the rendered
    lines around it, which must appear in *source* verbatim.  Every other
    line stays byte for byte, including what a dump would spell its own way:
    ``True`` and ``~``, an indent unlike the rest of the file, a ``%YAML``
    directive or a closing ``...``, CRLF line ends.

    *section* names the top-level key the change sits under; the rendering
    takes that section's own indentation, so a list indented unlike the rest
    of the file keeps its layout when an entry joins it.

    Raises:
        ValueError: The change cannot be placed that way — the lines around
            it do not appear in *source* as the emitter writes them — or the
            result would not load to the same data as *edited*, or *source*
            holds no mapping to edit.  Nothing is decided for the caller.
    """
    original = load(source)
    if not isinstance(original, CommentedMap):
        raise ValueError("the file holds no mapping to edit in place")
    layout = _guess_layout(source)
    if section is not None and (lines := _section_text(source, original, section)):
        layout = _guess_layout(lines, fallback=layout)
    before = _dump_laid_out(original, source, layout)
    after = _dump_laid_out(edited, source, layout)
    spliced = _splice(source, before, after)
    if spliced is None:
        raise ValueError(
            "the lines around the change are not in the file the way they would be written"
        )
    if not _loads_as(spliced, _plain(load(after))):
        raise ValueError("carrying the change into the file would change what it means")
    return spliced


def append_section(source: str, key: str, value: Any) -> str:
    """*source* with a new top-level *key* holding *value*, and every other byte as it was.

    The section is rendered on its own, in the file's indentation, and goes
    after the file's last section — that section's own indented comment
    lines included, so a commented-out body stays with its key — and above
    the comment block that closes the file, if any, with a blank line before
    it.  The file's line ends are used for the new lines.

    Raises:
        ValueError: *source* holds no mapping, already has *key*, or the
            result would not load as *source*'s data with the section added.
    """
    original = load(source)
    if not isinstance(original, CommentedMap):
        raise ValueError(f"the file holds no mapping to add a {key!r} section to")
    if key in original:
        raise ValueError(f"the file has a {key!r} section already")
    lines = source.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in source else "\n"
    at = len(lines)
    while at and _closes_the_file(lines[at - 1]):
        at -= 1
    section = _dump_laid_out(CommentedMap([(key, value)]), "", _guess_layout(source))
    added = [newline, *(text + newline for text in section.splitlines())]
    result = _with_lines_replaced(source, [(at, at, added)], newline)
    if not _loads_as(result, [*_plain(original), (key, _plain(value))]):
        raise ValueError(f"adding the {key!r} section would change what the file means")
    return result


def _loads_as(text: str, meaning: Any) -> bool:
    """Whether *text* loads to *meaning* — a document as ``_plain`` renders it.

    A text that no longer loads at all means something else too: a line the
    operator spelled their own way — ``key: null`` — cannot take a block
    under it, and the writer refuses rather than leave a file that breaks.
    """
    try:
        return _plain(load(text)) == meaning
    except YAMLError:
        return False


def _closes_the_file(line: str) -> bool:
    """Whether *line* belongs to what closes a file: blank, ``...``, or an unindented comment."""
    text = line.rstrip("\r\n")
    return not text.strip() or text.startswith("#") or bool(_DOCUMENT_END_LINE.fullmatch(text))


def _with_lines_replaced(source: str, edits: list[tuple[int, int, list[str]]], newline: str) -> str:
    """*source* with its lines ``start``..``end`` replaced by ``lines``, for each of *edits*.

    *edits* are ``(start, end, lines)``, in order and not overlapping.  A last
    line without a line end gets *newline* while others may follow it, and
    when *source* has no final line end, a *newline* ending the result is
    dropped.
    """
    lines = source.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += newline  # a line may follow it now
    for start, end, text in reversed(edits):
        lines[start:end] = text
    result = "".join(lines)
    if not source.endswith(("\n", "\r")) and result.endswith(newline):
        result = result[: -len(newline)]
    return result


def _dump_laid_out(data: Any, source: str, layout: _Layout) -> str:
    """Dump *data* in *layout*, keeping *source*'s long lines and its leading ``---``."""
    emitter = YAML(typ="rt")
    emitter.preserve_quotes = True
    emitter.width = _NO_WRAP_WIDTH
    emitter.indent(mapping=layout.mapping, sequence=layout.sequence, offset=layout.offset)
    prefix = match.group(0) if (match := _DOC_START_PREFIX.match(source)) else ""
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    buf = StringIO()
    emitter.dump(data, buf)
    return prefix + buf.getvalue()


def _section_text(source: str, document: CommentedMap, key: str) -> str:
    """The lines of *source* that hold top-level *key* and its value; empty when it has none."""
    if key not in document:
        return ""
    keys = list(document)
    start = document.lc.key(key)[0]
    after = keys.index(key) + 1
    end = document.lc.key(keys[after])[0] if after < len(keys) else None
    return "\n".join(source.splitlines()[start:end])


def _splice(source: str, before: str, after: str) -> str | None:
    """Carry the lines that differ from *before* to *after* into *source*; ``None`` when unplaceable.

    *before* and *after* are two renderings of the same document, unchanged
    and changed.  Each differing stretch is placed between the nearest
    rendered lines around it that *source* has verbatim — the document's
    first and last lines of content stand in at the edges.  Between those
    anchors, *source* may spell lines its own way (``[ python ]``, ``True``,
    another indent).  When it has as many lines there as the rendering, the
    lines correspond one to one and the stretch is placed by position: its
    own lines are replaced and the neighbours left as written.  When it does
    not, the stretch must fill the gap between its anchors — or, for an
    insertion, sit right against one of them — or it cannot be placed.
    """
    plain = [line.rstrip("\r\n") for line in source.splitlines(keepends=True)]
    newline = "\r\n" if "\r\n" in source else "\n"
    old, new = before.splitlines(), after.splitlines()
    found = _verbatim_lines(old, plain)
    anchors = sorted(found)
    head, tail = _content_span(plain)
    edits: list[tuple[int, int, list[str]]] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        p, q = _anchors_around(anchors, i1, i2, len(old))
        low, high = _source_between(found, p, q, len(old), head, tail)
        if (span := _place_stretch(tag, i1, i2, p, q, low, high)) is None:
            return None
        edits.append((*span, [text + newline for text in new[j1:j2]]))
    edits.sort()
    if any(edits[k][1] > edits[k + 1][0] for k in range(len(edits) - 1)):
        return None
    return _with_lines_replaced(source, edits, newline)


def _verbatim_lines(rendered: list[str], plain: list[str]) -> dict[int, int]:
    """Where *plain* has the *rendered* lines verbatim, matched in order: index to index."""
    found: dict[int, int] = {}
    for block in SequenceMatcher(None, rendered, plain, autojunk=False).get_matching_blocks():
        for k in range(block.size):
            found[block.a + k] = block.b + k
    return found


def _content_span(plain: list[str]) -> tuple[int, int]:
    """Where the document's content lies in *plain*: ``(head, tail)``, ``tail`` exclusive.

    Before it come blank and comment lines, ``%`` directives and ``---``;
    after it, blank lines and ``...``.
    """
    head = next(
        (i for i, text in enumerate(plain) if not _DOCUMENT_HEADER_LINE.fullmatch(text)),
        len(plain),
    )
    tail = len(plain)
    while tail > head and (
        not plain[tail - 1].strip() or _DOCUMENT_END_LINE.fullmatch(plain[tail - 1])
    ):
        tail -= 1
    return head, tail


def _anchors_around(anchors: list[int], i1: int, i2: int, size: int) -> tuple[int, int]:
    """The anchors nearest the rendered stretch *i1*..*i2*: the last above it, the first below.

    ``-1`` stands in when no anchor is above it, and *size* — one past the
    last rendered line — when none is below.
    """
    above = bisect_left(anchors, i1)
    below = bisect_left(anchors, i2)
    p = anchors[above - 1] if above else -1
    q = anchors[below] if below < len(anchors) else size
    return p, q


def _source_between(
    found: dict[int, int], p: int, q: int, size: int, head: int, tail: int
) -> tuple[int, int]:
    """The source lines between the anchors *p* and *q*: ``(low, high)``, ``high`` exclusive.

    At the document's edges — *p* ``-1``, *q* *size* — its first and last
    lines of content stand in: *head* and *tail*.
    """
    low = found[p] + 1 if p >= 0 else None
    high = found[q] if q < size else None
    if low is None:  # the stretch opens the document: it starts at the first content line
        low = head if high is None else min(head, high)
    if high is None:  # it closes the document: never before the last anchor
        high = max(tail, low)
    return low, high


def _place_stretch(
    tag: str, i1: int, i2: int, p: int, q: int, low: int, high: int
) -> tuple[int, int] | None:
    """Where the rendered stretch *i1*..*i2* goes in the source: ``(start, end)``, or ``None``.

    *low*..*high* are the source lines between its anchors *p* and *q*.
    """
    if high < low:
        return None
    if high - low == q - p - 1:  # same lines, maybe spelled their own way: place by position
        return low + (i1 - p - 1), low + (i2 - p - 1)
    if i1 == p + 1 and i2 == q:  # the stretch is all there is between its anchors
        return low, high
    if tag == "insert" and i1 == p + 1:
        return low, low
    if tag == "insert" and i2 == q:
        return high, high
    return None


def _plain(node: Any) -> Any:
    """*node* as plain values, mappings as ordered pairs — what a loaded document *means*."""
    if isinstance(node, Mapping):
        return [(key, _plain(value)) for key, value in node.items()]
    if isinstance(node, list | tuple):
        return [_plain(item) for item in node]
    return node


def append_entry(seq: list[Any], value: Any) -> None:
    """Append *value* to the round-trip sequence *seq*, right after its last entry.

    Comment and blank lines that trailed the old last entry move below the
    new one, so an entry never lands under a section's commented-out
    example.  An empty sequence can only have been written ``[]``, so it
    stays in flow style: ``sets: []`` becomes ``sets: [python]``.
    """
    if not seq and isinstance(seq, CommentedSeq):
        seq.fa.set_flow_style()
    tail = _cut_trailing_lines(seq)
    seq.append(_commented(value))
    if tail is not None:
        _attach_trailing_lines(seq, tail)


def put_key(
    mapping: dict[str, Any], key: str, value: Any, *, blank_line_before: bool = False
) -> None:
    """Set *key* to *value* in the round-trip *mapping*.

    An existing key keeps its place and the comment on its line.  A new key
    becomes the last one, right after the old last key: the comment lines
    that trailed it move below the new one, as in
    [`append_entry`][terok.lib.util.yaml.append_entry], and
    *blank_line_before* separates it the way hand-written files separate
    top-level sections.
    """
    if key in mapping:
        mapping[key] = _commented(value)
        return
    tail = _cut_trailing_lines(mapping)
    mapping[key] = _commented(value)
    if tail is not None:
        _attach_trailing_lines(mapping, tail)
    if blank_line_before and isinstance(mapping, CommentedMap) and len(mapping) > 1:
        mapping.yaml_set_comment_before_after_key(key, before="\n")


def set_entries(mapping: dict[str, Any], key: str, values: list[Any]) -> None:
    """Make ``mapping[key]`` a sequence holding exactly *values*.

    An existing sequence is refilled in place: its flow or block style, the
    comment on its key line and the comment lines trailing it survive
    (comments on its individual entries go with them).  Anything else
    under *key* — nothing, or a null — becomes a new block sequence, as
    [`put_key`][terok.lib.util.yaml.put_key] would add it.
    """
    current = mapping.get(key)
    if not isinstance(current, CommentedSeq):
        put_key(mapping, key, list(values))
        return
    flow = not current or bool(current.fa.flow_style())
    tail = _cut_trailing_lines(current)
    del current[:]
    current.ca.items.clear()
    current.extend(_commented(value) for value in values)
    if flow or not current:
        current.fa.set_flow_style()
    if tail is None:
        return
    if current:
        _attach_trailing_lines(current, tail)
    elif isinstance(mapping, CommentedMap):  # an emptied block list is written ``[]``
        mapping.ca.items.setdefault(key, [None, None, None, None])[2] = tail


def _commented(value: Any) -> Any:
    """Turn plain dicts and lists into round-trip nodes, which can carry comments."""
    if isinstance(value, dict) and not isinstance(value, CommentedMap):
        node = CommentedMap()
        for key, item in value.items():
            node[key] = _commented(item)
        return node
    if isinstance(value, list | tuple) and not isinstance(value, CommentedSeq):
        return CommentedSeq(_commented(item) for item in value)
    return value


def _last_slot(node: Any) -> tuple[list[Any], int, Any] | None:
    """Find the comment slot that ends *node*'s block: ``(slot list, index, value)``.

    ruamel hangs a comment token after the deepest last value of a block
    collection — the EOL comment of its line, plus every comment and blank
    line up to the next node.  ``None`` when nothing trails it.
    """
    if isinstance(node, CommentedMap) and node:
        key = next(reversed(node))
        if (deeper := _last_slot(node[key])) is not None:
            return deeper
        entry = node.ca.items.get(key)
        if entry and len(entry) > 2 and entry[2] is not None:
            return entry, 2, node[key]
    elif isinstance(node, CommentedSeq) and node:
        index = len(node) - 1
        if (deeper := _last_slot(node[index])) is not None:
            return deeper
        entry = node.ca.items.get(index)
        if entry and entry[0] is not None:
            return entry, 0, node[index]
    return None


def _cut_trailing_lines(node: Any) -> Any:
    """Detach the full lines trailing *node*'s last entry; return them as a token.

    The entry keeps its own end-of-line comment.  A block scalar's token
    starts on a fresh line already (the scalar ends with its newline), so
    all of it trails.  ``None`` when there is nothing to move.
    """
    slot = _last_slot(node)
    if slot is None:
        return None
    entry, index, value = slot
    token = entry[index]
    if isinstance(value, LiteralScalarString | FoldedScalarString):
        head, tail = "", token.value
    else:
        head, _, tail = token.value.partition("\n")
    if not tail:
        return None
    if head:
        token.value = head + "\n"
    else:
        entry[index] = None
    moved = copy.copy(token)  # keeps the start mark the emitter positions comments by
    moved.value = "\n" + tail
    return moved


def _attach_trailing_lines(node: Any, token: Any) -> None:
    """Hang *token* after the deepest last entry of *node*'s block."""
    if isinstance(node, CommentedMap) and node:
        key = next(reversed(node))
        if _is_block_collection(node[key]):
            _attach_trailing_lines(node[key], token)
        else:
            node.ca.items.setdefault(key, [None, None, None, None])[2] = token
    elif isinstance(node, CommentedSeq) and node:
        index = len(node) - 1
        if _is_block_collection(node[index]):
            _attach_trailing_lines(node[index], token)
        else:
            node.ca.items.setdefault(index, [None, None, None, None])[0] = token


def _is_block_collection(value: Any) -> bool:
    """Whether *value* is a non-empty block (not flow) mapping or sequence."""
    return (
        isinstance(value, CommentedMap | CommentedSeq) and bool(value) and not value.fa.flow_style()
    )
