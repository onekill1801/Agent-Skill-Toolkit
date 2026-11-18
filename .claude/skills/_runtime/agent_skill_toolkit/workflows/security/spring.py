"""Spring endpoint + authorization inventory (offline, regex-based — a heuristic, not a compiler).

    endpoints(files)       -> [{method, path, file, line, handler, method_auth, id_params}]
    url_rules(files)       -> [{patterns, methods, access, file, line}] from HttpSecurity chains, in order
    classify(eps, rules)   -> endpoints with "access" (first matching rule) + AUTHZ-00x findings

Limits (stated in every report): several SecurityFilterChains / securityMatcher scoping are merged in file
order; paths built from constants are not resolved (`path` keeps the constant name); method-level checks
are only seen as @PreAuthorize / @Secured / @RolesAllowed.
"""

import re

_MAPPING = re.compile(r"@(Get|Post|Put|Delete|Patch|Request)Mapping\b\s*(\((?:[^()\"]|\"(?:\\.|[^\"\\])*\"|\([^()]*\))*\))?")
_CLASS = re.compile(r"\b(?:class|interface)\s+(\w+)")
_METHOD_SIG = re.compile(r"(?:public|protected|private)?\s*(?:static\s+)?(?:final\s+)?[\w<>\[\],.?\s]+?\s+(\w+)\s*\(")
_AUTH = re.compile(r"@(?:PreAuthorize|PostAuthorize|Secured|RolesAllowed)\b")
_STRINGS = re.compile(r"\"((?:\\.|[^\"\\])*)\"")
_REQ_METHOD = re.compile(r"RequestMethod\.(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)")
_ID_PARAM = re.compile(r"@(?:PathVariable|RequestParam)\b[^,)]*?\s(\w*(?:[Ii]d|[Ii]ds|Key|Uuid|UUID))\b")
_PATH_VAR = re.compile(r"\{(\w+)(?::[^}]*)?\}")

_MATCHER = re.compile(r"\.(?:antMatchers|requestMatchers|mvcMatchers|regexMatchers)\(\s*((?:[^()]|\([^()]*\))*)\)"
                      r"\s*\.\s*(permitAll|authenticated|denyAll|anonymous|fullyAuthenticated|rememberMe|"
                      r"hasAuthority|hasAnyAuthority|hasRole|hasAnyRole|access|hasIpAddress)\s*\(\s*([^)]*)\)")
_ANY = re.compile(r"\.anyRequest\(\)\s*\.\s*(permitAll|authenticated|denyAll|fullyAuthenticated|hasAuthority|"
                  r"hasAnyAuthority|hasRole|hasAnyRole|access)\s*\(\s*([^)]*)\)")
_HTTP_METHOD = re.compile(r"HttpMethod\.(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)")


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def strip_comments(text: str) -> str:
    """Java/Kotlin source with every comment blanked (same length, newlines kept -> positions and line
    numbers unchanged). String / char literals are left intact ("http://x" is not a comment). A word in a
    Javadoc ("class này …") must not be read as code."""
    out, i, n = list(text), 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            if text.startswith('"""', i):                     # text block
                j = text.find('"""', i + 3)
                i = n if j < 0 else j + 3
                continue
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out[i:j] = " " * (j - i)
            i = j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out[i:j] = [ch if ch == "\n" else " " for ch in text[i:j]]
            i = j
            continue
        i += 1
    return "".join(out)


def _mapping_paths(args: str) -> list:
    """Paths of a mapping annotation: value / path / the positional string(s); a constant -> its name."""
    if not args:
        return [""]
    inner = args[1:-1]
    m = re.search(r"\b(?:value|path)\s*=\s*(\{[^}]*\}|\"(?:\\.|[^\"\\])*\"|[\w.]+)", inner)
    src = m.group(1) if m else None
    if src is None:
        first = inner.split("method", 1)[0] if "=" in inner else inner
        src = first
    found = _STRINGS.findall(src)
    if found:
        return found
    const = re.match(r"\s*\{?\s*([A-Za-z_][\w.]*)", src or "")
    return [f"${{{const.group(1)}}}"] if const and const.group(1) not in ("method", "produces", "consumes") else [""]


def _join(base: str, sub: str) -> str:
    path = "/" + "/".join(x.strip("/") for x in (base, sub) if x and x.strip("/"))
    return path if path != "/" or not (base or sub) else "/"


def endpoints(files: dict) -> list:
    """files: {relpath: text} of Java sources. Only classes annotated @RestController / @Controller."""
    out = []
    for rel, text in files.items():
        if "@RestController" not in text and "@Controller" not in text:
            continue
        text = strip_comments(text)
        if "@RestController" not in text and "@Controller" not in text:
            continue
        cm = _CLASS.search(text)
        if not cm:
            continue
        head = text[:cm.start()]
        base_m = list(_MAPPING.finditer(head))
        bases = _mapping_paths(base_m[-1].group(2)) if base_m and base_m[-1].group(1) == "Request" else [""]
        class_auth = bool(_AUTH.search(head))
        body_start = cm.end()
        for m in _MAPPING.finditer(text, body_start):
            sig = _METHOD_SIG.search(text, m.end())
            if not sig:
                continue
            # the annotations of this method: from the previous statement end to its name
            block_start = max(text.rfind(";", body_start, m.start()), text.rfind("}", body_start, m.start()),
                              text.rfind("{", body_start, m.start()))
            block = text[block_start + 1:sig.end()]
            depth, i = 0, sig.end() - 1
            while i < len(text):
                depth += {"(": 1, ")": -1}.get(text[i], 0)
                if depth == 0:
                    break
                i += 1
            params = text[sig.end() - 1:i + 1]
            kind = m.group(1).upper()
            methods = [kind] if kind != "REQUEST" else (_REQ_METHOD.findall(m.group(2) or "") or ["ANY"])
            ids = sorted(set(_ID_PARAM.findall(params)))
            for base in bases:
                for sub in _mapping_paths(m.group(2)):
                    path = _join(base, sub)
                    ids_here = sorted(set(ids) | {v for v in _PATH_VAR.findall(path)
                                                  if re.search(r"(?i)id$|ids$|key$|uuid$", v)})
                    for meth in methods:
                        out.append({"method": meth, "path": path, "file": rel, "line": _line_of(text, m.start()),
                                    "handler": sig.group(1), "method_auth": class_auth or bool(_AUTH.search(block)),
                                    "id_params": ids_here})
    return out


def url_rules(files: dict) -> list:
    """Ordered (patterns, methods, access) rules of every HttpSecurity chain found."""
    out = []
    for rel, text in sorted(files.items()):
        if "HttpSecurity" not in text:
            continue
        text = strip_comments(text)                        # a commented-out rule does not protect anything
        events = [(m.start(), "m", m) for m in _MATCHER.finditer(text)] + \
                 [(m.start(), "a", m) for m in _ANY.finditer(text)]
        for pos, kind, m in sorted(events, key=lambda e: e[0]):
            if kind == "m":
                args, access = m.group(1), m.group(2)
                pats = _STRINGS.findall(args)
                if not pats:
                    continue
                out.append({"patterns": pats, "methods": _HTTP_METHOD.findall(args), "access": access,
                            "arg": m.group(3).strip(), "file": rel, "line": _line_of(text, pos)})
            else:
                out.append({"patterns": ["/**"], "methods": [], "access": m.group(1), "arg": m.group(2).strip(),
                            "file": rel, "line": _line_of(text, pos), "any_request": True})
    return out


def _ant(pattern: str):
    p = re.escape(pattern).replace(r"\*\*", "\0").replace(r"\*", "[^/]*").replace("\0", ".*")
    p = re.sub(r"\\\{\w+(?::[^}]*)?\\\}", "[^/]+", p)
    if p.endswith("/.*"):
        p = p[:-3] + "(?:/.*)?"
    return re.compile("^" + p + "$")


_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_WORST = {"AUTHZ-002": 0, "AUTHZ-004": 1, "AUTHZ-001": 2, "AUTHZ-003": 3, None: 9}


def _first_rule(compiled, method, sample):
    for r, pats in compiled:
        if r["methods"] and method not in r["methods"]:
            continue
        if any(p.match(sample) for p in pats):
            return r
    return None


def _outcome(ep, method, hit):
    if hit is None:
        return "AUTHZ-002", "không rule HttpSecurity nào khớp"
    if hit["access"] in ("permitAll", "anonymous"):
        write = method in ("POST", "PUT", "PATCH", "DELETE")
        return ("AUTHZ-004" if write and not ep["method_auth"] else "AUTHZ-001"), \
            f"permitAll tại {hit['file']}:{hit['line']}"
    if hit["access"] in ("authenticated", "fullyAuthenticated", "rememberMe") and ep["id_params"] \
            and not ep["method_auth"]:
        return "AUTHZ-003", "tham số id: " + ", ".join(ep["id_params"])
    return None, ""


def classify(eps: list, rules: list) -> tuple:
    """(endpoints with their access, findings). An endpoint path that is a constant is not matched.
    A mapping without a method (ANY) is judged once per HTTP method and keeps the WORST outcome: a rule
    scoped to GET must not make its POST look protected (Codex security R1 #6)."""
    compiled = [(r, [_ant(p) for p in r["patterns"]]) for r in rules]
    findings = []
    for ep in eps:
        sample = _PATH_VAR.sub("x", ep["path"])
        if sample.startswith("/${"):
            ep["access"], ep["rule_at"] = None, None
            continue                                       # path from a constant: not resolvable offline
        methods = _METHODS if ep["method"] == "ANY" else (ep["method"],)
        per = []
        for m in methods:
            hit = _first_rule(compiled, m, sample)
            rid, detail = _outcome(ep, m, hit)
            per.append((_WORST[rid], m, hit, rid, detail))
        _, m, hit, rid, detail = min(per, key=lambda x: x[0])
        label = lambda h: (h["access"] + (f"({h['arg']})" if h.get("arg") else "")) if h else None
        accesses = {label(h) for _, _, h, _, _ in per}
        ep["access"] = label(hit) if len(accesses) == 1 else \
            "; ".join(f"{mm}={label(h)}" for _, mm, h, _, _ in per)
        ep["rule_at"] = f"{hit['file']}:{hit['line']}" if hit else None
        if rid:
            if ep["method"] == "ANY":
                detail += f" (method {m})"
            findings.append({"rule": rid, "file": ep["file"], "line": ep["line"],
                             "text": f"{ep['method']} {ep['path']} -> {ep['handler']}()", "detail": detail})
    return eps, findings
