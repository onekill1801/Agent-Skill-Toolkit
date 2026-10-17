#!/usr/bin/env python3
"""bg_notify.py — chạy một tác vụ DÀI ở tiến trình TÁCH RỜI, xong thì TỰ ĐẨY kết quả về Telegram.

Vì sao cần
----------
Khi điều khiển qua Telegram bridge, mỗi tin nhắn là một phiên `claude -p` headless
ONE-SHOT: agent kết thúc lượt là tiến trình thoát NGAY. Mọi tác vụ chờ-nền mà agent
"spawn rồi hẹn báo sau" (jenkins build --wait, compile, test…) sẽ bị MỒ CÔI — không còn
agent nào sống để nhận thông báo hoàn thành rồi đẩy về cho bạn. Kết quả: tác vụ xong
nhưng bạn KHÔNG nhận được gì.

Tool này tự TÁCH RỜI khỏi tiến trình cha (Windows: DETACHED_PROCESS; POSIX: setsid),
chạy lệnh tới khi xong, rồi gửi THẲNG kết quả về chat Telegram qua tg_api — không phụ
thuộc `claude -p` còn sống hay không. Tự đo THỜI LƯỢNG và đoán SUCCESS/FAILURE từ JSON
output (`passed`/`result`/`error`) thay vì chỉ dựa vào exit code (vốn không phản ánh
build FAILURE của jenkins.py).

Cách dùng (agent fire-and-forget)
---------------------------------
    cd .claude/skills/dev-automation/tools
    python bg_notify.py --label "Build dev atask" -- \
        python jenkins.py build --project atask --env dev --wait
    python bg_notify.py --label "Compile/test atask" -- \
        python test_runner.py run --project atask --kind test

Mọi thứ sau `--` là LỆNH cần chạy (không qua shell). Tool in NGAY một JSON
`{"detached": true, "pid": …, "log": …}` rồi trả quyền — agent có thể kết thúc lượt
an toàn; khi lệnh xong, bạn nhận tin Telegram.

Cờ
--
  --label   nhãn hiển thị trong tin báo (mặc định "Tác vụ nền")
  --chat    chat id đích (mặc định lấy env CLAUDE_TG_CHAT_ID do bridge set sẵn)
  --tail    số dòng cuối output đính kèm tin báo (mặc định 0 = chỉ báo trạng thái +
            thời lượng; full output luôn nằm trong .log)
  --detach / --no-detach
            ép bật/tắt chế độ tách rời. Mặc định: BẬT khi đang chạy dưới bridge
            (env CLAUDE_TG_BRIDGE=1), TẮT (chạy đồng bộ, để debug) khi chạy tay.

Không có chat (chạy ngoài bridge) → vẫn chạy lệnh, ghi kết quả ra log + in JSON tóm tắt
(không crash, không gửi Telegram).
"""

import argparse
import html
import json
import os
import subprocess
import sys
import time

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
# tools -> dev-automation -> skills -> .claude -> repo root
REPO_ROOT = os.path.abspath(os.path.join(TOOLS_DIR, "..", "..", "..", ".."))
LOG_DIR = os.path.join(REPO_ROOT, "temp", "bg_notify")

# tg_api lives in the remote-control skill; import it lazily/optionally so this
# tool still works (log-only) on a machine without the bridge configured.
_RC_DIR = os.path.abspath(os.path.join(TOOLS_DIR, "..", "..", "remote-control", "tools"))
if _RC_DIR not in sys.path:
    sys.path.insert(0, _RC_DIR)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def _slug(label: str) -> str:
    keep = [c if c.isalnum() else "-" for c in label.lower()]
    s = "".join(keep).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return s[:40] or "task"


def _log_path(label: str) -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    # os.getpid() keeps concurrent watchers from colliding; time isn't used so the
    # path is deterministic enough for the agent to read back if it wants.
    return os.path.join(LOG_DIR, f"{_slug(label)}-{os.getpid()}.log")


def _try_json(text: str):
    """Best-effort parse of a tool's JSON output: whole string first, then the
    last {...} block (tools may print a banner line before the JSON)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
    return None


def _verdict(proc) -> tuple:
    """Return (ok: bool, detail: str|None). Trusts JSON `passed`/`result`/`error`
    over the exit code, because jenkins.py/emit() returns exit 0 even on a build
    FAILURE (only `error: true` flips the code to 1)."""
    obj = _try_json(proc.stdout)
    if isinstance(obj, dict):
        if obj.get("error"):
            return False, str(obj.get("message") or obj.get("result") or "error")
        if "passed" in obj:
            return bool(obj["passed"]), obj.get("result")
        if obj.get("result"):
            return obj["result"] == "SUCCESS", obj["result"]
    return proc.returncode == 0, None


def _fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    m, s = divmod(s, 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def _tail(text: str, n: int) -> str:
    if n <= 0:                       # -0 == 0 would slice the WHOLE list, so guard
        return ""
    lines = (text or "").splitlines()
    return "\n".join(lines[-n:]) if len(lines) > n else (text or "")


def _notify_fchat(summary: dict, tail_text: str):
    """Đăng kết quả về TChat group (tag người yêu cầu) nếu env FCHAT_NOTIFY_GROUP
    được set — group_watch.py set sẵn khi giao việc cho agent. Gọi notify_group.py
    của skill tchat như SUBPROCESS để tránh đụng module config.py trùng tên.
    Trả True nếu đã đăng, False nếu không cấu hình / lỗi."""
    gid = os.environ.get("FCHAT_NOTIFY_GROUP")
    if not gid:
        return False
    icon = "✅" if summary["ok"] else "❌"
    status = "xong" if summary["ok"] else "THẤT BẠI"
    text = f"{icon} {summary['label']} — {status} (⏱ {summary['duration']})"
    if tail_text.strip():
        text += "\n" + tail_text
    script = os.path.join(REPO_ROOT, ".claude", "skills", "tchat-automation",
                          "tools", "notify_group.py")
    if not os.path.isfile(script):
        return False
    argv = [sys.executable, script, "--group", gid, "--text", text]
    gtype = os.environ.get("FCHAT_NOTIFY_GROUP_TYPE")
    uid = os.environ.get("FCHAT_NOTIFY_USER_ID")
    uname = os.environ.get("FCHAT_NOTIFY_USER_NAME")
    if gtype:
        argv += ["--group-type", gtype]
    if uid and uname:
        argv += ["--user-id", uid, "--user-name", uname]
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _notify(chat, summary: dict, tail_text: str):
    """Push the result to Telegram if a chat + tg_api are available; otherwise
    return False so the caller logs locally instead. Kept deliberately short —
    just status + duration. The log tail is only attached when --tail > 0 (off by
    default); the full output is always in the .log file for inspection."""
    if not chat:
        return False
    try:
        import tg_api  # noqa: WPS433 - optional, present only with remote-control
    except Exception:  # noqa: BLE001
        return False
    icon = "✅" if summary["ok"] else "❌"
    status = "xong" if summary["ok"] else "THẤT BẠI"
    msg = f"{icon} <b>{html.escape(summary['label'])}</b> — {status}\n⏱ {summary['duration']}"
    if tail_text.strip():
        msg += f"\n<pre>{html.escape(tail_text)}</pre>"
    res = tg_api.send_message(chat, msg, bot=tg_api.notify_bot())
    return bool(res.get("ok"))


def run_worker(cmd: list, label: str, chat, tail_n: int) -> dict:
    """Run `cmd` to completion, build a summary, notify Telegram (or log)."""
    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        proc = subprocess.CompletedProcess(cmd, 127, "", str(e))
    elapsed = time.time() - started

    ok, detail = _verdict(proc)
    combined = (proc.stdout or "")
    if proc.stderr:
        combined += ("\n--- stderr ---\n" + proc.stderr)
    summary = {
        "label": label,
        "ok": ok,
        "detail": detail,
        "returncode": proc.returncode,
        "duration": _fmt_duration(elapsed),
        "elapsed_seconds": round(elapsed, 1),
        "command": cmd,
    }
    # Full output goes to this process's stdout — which the detached parent has
    # redirected to the .log file — so the run is inspectable after the fact.
    print(f"=== bg_notify: {label} === exit={proc.returncode} ok={ok} "
          f"duration={summary['duration']}")
    print(combined)
    sys.stdout.flush()

    # Báo CẢ HAI kênh: TChat (nếu có context group_watch → tag người yêu cầu) VÀ
    # Telegram (nếu có chat → khép vòng sau khi bấm Duyệt). Telegram-bridge thuần
    # không set FCHAT env → _notify_fchat trả False, chỉ báo Telegram như cũ.
    tail_text = _tail(combined, tail_n)
    summary["notified_fchat"] = _notify_fchat(summary, tail_text)
    summary["notified"] = _notify(chat, summary, tail_text)
    return summary


def _spawn_detached(child_argv: list, log_path: str) -> int:
    """Launch the worker as a process that OUTLIVES this one, with std streams
    redirected to `log_path` (NOT inherited pipes) so a parent that captures our
    output — the bridge's `subprocess.run` — doesn't block waiting on the pipe."""
    logf = open(log_path, "ab")  # noqa: SIM115 - handed to the child; closed on our exit
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, close_fds=True)
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                                   | CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(child_argv, **kwargs)
    return proc.pid


def _split_argv(argv: list) -> tuple:
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def main() -> int:
    opt_argv, cmd = _split_argv(sys.argv[1:])

    p = argparse.ArgumentParser(
        prog="bg_notify.py",
        description="Chạy tác vụ dài tách rời, xong tự báo kết quả về Telegram.")
    p.add_argument("--label", default="Tác vụ nền")
    p.add_argument("--chat", default=None)
    p.add_argument("--tail", type=int, default=0,
                   help="số dòng cuối output đính kèm tin báo (mặc định 0 = không đính, "
                        "chỉ báo trạng thái + thời lượng; full output luôn có trong .log)")
    p.add_argument("--detach", action="store_true",
                   help="ép chạy tách rời (mặc định bật khi dưới bridge)")
    p.add_argument("--no-detach", dest="no_detach", action="store_true",
                   help="ép chạy đồng bộ (để debug)")
    p.add_argument("--_run", dest="run_worker", action="store_true",
                   help=argparse.SUPPRESS)  # internal: this IS the detached worker
    args = p.parse_args(opt_argv)

    if not cmd:
        print(json.dumps({"error": True, "message":
                          "thiếu lệnh: đặt sau `--`, vd: bg_notify.py --label X -- python jenkins.py build …"},
                         ensure_ascii=False, indent=2))
        return 1

    chat = args.chat or os.environ.get("CLAUDE_TG_CHAT_ID") or None

    # The detached worker (or an explicit synchronous run) executes the command.
    if args.run_worker or args.no_detach or \
            not (args.detach or os.environ.get("CLAUDE_TG_BRIDGE") == "1"):
        summary = run_worker(cmd, args.label, chat, args.tail)
        if not args.run_worker:   # synchronous mode: report inline
            print(json.dumps({"detached": False, **summary}, ensure_ascii=False, indent=2))
        return 0 if summary["ok"] else 1

    # Detached mode: relaunch ourselves as the worker, then return immediately.
    log_path = _log_path(args.label)
    child = [sys.executable, os.path.abspath(__file__),
             "--label", args.label, "--tail", str(args.tail), "--_run"]
    if chat:
        child += ["--chat", str(chat)]
    child += ["--"] + cmd
    try:
        pid = _spawn_detached(child, log_path)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": True, "message": f"không tách rời được tiến trình: {e}"},
                         ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({
        "detached": True,
        "pid": pid,
        "label": args.label,
        "log": log_path,
        "chat": chat,
        "note": "Đã chạy nền tách rời. Khi lệnh xong sẽ tự gửi kết quả về Telegram"
                + ("" if chat else " (KHÔNG có chat → chỉ ghi log)") + ".",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
