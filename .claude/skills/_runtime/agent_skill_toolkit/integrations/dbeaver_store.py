"""DBeaver workspace files: read/write a project's data-sources.json and its credential store.

credentials-config.json is AES-128-CBC with DBeaver's built-in key (the same for every install, so the
file is only obfuscated — treat it as a plaintext secret): 16-byte IV + ciphertext of a JSON object
{connection id: {"#connection": {"user": ..., "password": ...}}}. The cipher runs through the `openssl`
CLI (stdlib has no AES); the payload goes over stdin, never argv. Nothing here prints a credential.

    from agent_skill_toolkit.integrations import dbeaver_store as ds
    proj = ds.read_project("<workspace>/General")       # {"name", "data", "creds"}
    ds.write_project("<workspace>", "Kyta", data, creds)  # files only: DBeaver lists a NEW project only after
                                                           # File > Import > Existing Projects into Workspace (once)
"""

import json
import os
import platform
import subprocess

from ..core import env

DBEAVER_KEY = "babb4a9f774ab853c96c2d653dfe544a"   # DBeaver's public default key (not a secret of ours)
PROJECT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<projectDescription>
\t<name>{name}</name>
\t<comment>{comment}</comment>
\t<projects>
\t</projects>
\t<buildSpec>
\t</buildSpec>
\t<natures>
\t\t<nature>org.jkiss.dbeaver.DBeaverNature</nature>
\t</natures>
</projectDescription>
"""


def default_workspace() -> str:
    home = os.path.expanduser("~")
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    elif system == "Darwin":
        base = os.path.join(home, "Library", "DBeaverData")
        return os.path.join(base, "workspace6")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(base, "DBeaverData", "workspace6")


def projects(workspace: str) -> list:
    """Names of the projects (folders with .dbeaver/data-sources.json) in a workspace."""
    try:
        names = sorted(os.listdir(workspace))
    except OSError:
        return []
    return [n for n in names if os.path.isfile(os.path.join(workspace, n, ".dbeaver", "data-sources.json"))]


def _openssl(args: list, data: bytes) -> bytes:
    exe = env.get("DBEAVER_OPENSSL_BIN") or "openssl"
    p = subprocess.run([exe, "enc", "-aes-128-cbc", "-K", DBEAVER_KEY, *args], input=data, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("openssl failed: " + p.stderr.decode(errors="replace")[:200])
    return p.stdout


def decrypt_credentials(raw: bytes) -> dict:
    if not raw:
        return {}
    plain = _openssl(["-d", "-iv", raw[:16].hex()], raw[16:])
    return json.loads(plain.decode("utf-8"))


def encrypt_credentials(creds: dict) -> bytes:
    iv = os.urandom(16)
    return iv + _openssl(["-iv", iv.hex()], json.dumps(creds, ensure_ascii=False).encode("utf-8"))


def read_project(path: str) -> dict:
    """{"name", "data": data-sources.json, "creds": decrypted credential store ({} if none)}."""
    with open(os.path.join(path, ".dbeaver", "data-sources.json"), encoding="utf-8") as f:
        data = json.load(f)
    cpath = os.path.join(path, ".dbeaver", "credentials-config.json")
    creds = {}
    if os.path.isfile(cpath):
        with open(cpath, "rb") as f:
            creds = decrypt_credentials(f.read())
    return {"name": os.path.basename(os.path.normpath(path)), "data": data, "creds": creds}


def write_project(workspace: str, name: str, data: dict, creds: dict, comment: str = "") -> str:
    """Write <workspace>/<name> (.project, .dbeaver/data-sources.json, credentials-config.json). Returns the path.
    It does NOT register the project: DBeaver/Eclipse keep the project list in .metadata/.plugins/
    org.eclipse.core.resources/.root/<n>.tree (binary, not touched here) — see registered()."""
    path = os.path.join(workspace, name)
    os.makedirs(os.path.join(path, ".dbeaver"), exist_ok=True)
    with open(os.path.join(path, ".project"), "w", encoding="utf-8", newline="\n") as f:
        f.write(PROJECT_XML.format(name=name, comment=comment or "Merged DBeaver project (dbeaver_bundle)"))
    with open(os.path.join(path, ".dbeaver", "data-sources.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent="\t")
    with open(os.path.join(path, ".dbeaver", "credentials-config.json"), "wb") as f:
        f.write(encrypt_credentials(creds))
    return path


def registered(workspace: str, name: str) -> bool:
    """Whether DBeaver knows the project: its name appears in the workspace resource tree."""
    root = os.path.join(workspace, ".metadata", ".plugins", "org.eclipse.core.resources", ".root")
    try:
        trees = [f for f in os.listdir(root) if f.endswith(".tree")]
    except OSError:
        return False
    for t in trees:
        with open(os.path.join(root, t), "rb") as f:
            if name.encode() in f.read():
                return True
    return False
