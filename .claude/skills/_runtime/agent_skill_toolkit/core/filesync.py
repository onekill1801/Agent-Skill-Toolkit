"""File-set sync primitives, stdlib only and NO package imports: the same source runs here and — sent over
SSH by integrations.secret_sync — on the peer machine, so both sides hash / pack / unpack identically.

    manifest(root, paths)              {rel: {"sha", "size", "mtime"}} for the files under those rel paths
    pack(root, rels) -> str            base64 tar.gz of those files (mode 0600, registry paths made portable)
    unpack(root, b64, backup_dir)      writes them atomically, backing up every file it replaces first

Registry files (PORTABLE) are hashed and sent with absolute paths under <root>/work rewritten to `work:<rel>`
(project_config / k8s resolve that form on any machine), so a Windows clone_dir never lands on Linux.
Peer entry point: one JSON envelope on stdin {"src": <this source>, "op", "args", "data"} — the command line
stays short (Windows ssh cuts it near 8 KB).   ops: manifest | fetch | apply
"""

import base64
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import time

EXCLUDE_DIRS = {"backups", "__pycache__", ".git"}
EXCLUDE_FILE = re.compile(r"\.(bak|lock|tmp)\b|\.sync-tmp$|~$")
PORTABLE = {"work/projects.json"}
KEEP_BACKUPS = 20


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _full(root: str, rel: str) -> str:
    return os.path.join(root, *rel.split("/"))


def walk(root: str, paths) -> list:
    """Rel paths of the files under `paths` (a file or a directory each); missing ones are skipped."""
    out = []
    for rel in paths:
        full = _full(root, _norm(rel).strip("/"))
        if os.path.isfile(full):
            out.append(_norm(rel).strip("/"))
        elif os.path.isdir(full):
            for dp, dn, fn in os.walk(full):
                dn[:] = sorted(d for d in dn if d not in EXCLUDE_DIRS)
                out += [_norm(os.path.relpath(os.path.join(dp, f), root)) for f in sorted(fn)
                        if not EXCLUDE_FILE.search(f)]
    return out


def portable(rel: str, data: bytes, root: str) -> bytes:
    """`"<root>/work/x/y"` -> `"work:x/y"` in a registry file (either slash style, JSON-escaped backslashes)."""
    if rel not in PORTABLE:
        return data
    work = _norm(os.path.join(os.path.abspath(root), "work"))
    sep = r"(?:/|\\\\)+"
    lead = sep if work.startswith("/") else ""
    pat = re.compile('"' + lead + sep.join(re.escape(p) for p in work.split("/") if p) + sep + r'([^"]*)"',
                     re.I if re.match(r"^[A-Za-z]:", work) else 0)
    text = pat.sub(lambda m: '"work:' + re.sub(sep, "/", m.group(1)) + '"', data.decode("utf-8"))
    return text.encode("utf-8")


def read(root: str, rel: str) -> bytes:
    with open(_full(root, rel), "rb") as f:
        return portable(rel, f.read(), root)


def manifest(root: str, paths) -> dict:
    out = {}
    for rel in walk(root, paths):
        data = read(root, rel)
        out[rel] = {"sha": hashlib.sha256(data).hexdigest(), "size": len(data),
                    "mtime": int(os.path.getmtime(_full(root, rel)))}
    return out


def _tar(entries) -> bytes:
    """entries: [(rel, bytes, mtime)] -> tar.gz bytes, every member 0600."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, data, mtime in entries:
            info = tarfile.TarInfo(rel)
            info.size, info.mode, info.mtime = len(data), 0o600, mtime
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def pack(root: str, rels) -> str:
    entries = [(rel, read(root, rel), int(os.path.getmtime(_full(root, rel)))) for rel in rels]
    return base64.b64encode(_tar(entries)).decode("ascii")


def _safe_rel(name: str) -> str:
    rel = _norm(name)
    parts = rel.split("/")
    if rel.startswith("/") or ":" in parts[0] or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"unsafe path in archive: {name!r}")
    return rel


def _prune(backup_dir: str) -> None:
    olds = sorted(f for f in os.listdir(backup_dir) if f.endswith(".tar.gz"))
    for f in olds[:-KEEP_BACKUPS]:
        os.remove(os.path.join(backup_dir, f))


def unpack(root: str, b64: str, backup_dir: str) -> dict:
    """Write the archived files under root. Regular files only, no absolute / `..` paths. The files it
    replaces are saved first (raw, mode 0600) to <backup_dir>/<stamp>.tar.gz; nothing is ever deleted."""
    files = {}
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(b64)), mode="r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile():
                raise ValueError(f"not a regular file in archive: {m.name!r}")
            files[_safe_rel(m.name)] = tar.extractfile(m).read()
    replaced = [rel for rel in sorted(files) if os.path.isfile(_full(root, rel))]
    backup = None
    if replaced:
        os.makedirs(backup_dir, exist_ok=True)
        backup = os.path.join(backup_dir, f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.tar.gz")
        entries = []
        for rel in replaced:
            with open(_full(root, rel), "rb") as f:
                entries.append((rel, f.read(), int(os.path.getmtime(_full(root, rel)))))
        with open(backup, "wb") as f:
            f.write(_tar(entries))
        os.chmod(backup, 0o600)
        _prune(backup_dir)
    for rel, data in sorted(files.items()):
        full = _full(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".sync-tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, full)
    return {"written": sorted(files), "replaced": replaced, "backup": backup}


def main(op: str, args: dict, data: str = None) -> int:
    """Peer side. Refuses a root that is not a toolkit checkout (agentkit.py missing)."""
    try:
        root = os.path.expanduser(args["root"])
        if not os.path.isfile(os.path.join(root, "agentkit.py")):
            raise ValueError(f"{root} is not a toolkit root (no agentkit.py)")
        if op == "manifest":
            out = {"files": manifest(root, args["paths"])}
        elif op == "fetch":
            out = {"tar": pack(root, args["rels"])}
        elif op == "apply":
            out = unpack(root, data, os.path.join(root, *args["backup_dir"].split("/")))
        else:
            raise ValueError(f"unknown op {op!r}")
    except Exception as e:  # noqa: BLE001 — every failure goes back to the caller as JSON
        print(json.dumps({"error": True, "message": f"{type(e).__name__}: {e}"}))
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    envelope = json.load(sys.stdin)
    sys.exit(main(envelope["op"], envelope["args"], envelope.get("data")))
