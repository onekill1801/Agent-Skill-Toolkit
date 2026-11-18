"""Untrusted text helpers (shared by every workflow that shows repo / chat / task text to a model or a
human). Text from outside is DATA: one line, bounded, markup neutralised, delimited.

    clean(text, limit)    one line, control chars collapsed, length bounded
    literal(text)         markdown / HTML markup inert (renders literally anywhere)
    quoted(text, limit)   clean + literal inside «…» (the data delimiter prompts refer to)
"""

import re

_CTRL = re.compile(r"[\x00-\x1f\x7f  ]+")
# only what CREATES markup: emphasis / code / strike / tables, and `[` (no link or image without it)
_MARKUP = re.compile(r"([\\`*_\[\]~|])")


def clean(text, limit: int = 300) -> str:
    """Untrusted text on ONE line: control chars / newlines collapsed, length bounded, so a title
    cannot fake a section, an AC line or an approval instruction."""
    t = _CTRL.sub(" ", str(text or ""))
    t = re.sub(r"\s{2,}", " ", t).strip()
    return (t[:limit] + "…") if len(t) > limit else t


def literal(text: str) -> str:
    """Neutralize markup so task text renders literally whether the destination treats the
    message as Markdown, MarkdownV2 or HTML: markdown punctuation is backslash-escaped, angle
    brackets / ampersands become look-alikes (no tags, no entities)."""
    t = str(text).replace("&", "＆").replace("<", "‹").replace(">", "›")
    return _MARKUP.sub(r"\\\1", t)


def quoted(text, limit: int = 300) -> str:
    """Untrusted task text: one line, bounded, markup neutralized, inside «…»."""
    return "«" + literal(clean(text, limit).replace("«", "‹").replace("»", "›")) + "»"


def one_line(text, limit: int = 300) -> str:
    """Inert single line for rendered artifacts (digests, ADRs, tables)."""
    return literal(" ".join(str(text).split()))[:limit]
