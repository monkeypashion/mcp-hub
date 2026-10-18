"""Where a line ENDS — one rule, in one place.

A render that splits text into lines has to agree with the reader about what
a line IS. The hub's renders split on `"\\n"` (after CRLF and CR), but the
readers — `str.splitlines()`, a terminal, a lane's own eyes — also break a
line at U+2028, U+2029, U+0085 (NEL), `\\v`, `\\f` and `\\x1c`-`\\x1e`. Text
carrying one of those therefore reached a reader as a NEW line the render had
not treated as one: unmarked in a task (r8's F26 fence, line by line) and
unindented in a message body (MH-H3), either of which can read as a line the
hub wrote.

So the break set lives here, once, and both renders normalise through it:
`tasks._data_lines` before it marks each line, `server._render_body` before it
indents each line. Two renders that each kept their own idea of a line break
is exactly how they came to disagree.

NOT a JSONL rule. A JSONL file holds one record per `"\\n"`-terminated line
and U+2028 inside a JSON string is DATA, not a frame boundary, so the
transcript readers split on `"\\n"` alone and deliberately do not come
through here (r9's F33).
"""

from __future__ import annotations

import re

#: Every boundary `str.splitlines()` honours, CRLF first so it is ONE break.
LINE_BREAKS: tuple[str, ...] = (
    "\r\n",
    "\n",
    "\r",
    "\x0b",      # vertical tab
    "\x0c",      # form feed
    "\x1c",      # file separator
    "\x1d",      # group separator
    "\x1e",      # record separator
    "\x85",      # NEXT LINE (C1)
    " ",    # LINE SEPARATOR
    " ",    # PARAGRAPH SEPARATOR
)

_BREAK_RE = re.compile("|".join(re.escape(b) for b in LINE_BREAKS))


def to_newlines(text: str) -> str:
    """`text` with every line break `str.splitlines()` honours written `"\\n"`.

    CRLF counts as one break, as it does for `splitlines()`. Nothing else is
    touched: the characters between breaks, the text's own indentation, its
    blank lines and a trailing break all survive, so
    `to_newlines(t).split("\\n")` is `t.splitlines()` with the trailing empty
    line kept.
    """
    return _BREAK_RE.sub("\n", text)
