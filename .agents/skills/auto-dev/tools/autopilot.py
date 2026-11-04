#!/usr/bin/env python3
"""Autopilot — MỘT LỆNH chạy trọn chu trình; Telegram CHỈ xuất hiện khi cần con người.

    python autopilot.py run --resolve-existing        # đọc lại TOÀN BỘ task rồi tự chạy

Ba pha nối tiếp (mỗi pha là mảnh đã có, autopilot chỉ là keo):
  1. REVIEW   task_resolver --once --enqueue [--resolve-existing]
              -> verify-in-code từng task: FIXED hỏi đóng (nút Telegram);
                 NOT_FIXED xếp vào queue theo ưu tiên.
  2. PREP     với từng item chưa approved: spawn agent chạy /atask-prep <id> autopilot
              -> làm rõ (hỏi Telegram khi thiếu info) -> plan -> verify -> gate
                 after_plan (Telegram) -> approve. Người CHƯA trả lời kịp -> agent
                 timeout, item giữ nguyên, autopilot đi tiếp (ping nhắc cuối phiên);
                 chạy lại autopilot sẽ chờ tiếp (KHÔNG debate lại — plan đã có).
  3. EXECUTE  queue_worker: tuần tự code theo plan đã duyệt -> fix_loop verify
              -> merge LOCAL vào nhánh gốc -> task kế. Kẹt = PARK + báo, không chờ.

Telegram chạm người đúng 3 chỗ: câu hỏi làm rõ · duyệt solution · (nút) đóng task FIXED
— cộng thông báo park/tổng kết (không cần trả lời).

Usage:
    python autopilot.py run [--resolve-existing] [--max-tasks 0] [--prep-timeout 2400]
        [--skip-resolver] [--skip-execute] [--poll-updates]
"""

import argparse
import json
import os
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
import queue_worker  # noqa: E402
import task_queue    # noqa: E402

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
_RESOLVER = os.path.join(_SKILLS, "atask-automation", "tools", "task_resolver.py")


def _log(msg):
    print(f"[autopilot {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _phase_review(args):
    """task_resolver review loạt task -> enqueue NOT_FIXED. Test seam."""
    # --max-per-cycle phải đủ lớn: mặc định 2 của resolver làm `--once` chỉ nhặt 2 task.
    cmd = [sys.executable, _RESOLVER, "--once", "--enqueue",
           "--max-per-cycle", str(getattr(args, "review_batch", 100))]
    if args.resolve_existing:
        cmd.append("--resolve-existing")
    _log("REVIEW: " + " ".join(os.path.basename(c) for c in cmd[1:]))
    proc = subprocess.run(cmd, cwd=os.path.dirname(_RESOLVER), timeout=args.review_timeout,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = ((proc.stdout or "") + (proc.stderr or ""))[-500:]
    return {"rc": proc.returncode, "tail": tail}


def _spawn_prep(task_id, timeout):
    """Một agent prep cho một task (blocking; agent tự chờ Telegram bên trong). Test seam."""
    prompt = (f"/atask-prep {task_id} autopilot — chạy đúng .claude/commands/atask-prep.md. "
              f"Plan/gate đã tồn tại từ lần trước thì DÙNG LẠI (chỉ wait, không debate lại). "
              f"Chờ Telegram bằng tg_gate.py wait; hết giờ thì để nguyên trạng thái rồi thoát.")
    proc = subprocess.run([CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions"],
                          cwd=task_queue._repo_root(), timeout=timeout,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode


def _pending_prep():
    return sorted([i for i in task_queue._all_items()
                   if i["state"] in ("needs_clarification", "ready")],
                  key=lambda i: (i.get("priority", 2), i.get("created", "")))


def cmd_run(args):
    t0 = time.time()
    quiet = getattr(args, "quiet_idle", False)   # watch mode: đừng spam khi không có gì
    if not quiet:
        task_queue._send_tg("🚀 <b>Autopilot</b> bắt đầu — sẽ chỉ hỏi khi cần làm rõ/duyệt.")

    review = None
    if not args.skip_resolver:
        review = _phase_review(args)
        if review["rc"] != 0:
            _log(f"REVIEW lỗi (rc={review['rc']}): {review['tail'][-200:]}")

    waiting_human, prepped = [], []
    for item in _pending_prep():
        qid, tid = item["qid"], item["task_id"]
        _log(f"PREP ▶ {qid} ({item['state']})")
        try:
            _spawn_prep(tid, args.prep_timeout)
        except subprocess.TimeoutExpired:
            pass
        after = task_queue._load(qid) or {}
        if after.get("state") == "approved":
            prepped.append(qid)
            _log(f"PREP ✅ {qid} approved")
        else:
            waiting_human.append(f"{qid} ({after.get('state')})")
            _log(f"PREP ⏳ {qid} chờ người ({after.get('state')})")

    executed = {"done": [], "parked": []}
    if not args.skip_execute:
        _log("EXECUTE: queue_worker (approved, tuần tự)")
        executed = queue_worker.cmd_run(argparse.Namespace(
            project=args.project, env=args.env, max_tasks=args.max_tasks,
            interval=0, task_timeout=args.task_timeout, dry_run=False))

    mins = int((time.time() - t0) / 60)
    happened = bool(executed["done"] or executed["parked"] or prepped or waiting_human)
    if happened or not quiet:
        summary = (f"🏁 <b>Autopilot xong lượt</b> ({mins} phút)\n"
                   f"✅ code+merge: {len(executed['done'])}"
                   + (f" — {', '.join(executed['done'])}" if executed["done"] else "") + "\n"
                   f"⏸️ park: {len(executed['parked'])}"
                   + (f" — {', '.join(executed['parked'])}" if executed["parked"] else "") + "\n"
                   f"⏳ chờ bạn trả lời Telegram: {len(waiting_human)}"
                   + (f" — {'; '.join(waiting_human)}" if waiting_human else "")
                   + ("\n👉 Trả lời xong, vòng poll sau tự đi tiếp." if waiting_human else ""))
        task_queue._send_tg(summary)
    else:
        _log("lượt rảnh — không có task mới/chờ")
    return {"ok": True, "review": review, "approved_in_prep": prepped,
            "waiting_human": waiting_human, **executed,
            "minutes": mins}


def cmd_watch(args):
    """CHẾ ĐỘ POLL: lặp cmd_run mãi (backoff + health log qua daemon_common).

    - `--resolve-existing` chỉ áp cho LƯỢT ĐẦU (các lượt sau resolver tự thấy task
      mới/đổi trạng thái nhờ state file).
    - Task đang chờ người trả lời Telegram được prep-resume MỖI lượt (plan/gate cũ
      dùng lại, chỉ wait) → bạn trả lời lúc nào, lượt kế đi tiếp lúc đó.
    - Ctrl+C để dừng. Trạng thái: `python ../../dev-automation/tools/daemon_common.py status`.
    """
    sys.path.insert(0, os.path.join(_SKILLS, "dev-automation", "tools"))
    import daemon_common
    first = {"v": True}

    def one_pass():
        a = argparse.Namespace(**vars(args))
        a.quiet_idle = True
        a.resolve_existing = args.resolve_existing and first["v"]
        first["v"] = False
        cmd_run(a)

    task_queue._send_tg(f"👁️ <b>Autopilot WATCH</b> bật — poll mỗi {args.interval}s; "
                        f"chỉ nhắn khi có việc/cần bạn. Ctrl+C để tắt.")
    daemon_common.supervise(one_pass, interval=args.interval, label="autopilot",
                            notify=lambda m: task_queue._send_tg(f"🚨 autopilot: {m}"))
    return {"ok": True, "stopped": True}


def _common_args(r):
    r.add_argument("--resolve-existing", action="store_true",
                   help="đọc lại TOÀN BỘ backlog (bỏ baseline resolver)")
    r.add_argument("--skip-resolver", action="store_true", help="dùng queue hiện có")
    r.add_argument("--skip-execute", action="store_true", help="chỉ review+prep")
    r.add_argument("--project", default=None)
    r.add_argument("--env", default="dev")
    r.add_argument("--max-tasks", type=int, default=0)
    r.add_argument("--review-batch", type=int, default=100,
                   help="số task tối đa resolver review trong 1 lần chạy (mặc định 100)")
    r.add_argument("--review-timeout", type=int, default=7200,
                   help="giây cho cả pha review (mỗi task 1 lượt phân tích agent)")
    r.add_argument("--prep-timeout", type=int, default=2400,
                   help="mỗi task prep chờ người tối đa (mặc định 40 phút)")
    r.add_argument("--task-timeout", type=int, default=7200)


def main():
    ap = argparse.ArgumentParser(description="One-command pipeline: review -> prep -> execute.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="chạy MỘT lượt rồi thoát (tổng kết Telegram)")
    _common_args(r)
    w = sub.add_parser("watch", help="CHẾ ĐỘ POLL: lặp mãi, backoff, chỉ nhắn khi có việc")
    _common_args(w)
    w.add_argument("--interval", type=int, default=600, help="giây giữa các lượt (mặc định 10 phút)")
    args = ap.parse_args()
    out = cmd_watch(args) if args.cmd == "watch" else cmd_run(args)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
