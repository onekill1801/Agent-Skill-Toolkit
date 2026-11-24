#!/usr/bin/env python3
"""Shared helpers for the stack-verify probes (api/db/redis/kafka) and flow_check.

Zero external dependencies — Python stdlib only. Imported by the probe tools;
not meant to be run directly.

Provides:
  - jsonpath_get(data, path)  : JSONPath-lite extraction ($.a.b, $.a[0].c)
  - substitute(obj, vars)     : replace {name} placeholders from a vars dict
  - match_value(actual, expected) : flexible equality (with "*" = any non-null)
  - emit(obj)                 : print one JSON object, return process exit code
"""

import json
import re
import sys

# Cross-platform: force UTF-8 stdout/stderr so JSON with non-ASCII (Vietnamese task
# names, messages) doesn't crash on a Windows cp1252/cp437 console. No-op elsewhere.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

_WILDCARD = "*"  # in an expected value, means "present and not null, any value"


def jsonpath_get(data, path: str):
    """Minimal JSONPath: $.a.b, $.a[0].b, a.b (leading $ optional).

    Returns the value, or raises KeyError/IndexError/TypeError if not found.
    """
    if path in ("$", ""):
        return data
    path = path.lstrip("$").lstrip(".")
    cur = data
    # Split into tokens like  a  b  [0]
    tokens = re.findall(r"[^.\[\]]+|\[\d+\]", path)
    for tok in tokens:
        if tok.startswith("[") and tok.endswith("]"):
            cur = cur[int(tok[1:-1])]
        else:
            cur = cur[tok]
    return cur


def substitute(obj, variables: dict):
    """Recursively replace {name} placeholders in strings using `variables`.

    A string that is exactly "{name}" is replaced by the raw value (keeps type);
    "{name}" embedded in a longer string is replaced by str(value).
    """
    if isinstance(obj, str):
        m = re.fullmatch(r"\{(\w+)\}", obj)
        if m and m.group(1) in variables:
            return variables[m.group(1)]

        def _repl(match):
            key = match.group(1)
            return str(variables.get(key, match.group(0)))

        return re.sub(r"\{(\w+)\}", _repl, obj)
    if isinstance(obj, list):
        return [substitute(x, variables) for x in obj]
    if isinstance(obj, dict):
        return {k: substitute(v, variables) for k, v in obj.items()}
    return obj


def match_value(actual, expected) -> bool:
    """True if actual matches expected. expected == '*' means any non-null value."""
    if expected == _WILDCARD:
        return actual is not None
    # Normalize numeric strings vs numbers (CLI/DB output is text).
    if isinstance(expected, str) and not isinstance(actual, str) and actual is not None:
        return str(actual) == expected
    if isinstance(actual, str) and not isinstance(expected, str) and expected is not None:
        return actual == str(expected)
    return actual == expected


def emit(obj) -> int:
    """Print a JSON object on stdout. Return 1 if it carries an error, else 0."""
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    if isinstance(obj, dict) and obj.get("error"):
        return 1
    return 0
