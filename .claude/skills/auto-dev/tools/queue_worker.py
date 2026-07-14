#!/usr/bin/env python3
"""Queue worker — tự chạy TUẦN TỰ các task đã xếp hàng (B.4), mỗi task một agent.

Vòng đời một lượt:
    task_queue.next (lock luồng task_resolver, ưu tiên theo priority->tuổi)
      -> spawn `claude -p "/atask-run <task_id> batch"` (pipeline mode=auto: gate người
         thay bằng gate bằng chứng; task mơ hồ tự PARK, không kẹt hàng)
      -> agent tự `done ok|fail` khi xong; worker đối chiếu state item:
           done   -> task kế (nhánh gốc local đã chứa merge của task này)
           failed -> park, báo Telegram, task kế
           processing (agent chết giữa chừng) -> release lock + done fail + báo
    hết item ready -> --interval N thì ngủ rồi poll tiếp, không thì thoát.

Git-chain nằm TRONG pipeline batch (atask-run.md § BATCH): trước mỗi task
`checkout <gốc> && pull`, xong task `merge --no-ff` nhánh task vào GỐC LOCAL
(chưa đụng remote) — task sau luôn build trên kết quả task trước.

Usage:
    python queue_worker.py run --project atask [--env dev] [--max-tasks 3]
        [--interval 0] [--task-timeout 7200] [--dry-run]
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
sys.path.insert(0, _HERE)
import task_queue  # noqa: E402

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")


def _repo_root():
    search = _HERE
    for _ in range(6):
        if os.path.isdir(os.path.join(search, ".git")) or \
           os.path.isfile(os.path.join(search, "CLAUDE.md")):
            return search
        search = os.path.dirname(search)
    return _HERE


def _spawn_agent(task_id, timeout):
    """Chạy một phiên agent đầy đủ cho task (blocking). Test seam."""
    prompt = (f"/atask-run {task_id} batch — item đã APPROVED (plan+verify đã duyệt ở "
              f"/atask-prep); chạy đúng .claude/commands/atask-run.md: luồng THỰC THI "
              f"thuần code, merge LOCAL vào nhánh gốc, kẹt thì PARK + báo Telegram.")
    proc = subprocess.run(
        [CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions"],
        cwd=_repo_root(), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout)
    return proc.returncode, (proc.stdout or "")[-2000:]


def _outcome(item):
    """state item sau khi agent thoát -> hành động của worker."""
    st = (item or {}).get("state")
    if st == "done":
        return "ok"
    if st in ("failed", "needs_clarification"):
        return "parked"
    if st == "processing":
        return "stuck"          # agent chết giữa chừng, còn giữ lock
    return "unknown"


def _notify(text):
    ok, _ = task_queue._send_tg(text)
    return ok


def cmd_run(args):
    done, parked, cycles = [], [], 0
    while True:
        if args.max_tasks and len(done) + len(parked) >= args.max_tasks:
            break
        nxt = task_queue.cmd_next(argparse.Namespace(owner="task_resolver"))
        if nxt.get("error"):        # lock đang bị resolver/phiên khác giữ
            print(f"[worker] {nxt.get('message')}", file=sys.stderr)
            if not args.interval:
                break
            time.sleep(args.interval)
            continue
        item = nxt.get("item")
        if not item:
            if not args.interval:
                break               # hết hàng -> nghỉ
            time.sleep(args.interval)
            continue
        qid, task_id = item["qid"], item["task_id"]
        title = (item.get("title") or "")[:60]
        print(f"[worker] ▶ {qid} {title!r}")
        if args.dry_run:
            # done (không phải release!) — release trả item về ready -> claim lại vô hạn
            task_queue.cmd_done(argparse.Namespace(qid=qid, result="ok",
                                                   note="worker dry-run",
                                                   owner="task_resolver"))
            done.append(qid)
            continue
        try:
            rc, tail = _spawn_agent(task_id, args.task_timeout)
        except subprocess.TimeoutExpired:
            rc, tail = -1, "(task timeout)"
        after = task_queue._load(qid)
        action = _outcome(after)
        if action == "ok":
            done.append(qid)
            print(f"[worker] ✅ {qid}")
        elif action == "parked":
            parked.append(qid)
            _notify(f"⏸️ Task <b>{title}</b> bị PARK ({after.get('state')}) — "
                    f"xem notes/câu hỏi rồi requeue/answer. Worker chạy task kế.")
        else:  # stuck/unknown: agent chết còn giữ lock -> gỡ để hàng không kẹt
            if (after or {}).get("state") == "processing":
                # done fail tự nhả lock theo owner ghi trên item
                task_queue.cmd_done(argparse.Namespace(
                    qid=qid, result="fail", owner="task_resolver",
                    note=f"worker: agent exited rc={rc} mid-run; {tail[-300:]}"))
            else:
                task_queue.cmd_release(argparse.Namespace(owner="task_resolver"))
            parked.append(qid)
            _notify(f"💥 Task <b>{title}</b>: agent thoát giữa chừng (rc={rc}) — "
                    f"đã nhả lock + đánh fail. Worker chạy task kế.")
        cycles += 1
    summary = {"ok": True, "done": done, "parked": parked, "cycles": cycles}
    if done or parked:
        _notify(f"🏁 Worker xong đợt: ✅ {len(done)} · ⏸️ {len(parked)}"
                + (f"\ndone: {', '.join(done)}" if done else "")
                + (f"\nparked: {', '.join(parked)}" if parked else ""))
    return summary


def main():
    ap = argparse.ArgumentParser(description="Serial queue worker: next -> agent /atask-run batch -> done.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--project", default=None, help="(thông tin) project chính của đợt chạy")
    r.add_argument("--env", default="dev")
    r.add_argument("--max-tasks", type=int, default=0, help="0 = chạy tới khi hết hàng")
    r.add_argument("--interval", type=int, default=0,
                   help=">0: hết hàng thì ngủ N giây rồi poll tiếp (chạy như daemon)")
    r.add_argument("--task-timeout", type=int, default=7200)
    r.add_argument("--dry-run", action="store_true", help="chỉ claim/release, không spawn agent")
    args = ap.parse_args()
    out = cmd_run(args)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
