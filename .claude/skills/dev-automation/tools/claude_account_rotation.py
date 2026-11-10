#!/usr/bin/env python3
"""Orca Claude account rotation. Preview by default; never restarts terminals.

Uses Orca's local authenticated runtime RPC for switching. OAuth usage fallback
reads existing host credentials; apply mode can renew expired inactive tokens. No secrets
are passed in argv, emitted in output, or saved to rotation state.
"""
import argparse
import concurrent.futures
import datetime
import email.utils
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import tempfile

REFRESH_RETRY_AT = {}
AUTH_STATUS = {}


class FileLock:
    """OS locks release on process exit, including a crashed watcher."""
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.stream = os.fdopen(fd, "r+b", buffering=0)
        if self.path.stat().st_size == 0:
            self.stream.write(b"0")
        self.stream.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError):
            self.stream.close()
            self.stream = None
            raise RuntimeError("Another rotation process holds this lock")
        return self

    def __exit__(self, *args):
        if self.stream:
            self.stream.close()

import config


def user_data_dir():
    override = config.get("ORCA_USER_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "orca"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "orca"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "orca"


def rpc_worker(method, params):
    """Local newline-delimited RPC. Parent process bounds blocking pipe reads."""
    if method not in {"accounts.list", "accounts.selectClaude"}:
        raise ValueError("Unsupported rotation RPC")
    metadata = json.loads((user_data_dir() / "orca-runtime.json").read_text(encoding="utf-8"))
    transport = next(t for t in metadata["transports"] if t["kind"] in {"named-pipe", "unix"})
    request_id = str(uuid.uuid4())
    request = {"id": request_id, "method": method, "params": params, "authToken": metadata["authToken"]}
    connection = None
    if transport["kind"] == "unix":
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(40)
        connection.connect(transport["endpoint"])
        stream = connection.makefile("rwb", buffering=0)
    else:
        stream = open(transport["endpoint"], "r+b", buffering=0)
    try:
        raw = (json.dumps(request) + "\n").encode()
        while raw:
            count = stream.write(raw)
            if not count:
                raise RuntimeError("Orca disconnected")
            raw = raw[count:]
        while True:
            line = stream.readline(8 * 1024 * 1024)
            if not line or not line.endswith(b"\n"):
                raise RuntimeError("Invalid Orca response")
            response = json.loads(line)
            if response.get("_keepalive"):
                continue
            if response.get("id") != request_id:
                raise RuntimeError("Orca response ID mismatch")
            if not response.get("ok"):
                # Do not echo arbitrary server data that could contain credentials.
                raise RuntimeError("Orca RPC rejected: " + str(response.get("error", {}).get("code", "unknown")))
            return response["result"]
    finally:
        stream.close()
        if connection:
            connection.close()


def rpc(method, params):
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "_rpc"],
                            input=json.dumps({"method": method, "params": params}),
                            capture_output=True, text=True, encoding="utf-8", timeout=45)
    if result.returncode:
        raise RuntimeError("Orca runtime call failed; check that Orca is running")
    response = json.loads(result.stdout)
    if response.get("error"):
        raise RuntimeError(response["error"])
    return response["result"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward account credentials to a redirect destination.


def renew_inactive_credentials(credentials_path, account_id):
    """Refresh an inactive account, preserving rotated tokens and unrelated fields."""
    if time.time() < REFRESH_RETRY_AT.get(account_id, 0):
        AUTH_STATUS[account_id] = "refresh_backoff"
        return None
    lock = FileLock(credentials_path.parent / ".orca-rotation-refresh.lock")
    try:
        lock.__enter__()
    except RuntimeError:
        AUTH_STATUS[account_id] = "refresh_locked"
        return None
    temporary = None
    try:
        snapshot = rpc("accounts.list", {"refreshUsage": False})
        selection = snapshot["claude"].get("activeAccountIdsByRuntime", {})
        if account_id == selection.get("host") or account_id in selection.get("wsl", {}).values():
            AUTH_STATUS[account_id] = "active_refresh_owned_by_claude"
            return None
        original = credentials_path.read_text(encoding="utf-8")
        credentials = json.loads(original)
        oauth = credentials.get("claudeAiOauth", {})
        if oauth.get("expiresAt", 0) > time.time() * 1000:
            return credentials
        if not oauth.get("refreshToken"):
            return None
        url, client_id = config.claude_oauth_config()
        if urllib.parse.urlsplit(url).scheme != "https":
            return None
        payload = urllib.parse.urlencode({"grant_type": "refresh_token",
                    "refresh_token": oauth["refreshToken"], "client_id": client_id}).encode()
        request = urllib.request.Request(url, data=payload,
                                        headers={"Content-Type": "application/x-www-form-urlencoded",
                                                 "User-Agent": "claude-code/2.1.0", "Accept": "application/json"})
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=12) as response:
            updated = json.load(response)
        if not isinstance(updated.get("access_token"), str) or not updated["access_token"]:
            return None
        expiry = updated.get("expires_in")
        if isinstance(expiry, bool) or not isinstance(expiry, (float, int)) or not math.isfinite(expiry) or expiry <= 0:
            return None
        # Orca may have refreshed this account concurrently; never clobber its result.
        if credentials_path.read_text(encoding="utf-8") != original:
            return json.loads(credentials_path.read_text(encoding="utf-8"))
        oauth["accessToken"] = updated["access_token"]
        oauth["expiresAt"] = int(time.time() * 1000 + expiry * 1000)
        if isinstance(updated.get("refresh_token"), str) and updated["refresh_token"]:
            oauth["refreshToken"] = updated["refresh_token"]
        if isinstance(updated.get("scope"), str) and updated["scope"]:
            oauth["scopes"] = updated["scope"].split()
        fd, name = tempfile.mkstemp(prefix=".rotation-auth-", dir=credentials_path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(credentials, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, credentials_path)
        temporary = None
        AUTH_STATUS.pop(account_id, None)
        return credentials
    except urllib.error.HTTPError as exc:
        delay = 900 if exc.code in (400, 401, 403) else 300
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                try:
                    delay = max(delay, email.utils.parsedate_to_datetime(retry_after).timestamp() - time.time())
                except (TypeError, ValueError, OverflowError):
                    pass
        REFRESH_RETRY_AT[account_id] = time.time() + min(delay, 86400)
        AUTH_STATUS[account_id] = "refresh_http_" + str(exc.code)
        return None
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError,
            subprocess.TimeoutExpired, RuntimeError):
        REFRESH_RETRY_AT[account_id] = time.time() + 300
        AUTH_STATUS[account_id] = "refresh_failed"
        return None
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
        lock.__exit__()


def host_usage(account, now, allow_refresh=False):
    """Preview reads only; apply mode also renews expired inactive host tokens."""
    account_id = account["id"]
    try:
        if str(uuid.UUID(account_id)) != account_id:
            return None
        root = (user_data_dir() / "claude-accounts").resolve()
        auth_dir = root / account_id / "auth"
        if auth_dir.is_symlink() or not auth_dir.resolve().is_relative_to(root):
            return None
        if (auth_dir / ".orca-managed-claude-auth").read_text(encoding="utf-8").strip() != account_id:
            return None
        credentials_path = auth_dir / ".credentials.json"
        if credentials_path.is_symlink():
            return None
        credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
        oauth = credentials.get("claudeAiOauth", {})
        token = oauth.get("accessToken")
        if allow_refresh and oauth.get("expiresAt", 0) <= now:
            credentials = renew_inactive_credentials(credentials_path, account_id)
            oauth = (credentials or {}).get("claudeAiOauth", {})
            token = oauth.get("accessToken")
        if not token or oauth.get("expiresAt", 0) <= time.time() * 1000:
            return None
        url = config.claude_usage_url()
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            return None
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token,
                "anthropic-beta": "oauth-2025-04-20", "User-Agent": "claude-code/2.1.0"})
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=12) as response:
            data = json.load(response)
        limits = {"status": "ok", "updatedAt": int(time.time() * 1000)}
        for field, source in (("session", "five_hour"), ("weekly", "seven_day")):
            bucket = data.get(source)
            if not isinstance(bucket, dict):
                return None
            reset = bucket.get("resets_at")
            limits[field] = {"usedPercent": bucket.get("utilization"), "resetsAt":
                int(datetime.datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp() * 1000) if reset else None}
        AUTH_STATUS.pop(account_id, None)
        return limits
    except urllib.error.HTTPError as exc:
        AUTH_STATUS[account_id] = "usage_http_" + str(exc.code)
        return None
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        AUTH_STATUS[account_id] = "usage_unavailable"
        return None


def valid_usage(limits, now, max_age=180000):
    if not isinstance(limits, dict) or limits.get("status") != "ok" or limits.get("error"):
        return None
    timestamp = limits.get("updatedAt")
    if not isinstance(timestamp, (float, int)) or not 0 <= now - timestamp <= max_age:
        return None
    values = []
    for field in ("session", "weekly"):
        bucket = limits.get(field)
        if not isinstance(bucket, dict):
            return None
        value = bucket.get("usedPercent")
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 100:
            return None
        reset = bucket.get("resetsAt")
        if isinstance(reset, (float, int)) and reset <= now:
            return None  # Refresh after reset rather than guessing the new usage.
        values.append(value)
    return values


def choose(accounts, usages, active_id, now):
    """Strict boundaries, round robin; unknown quota never raises the tier."""
    ids = [a["id"] for a in accounts]
    if not ids or active_id not in ids:
        return {"action": "wait", "reason": "active_account_outside_pool", "threshold": 80}
    known = {i: valid_usage(usages.get(i), now) for i in ids}
    threshold = 80
    if all(v is not None and max(v) >= 80 for v in known.values()):
        threshold = 95
    eligible = [i for i in ids if known[i] is not None and max(known[i]) < threshold]
    if active_id in eligible:
        return {"action": "keep", "threshold": threshold, "accountId": active_id}
    if eligible:
        start = ids.index(active_id)
        following = ids[start + 1:] + ids[:start + 1]
        return {"action": "switch", "threshold": threshold,
                "accountId": next(i for i in following if i in eligible)}
    return {"action": "wait", "threshold": threshold,
            "reason": "quota_unknown" if any(v is None for v in known.values()) else "all_accounts_at_95"}


def inspect_pool(pool, allow_refresh=False):
    snapshot = rpc("accounts.list", {"refreshUsage": not allow_refresh})
    available = {a["id"]: a for a in snapshot["claude"]["accounts"] if a.get("managedAuthRuntime", "host") == "host"}
    accounts = [available[account_id] for account_id in pool if account_id in available]
    active = snapshot["claude"]["activeAccountId"]
    limits = snapshot.get("rateLimits", {})
    usages = {entry["accountId"]: entry.get("rateLimits") for entry in limits.get("inactiveClaudeAccounts", [])}
    current = limits.get("claude", {})
    # Only attribute active usage when the runtime provides matching provenance.
    if current.get("usageMetadata", {}).get("authProvenance") == "managed:" + str(active):
        usages[active] = current
    now = int(time.time() * 1000)
    missing = [a for a in accounts if valid_usage(usages.get(a["id"]), now) is None]
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        for account, usage in zip(missing, executor.map(lambda a: host_usage(a, now, allow_refresh), missing)):
            usages[account["id"]] = usage
    now = int(time.time() * 1000)
    if len(accounts) != len(pool):
        decision = {"action": "wait", "threshold": 80, "reason": "configured_accounts_missing"}
    else:
        decision = choose(accounts, usages, active, now)
    return {"decision": decision, "activeAccountId": active, "accounts": [
        {"id": a["id"], "email": a.get("email"), "usage": valid_usage(usages.get(a["id"]), now),
         "authStatus": AUTH_STATUS.get(a["id"])} for a in accounts]}


def main():
    if sys.argv[1:] == ["_rpc"]:
        try:
            request = json.load(sys.stdin)
            print(json.dumps({"result": rpc_worker(request["method"], request["params"])}))
        except Exception as exc:
            # Internal exceptions can contain auth file contents; emit class only.
            print(json.dumps({"error": "Local runtime failure: " + type(exc).__name__}))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", nargs="+", required=True, help="Explicit managed account UUIDs")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--refresh-delay", type=int, default=0, help="Delay first expired-token refresh after recent rate limiting")
    parser.add_argument("--status-file", type=Path, help="Write the latest decision to a JSON status file")
    parser.add_argument("--apply", "--apply-new-sessions", dest="apply", action="store_true",
                        help="WRITE: renew inactive tokens and switch the same way as Orca's account button")
    args = parser.parse_args()
    pool = list(dict.fromkeys(args.accounts))
    if args.interval < 60:
        parser.error("interval must be at least 60 seconds")
    for account_id in pool:
        try:
            uuid.UUID(account_id)
        except ValueError:
            parser.error("accounts must be managed account UUIDs")
    if args.refresh_delay < 0:
        parser.error("refresh-delay must not be negative")
    for account_id in pool:
        REFRESH_RETRY_AT[account_id] = time.time() + args.refresh_delay
    watcher_lock = None
    if args.watch:
        watcher_lock = FileLock(user_data_dir() / "claude-account-rotation" / "watch.lock")
        watcher_lock.__enter__()
    while True:
        try:
            result = inspect_pool(pool, allow_refresh=args.apply)
            result["checkedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            result["pid"] = os.getpid()
            result["applied"] = False
            if args.apply and result["decision"]["action"] == "switch":
                # Recheck selection to avoid overwriting a human switch during polling.
                latest = rpc("accounts.list", {"refreshUsage": False})
                if latest["claude"]["activeAccountId"] != result["activeAccountId"]:
                    result["decision"] = {"action": "wait", "reason": "selection_changed"}
                else:
                    target = result["decision"]["accountId"]
                    rpc("accounts.selectClaude", {"accountId": target})
                    verify = rpc("accounts.list", {"refreshUsage": False})
                    result["applied"] = verify["claude"]["activeAccountId"] == target
                    result["liveSessionReload"] = "Claude Code must adopt changed credentials; terminal processes are preserved"
            print(json.dumps(result, ensure_ascii=False), flush=True)
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
            result = {"error": True, "kind": type(exc).__name__, "action": "wait", "pid": os.getpid(),
                      "checkedAt": datetime.datetime.now(datetime.timezone.utc).isoformat()}
            print(json.dumps(result), flush=True)
            if not args.watch:
                return 1
        if args.status_file:
            args.status_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.status_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, args.status_file)
        if not args.watch:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
