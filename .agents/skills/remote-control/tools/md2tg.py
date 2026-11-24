#!/usr/bin/env python3
"""Convert the agent's Markdown into Telegram-flavoured HTML.

Telegram's HTML parser supports only a small tag set (<b> <i> <u> <s> <a>
<code> <pre> <blockquote>) and has NO concept of headings, tables, or lists.
Raw Markdown therefore shows up as noise (`##`, `**`, `|---|`). This module
rewrites the common Markdown constructs into something Telegram renders well:

  ## / ### heading      -> <b>heading</b>
  **bold**              -> <b>bold</b>
  `code`                -> <code>code</code>
  ```fenced```          -> <pre>fenced</pre>
  [text](url)           -> <a href="url">text</a>
  - bullet / * bullet   -> • bullet
  | a | b |  table       -> • <b>a</b>: b   (one readable line per data row)
  ---  (horizontal rule) -> a thin separator

Inline code / fenced blocks are protected before escaping so their contents are
never mangled (snake_case, "name", etc. stay intact). Italics via single
`*`/`_` are intentionally NOT converted — they corrupt snake_case identifiers
and rarely matter; the markers are left as-is.
"""

import html
import re

_CODE_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}.*$")


def _row_cells(line: str):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _inline(text: str) -> str:
    """Escape + apply inline conversions (code, links, bold) to a fragment."""
    stash = []

    def _protect(m):
        stash.append(html.escape(m.group(1)))
        return f"\x00{len(stash) - 1}\x00"

    text = _INLINE_CODE.sub(_protect, text)
    text = html.escape(text)
    text = _LINK.sub(
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    # restore protected inline code as <code>
    text = re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{stash[int(m.group(1))]}</code>", text)
    return text


def to_html(md: str) -> str:
    """Convert a full Markdown message to Telegram HTML."""
    if not md:
        return md
    md = md.replace("\r\n", "\n").replace("\r", "\n")

    # 1) pull fenced code blocks out so line processing never touches them
    blocks = []

    def _stash_fence(m):
        blocks.append(html.escape(m.group(1).rstrip("\n")))
        return f"\x01{len(blocks) - 1}\x01"

    md = _CODE_FENCE.sub(_stash_fence, md)

    lines = md.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]

        # table: a header row followed by a |---| separator
        if "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]) \
                and "|" in lines[i + 1]:
            i += 2  # skip header + separator (the bold key carries the meaning)
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                cells = [c for c in _row_cells(lines[i]) if c != ""]
                if cells:
                    head = _inline(cells[0])
                    rest = " — ".join(_inline(c) for c in cells[1:])
                    out.append(f"• <b>{head}</b>" + (f": {rest}" if rest else ""))
                i += 1
            continue

        hm = _HEADING.match(line)
        if hm:
            out.append(f"<b>{_inline(hm.group(1).replace('**', ''))}</b>")
            i += 1
            continue

        if _HR.match(line):
            out.append("——————————")
            i += 1
            continue

        bm = _BULLET.match(line)
        if bm:
            indent = "  " if bm.group(1) else ""
            out.append(f"{indent}• {_inline(bm.group(2))}")
            i += 1
            continue

        out.append(_inline(line))
        i += 1

    text = "\n".join(out)
    # 2) restore fenced blocks as <pre>
    text = re.sub(r"\x01(\d+)\x01", lambda m: f"<pre>{blocks[int(m.group(1))]}</pre>", text)
    # collapse 3+ blank lines that headings/tables may leave behind
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


if __name__ == "__main__":
    import sys
    print(to_html(sys.stdin.read()))
