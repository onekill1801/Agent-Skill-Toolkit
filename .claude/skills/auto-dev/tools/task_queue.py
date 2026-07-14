#!/usr/bin/env python3
"""Task queue — intake aTask/Azure tasks, clarify them, then process ONE at a time.

Why a queue: several automated runs on the same repo at once risk stepping on each
other's branches/worktrees. The lock is PER FLOW (owner), default `task_resolver`:
the task_resolver daemon and queue-driven processing share one lock so that flow
handles ONE task at a time — while a human working manually is never blocked by it.
Intake (enrich + clarify) is cheap and safe to do for many tasks up front; only the
processing part is serialised.

Hand-off: `answer` folds the human's answers into a brief AND (source=atask, default)
comments that brief back onto the aTask task — the task becomes self-contained so it
can be reassigned to someone else with full context (`--no-sync` to skip the [WRITE]).

Item lifecycle — HAI LUỒNG TÁCH BIỆT (chuẩn bị ≠ thực thi):
    [LUỒNG CHUẨN BỊ — bổ sung thông tin + duyệt solution, có người, song song được]
    intake/add -> needs_clarification --answer--> ready --(plan+verify_gen+duyệt
                     \\--(clarify pass)---------> ready    after_plan)--approve--> approved
    [LUỒNG THỰC THI — chỉ code, tuần tự, không cần người]
    approved --next--> processing --done ok--> done
                                 \\--done fail--> failed --requeue--> approved
    `next` CHỈ nhặt `approved` — task chưa duyệt solution không bao giờ được thực thi.

Intake per task = context_pack (comments/checklist/subtasks) -> scout (candidate
files in clone_dir) -> feedback recall (past corrections) -> clarify (questions with
`proposed` answers). Artifacts land in temp/runs/, the item stores their paths, so
when the task's turn comes the pipeline starts fully grounded.

Storage: <work_dir>/queue/items/<qid>.json + <work_dir>/queue/lock_<owner>.json
(work_dir = $WORK_DIR or <repo>/work — same resolution as feedback.py). One file per
item; qid = "<source>-<task_id>" (natural dedupe).

Stdlib only. Output: one JSON object on stdout.

Usage:
    python task_queue.py intake --source atask --task 12345 --project atask --type bugfix \\
        [--backend claude] [--priority 1|2|3] [--post-questions]
    python task_queue.py scan [--take 3 --project atask] [--limit 20]
    python task_queue.py list [--state ready]
    python task_queue.py show <qid>
    python task_queue.py answer <qid> --answers-file ans.json | --accept-proposed [--no-sync]
    python task_queue.py next [--owner task_resolver]    # claim head of queue (flow lock)
    python task_queue.py done <qid> --result ok|fail [--note "..."] [--owner ...]
    python task_queue.py release [--owner ...]           # drop a stale lock (item back to ready)
    python task_queue.py requeue <qid> · remove <qid>
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS = os.path.dirname(os.path.dirname(_HERE))
_DEV_TOOLS = os.path.join(_SKILLS, "dev-automation", "tools")
_ATASK_TOOLS = os.path.join(_SKILLS, "atask-automation", "tools")
sys.path.insert(0, _HERE)
sys.path.insert(0, _DEV_TOOLS)

STATES = ("needs_clarification", "ready", "approved", "processing", "done", "failed")
OPEN_STATES = ("needs_clarification", "ready", "approved", "processing")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _repo_root():
    search = _HERE
    for _ in range(6):
        if os.path.isdir(os.path.join(search, ".git")) or \
           os.path.isfile(os.path.join(search, "CLAUDE.md")):
            return search
        search = os.path.dirname(search)
    return _HERE


def _work_dir():
    return os.environ.get("WORK_DIR") or os.path.join(_repo_root(), "work")


def _items_dir():
    d = os.path.join(_work_dir(), "queue", "items")
    os.makedirs(d, exist_ok=True)
    return d


DEFAULT_OWNER = "task_resolver"  # the automated flow that must stay serial


def _lock_path(owner=DEFAULT_OWNER):
    d = os.path.join(_work_dir(), "queue")
    os.makedirs(d, exist_ok=True)
    safe = "".join(c for c in str(owner) if c.isalnum() or c in "-_.") or DEFAULT_OWNER
    return os.path.join(d, f"lock_{safe}.json")


def _qid(source, task_id):
    raw = f"{source}-{task_id}"
    return "".join(c for c in raw if c.isalnum() or c in "-_.")


def _item_path(qid):
    return os.path.join(_items_dir(), f"{qid}.json")


def _load(qid):
    path = _item_path(qid)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(item):
    item["updated"] = _now()
    path = _item_path(item["qid"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(item, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def _all_items():
    out = []
    for fn in os.listdir(_items_dir()):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(_items_dir(), fn), encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _read_lock(owner=DEFAULT_OWNER):
    path = _lock_path(owner)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _all_locks():
    d = os.path.join(_work_dir(), "queue")
    out = {}
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if fn.startswith("lock_") and fn.endswith(".json"):
            owner = fn[len("lock_"):-len(".json")]
            lock = _read_lock(owner)
            if lock:
                out[owner] = lock
    return out


def try_claim(owner, qid):
    """Non-blocking claim of the flow lock. True iff acquired.

    Shared with task_resolver.py: the daemon claims this before handling a task, so
    the resolver flow and queue-driven `next` can never run two tasks at once — while
    other owners (e.g. a human's manual run) are unaffected.
    """
    try:
        with open(_lock_path(owner), "x", encoding="utf-8") as f:
            json.dump({"qid": qid, "pid": os.getpid(), "ts": _now(), "owner": owner}, f)
        return True
    except FileExistsError:
        return False


def release_claim(owner, qid=None):
    """Release the flow lock (only if held by `qid` when given). True iff removed."""
    lock = _read_lock(owner)
    if not lock:
        return False
    if qid and lock.get("qid") != qid:
        return False
    try:
        os.remove(_lock_path(owner))
        return True
    except OSError:
        return False


def _summary(item):
    return {k: item.get(k) for k in
            ("qid", "state", "priority", "title", "project", "task_type",
             "clarify_verdict", "blocking_count", "created")}


def _ns(**kw):
    return argparse.Namespace(**kw)


def _runs_dir():
    d = os.path.join(_repo_root(), "temp", "runs")
    os.makedirs(d, exist_ok=True)
    return d


# --- intake enrichment (each step is best-effort; failures land in notes) ----------

def atask_priority_to_queue(p):
    """aTask priority (str '1'..'4', 1=Khẩn cấp) -> queue priority (1..3, 1 chạy trước).
    Dùng bởi task_resolver --enqueue để xếp thứ tự batch."""
    return {"1": 1, "2": 1, "3": 2, "4": 3}.get(str(p or "").strip(), 2)


def _apply_thin_guard(item):
    """Enforce the intake rule: a thin description with NO extra context (no comments/
    checklist/subtasks) can never be `ready` — the clarify heuristic misses it because
    the pack's markdown chrome makes the desc look non-empty."""
    sig = item.get("signals") or {}
    if sig.get("thin_description") and not sig.get("has_extra_context") \
            and item["state"] == "ready":
        item["state"] = "needs_clarification"
        item["notes"].append(
            "forced needs_clarification: thin description, no comments/checklist/subtasks")


def _registry_clone_dir(project, notes):
    reg = os.path.join(_work_dir(), "projects.json")
    try:
        with open(reg, encoding="utf-8") as f:
            data = json.load(f)
        cd = (data.get(project) or {}).get("clone_dir")
        if cd and os.path.isdir(cd):
            return cd
        notes.append(f"registry: project '{project}' has no usable clone_dir -> scout skipped")
    except (OSError, json.JSONDecodeError) as e:
        notes.append(f"registry unreadable ({e}) -> scout skipped")
    return None


def _enrich(item, backend, model, notes):
    """context_pack -> scout -> recall -> clarify. Mutates item; never raises."""
    import context_pack
    import grounding
    import clarify
    qid = item["qid"]

    # 1. Context pack (task + comments + checklist + subtasks / AC fields).
    pack = context_pack.cmd_build(_ns(
        source=item["source"], task=item["task_id"], type=item.get("task_type"),
        out=os.path.join(_runs_dir(), f"{qid}_context.md"),
        no_comments=False, no_checklist=False, no_subtasks=False))
    if pack.get("error"):
        notes.append(f"context_pack: {pack.get('message')}")
        pack = {}
    else:
        item["artifacts"]["pack"] = pack["pack_path"]
        item["title"] = item.get("title") or pack.get("title") or ""
        item["ac_seeds"] = pack.get("ac_seeds") or []
        item["signals"] = pack.get("signals") or {}
        notes.extend(pack.get("notes") or [])

    # 2. Scout candidate files in the project's clone (needs the registry).
    clone_dir = _registry_clone_dir(item.get("project") or "", notes) \
        if item.get("project") else None
    if not item.get("project"):
        notes.append("no --project -> scout skipped (pass it to anchor the plan to code)")
    if clone_dir:
        kw = ",".join(pack.get("keywords") or []) or None
        scout = grounding.cmd_scout(_ns(
            run=qid, root=clone_dir, keywords=kw,
            desc=None if kw else item.get("title"),
            desc_file=None if kw else item["artifacts"].get("pack")))
        if scout.get("error"):
            notes.append(f"scout: {scout.get('message')}")
        else:
            item["artifacts"]["scout"] = scout["artifact"]
            item["scout_verdict"] = scout["verdict"]
            item["candidates"] = scout.get("candidates") or []

    # 3. Past corrections for this kind of task (feedback ledger).
    if item.get("project"):
        try:
            import feedback
            rec = feedback.cmd_recall(_ns(
                project=item["project"], stage="plan",
                task_type=item.get("task_type"), query=item.get("title") or "", limit=5))
            if rec.get("block"):
                cpath = os.path.join(_runs_dir(), f"{qid}_corrections.xml")
                with open(cpath, "w", encoding="utf-8") as f:
                    f.write(rec["block"])
                item["artifacts"]["corrections"] = cpath
        except (OSError, ValueError) as e:
            notes.append(f"recall: {e}")

    # 4. Clarify against the pack + scout context (agent backend recommended).
    cl = clarify.cmd_analyze(_ns(
        type=item.get("task_type"), title=item.get("title"),
        desc=None, desc_file=item["artifacts"].get("pack"),
        context_file=item["artifacts"].get("scout"),
        backend=backend, model=model, dry_run_text=None)) \
        if item["artifacts"].get("pack") else \
        clarify.cmd_analyze(_ns(
            type=item.get("task_type"), title=item.get("title"),
            desc=item.get("title") or "", desc_file=None, context_file=None,
            backend=backend, model=model, dry_run_text=None))
    item["clarify_verdict"] = cl["verdict"]
    item["blocking_count"] = cl["blocking_count"]
    item["questions"] = cl["questions"]
    if cl.get("note"):
        notes.append(f"clarify: {cl['note']}")
    item["state"] = "ready" if cl["verdict"] == "pass" else "needs_clarification"
    _apply_thin_guard(item)


def _post_questions(item, notes):
    """[WRITE] Comment the blocking questions (+proposed answers) onto the aTask task."""
    blocking = [q for q in item.get("questions", []) if q.get("blocking")]
    if not blocking or item["source"] != "atask":
        return
    lines = ["[auto-dev intake] Cần làm rõ trước khi code:"]
    for i, q in enumerate(blocking, 1):
        lines.append(f"{i}. {q['ask']}")
        if q.get("proposed"):
            lines.append(f"   → Đề xuất: {q['proposed']} (xác nhận hoặc sửa giúp)")
    body = "\n".join(lines)
    proc = subprocess.run(
        [sys.executable, os.path.join(_ATASK_TOOLS, "checklists.py"),
         "add-comment", str(item["task_id"]), body],
        cwd=_ATASK_TOOLS, capture_output=True, text=True, encoding="utf-8", timeout=60)
    if proc.returncode != 0:
        notes.append(f"post-questions failed: {(proc.stderr or '').strip()[:200]}")
    else:
        item["questions_posted"] = _now()


# --- commands ----------------------------------------------------------------------

def cmd_intake(args):
    qid = _qid(args.source, args.task)
    existing = _load(qid)
    if existing and existing["state"] in OPEN_STATES:
        return {"error": True,
                "message": f"{qid} already queued (state={existing['state']}); "
                           f"use requeue/remove first"}
    item = {
        "qid": qid, "source": args.source, "task_id": str(args.task),
        "project": args.project or "", "task_type": args.type or "",
        "priority": args.priority, "title": "", "state": "needs_clarification",
        "artifacts": {}, "notes": [], "created": _now(),
    }
    notes = item["notes"]
    _enrich(item, args.backend, args.model, notes)
    if args.post_questions:
        _post_questions(item, notes)
    _save(item)
    return {"ok": True, "item": item}


def cmd_add(args):
    """Enqueue WITHOUT enrichment (direct description / tests / manual entry)."""
    qid = _qid(args.source, args.task)
    existing = _load(qid)
    if existing and existing["state"] in OPEN_STATES:
        return {"error": True, "message": f"{qid} already queued (state={existing['state']})"}
    item = {
        "qid": qid, "source": args.source, "task_id": str(args.task),
        "project": args.project or "", "task_type": args.type or "",
        "priority": args.priority, "title": args.title or "",
        "state": "ready" if args.ready else "needs_clarification",
        "artifacts": {}, "notes": ["added without enrichment"], "created": _now(),
    }
    _save(item)
    return {"ok": True, "item": _summary(item)}


def cmd_scan(args):
    """List my open aTask tasks not yet queued; --take N runs intake on the first N."""
    proc = subprocess.run(
        [sys.executable, os.path.join(_ATASK_TOOLS, "search.py"), "my-tasks",
         "--format", "json"],
        cwd=_ATASK_TOOLS, capture_output=True, text=True, encoding="utf-8", timeout=120)
    out = (proc.stdout or "").strip()
    start = out.find("{")
    if start == -1:
        return {"error": True, "message": (proc.stderr or "no output").strip()[:300]}
    try:
        data = json.loads(out[start:])
    except json.JSONDecodeError as e:
        return {"error": True, "message": f"my-tasks returned non-JSON: {e}"}
    records = ((data.get("content") or {}).get("data")) or []
    known = {i["task_id"] for i in _all_items()}
    fresh = [{"task_id": r.get("id"), "title": r.get("name")}
             for r in records if r.get("id") and str(r.get("id")) not in known]
    fresh = fresh[:args.limit]

    taken = []
    for cand in fresh[:args.take] if args.take else []:
        res = cmd_intake(_ns(source="atask", task=cand["task_id"], project=args.project,
                             type=None, priority=2, backend=args.backend, model=None,
                             post_questions=False))
        taken.append({"task_id": cand["task_id"],
                      "state": (res.get("item") or {}).get("state"),
                      "error": res.get("message") if res.get("error") else None})
    return {"ok": True, "total_mine": len(records), "new_count": len(fresh),
            "new": fresh, "intaken": taken}


def cmd_list(args):
    items = _all_items()
    if args.state:
        items = [i for i in items if i["state"] == args.state]
    items.sort(key=lambda i: (i.get("priority", 2), i.get("created", "")))
    return {"ok": True, "count": len(items), "locks": _all_locks(),
            "items": [_summary(i) for i in items]}


def cmd_show(args):
    item = _load(args.qid)
    if not item:
        return {"error": True, "message": f"no item {args.qid}"}
    return {"ok": True, "item": item}


def _sync_brief_to_atask(item, brief_text, notes):
    """[WRITE] Comment the clarified brief onto the aTask task so it is self-contained
    and can be handed off to another person with full context."""
    if item["source"] != "atask" or not (brief_text or "").strip():
        return
    body = ("[auto-dev intake] Yêu cầu đã làm rõ (đủ ngữ cảnh để bàn giao):\n\n"
            + brief_text.strip())[:4000]
    proc = subprocess.run(
        [sys.executable, os.path.join(_ATASK_TOOLS, "checklists.py"),
         "add-comment", str(item["task_id"]), body],
        cwd=_ATASK_TOOLS, capture_output=True, text=True, encoding="utf-8", timeout=60)
    if proc.returncode != 0:
        notes.append(f"sync brief -> aTask failed: {(proc.stderr or '').strip()[:200]}")
    else:
        item["brief_synced"] = _now()


_RC_TOOLS = os.path.join(_SKILLS, "remote-control", "tools")


def _send_tg(text):
    """Send an HTML message to the first allowed Telegram chat. Test seam.
    Returns (ok, detail)."""
    sys.path.insert(0, _RC_TOOLS)
    try:
        import tg_api
        bot = tg_api.approval_bot()
        chats = tg_api.allowed_chats(bot)
        if not chats:
            return False, "TELEGRAM_ALLOWED_CHATS trống"
        r = tg_api.send_message(chats[0], text, bot=bot)
        return bool(r.get("ok")), r.get("description") or ""
    except Exception as e:  # noqa: BLE001 — Telegram down must not crash the queue
        return False, str(e)


def _questions_tg_text(item):
    """Numbered free-reply form — the human answers by COMMENT, not buttons."""
    import html as _html
    qid = item["qid"]
    lines = [f"📋 <b>Làm rõ yêu cầu — {_html.escape(item.get('title') or qid)}</b>",
             f"<code>{qid}</code>", ""]
    for i, q in enumerate(item.get("questions") or [], 1):
        mark = "❗" if q.get("blocking") else "▫️"
        lines.append(f"{i}. {mark} {_html.escape(q.get('ask') or '')}")
        prop = q.get("proposed") or q.get("assumption") or ""
        if prop:
            lines.append(f"   → <i>Đề xuất: {_html.escape(prop)}</i>")
    lines += ["",
              "✍️ Trả lời bằng MỘT tin nhắn, mỗi mục một ý:",
              f"<code>{qid} 1: ok; 2: dùng 409 thay vì 400; 3: chỉ parent</code>",
              "(mục bỏ qua hoặc 'ok/đồng ý' = chấp nhận đề xuất; ❗ = câu chặn)"]
    return "\n".join(lines)


# Ranh giới mục: đầu chuỗi, ';', ',', xuống dòng, hoặc '. ' — người dùng viết tự nhiên
# đủ kiểu ("1: ok; 2: ...", "1: ok, 2: ...", "...xóa. 5: subtask...").
_REPLY_SEG = re.compile(r"(?:^|[;,\n]|\.\s)\s*(\d{1,2})\s*[:.)]\s*")
_ACCEPT_WORDS = {"ok", "oke", "okay", "y", "yes", "đồng ý", "dong y", "agree", "chốt", "chot"}


def _parse_reply(text, questions):
    """'1: ok; 2: dùng 409; 3: chỉ parent' -> answers list khớp thứ tự câu hỏi.

    Mục bỏ qua / 'ok' = chấp nhận proposed (thành assumption trong brief);
    text tự do = câu trả lời CHỐT của người dùng.
    """
    text = (text or "").strip()
    # bỏ tiền tố qid nếu người dùng dán lại
    text = re.sub(r"^\S*-\S+\s+", "", text) if re.match(r"^\S+-\S+\s+\d", text) else text
    found = {}
    matches = list(_REPLY_SEG.finditer(text))
    for j, m in enumerate(matches):
        end = matches[j + 1].start() if j + 1 < len(matches) else len(text)
        found[int(m.group(1))] = text[m.end():end].strip().strip(";")
    answers = []
    for i, q in enumerate(questions or [], 1):
        raw = (found.get(i) or "").strip()
        accepted = not raw or raw.lower() in _ACCEPT_WORDS
        answers.append({
            "ask": q.get("ask") or "",
            "answer": "" if accepted else raw,
            "proposed": q.get("proposed") or q.get("assumption") or "",
        })
    return answers, sorted(found)


def _atask_tool(script, cli_args, timeout=60):
    """Run an atask CLI, parse its JSON (tolerates warn-preamble). Test seam."""
    proc = subprocess.run([sys.executable, os.path.join(_ATASK_TOOLS, script), *cli_args],
                          cwd=_ATASK_TOOLS, capture_output=True, text=True,
                          encoding="utf-8", timeout=timeout)
    out = (proc.stdout or "").strip()
    start = out.find("{")
    if start == -1:
        return {"error": True, "message": (proc.stderr or out or "no output")[-300:]}
    try:
        return json.loads(out[start:])
    except json.JSONDecodeError:
        return {"error": True, "message": f"non-JSON from {script}"}


def _envelope_rows(res):
    content = (res or {}).get("content")
    if isinstance(content, dict):
        data = content.get("data")
        return data if isinstance(data, list) else [content]
    return content if isinstance(content, list) else []


def cmd_ask_tg(args):
    """Gửi các câu hỏi làm rõ qua Telegram — dạng mục đánh số để NGƯỜI TRẢ LỜI TỰ DO
    bằng comment (không phải nút chọn). Nhận phản hồi bằng `reply <qid> --text "..."`."""
    item = _load(args.qid)
    if not item:
        return {"error": True, "message": f"no item {args.qid}"}
    if not item.get("questions"):
        return {"error": True, "message": "item không có câu hỏi clarify nào"}
    ok, detail = _send_tg(_questions_tg_text(item))
    if not ok:
        return {"error": True, "message": f"gửi Telegram thất bại: {detail}"}
    item["questions_sent_tg"] = _now()
    _save(item)
    return {"ok": True, "sent": len(item["questions"]),
            "next": f"nhận phản hồi -> task_queue.py reply {args.qid} --text \"<tin nhắn>\""}


def cmd_reply(args):
    """Parse comment tự do ('1: ok; 2: dùng 409...') -> chốt answers -> brief + sync aTask."""
    item = _load(args.qid)
    if not item:
        return {"error": True, "message": f"no item {args.qid}"}
    if not item.get("questions"):
        return {"error": True, "message": "item không có câu hỏi để trả lời"}
    answers, matched = _parse_reply(args.text, item["questions"])
    apath = os.path.join(_runs_dir(), f"{item['qid']}_answers.json")
    with open(apath, "w", encoding="utf-8") as f:
        json.dump(answers, f, ensure_ascii=False, indent=2)
    out = cmd_answer(argparse.Namespace(qid=args.qid, answers_file=apath,
                                        accept_proposed=False,
                                        no_sync=getattr(args, "no_sync", False)))
    if isinstance(out, dict) and not out.get("error"):
        out["items_answered"] = matched
        out["items_accepted_proposed"] = [i + 1 for i in range(len(answers))
                                          if (i + 1) not in matched]
        confirm = _send_tg(f"✅ Đã chốt yêu cầu cho <b>{item.get('title') or args.qid}</b> — "
                           f"{len(matched)} mục bạn sửa, còn lại theo đề xuất. "
                           f"Task chuyển <code>ready</code>, vào hàng đợi xử lý.")
        out["tg_confirmed"] = confirm[0]
    return out


def cmd_mark(args):
    """[WRITE] Chuyển TRẠNG THÁI task trên aTask theo statusType (team thấy được).

    aTask `update_task(status=...)` nhận status-ID theo-từng-list, không nhận keyword —
    nên tra ID bằng cách quét task trong CÙNG list có statusType đích (đúng cách
    task_resolver làm; ES trả task chéo list nên phải check listTaskId khớp).
    Điểm gọi trong luồng: `next` xong -> mark processing ("Đang làm");
    MR tạo xong -> mark theo workflow của bạn (vd approved/"Chờ phê duyệt").
    """
    item = _load(args.qid)
    if not item:
        return {"error": True, "message": f"no item {args.qid}"}
    if item["source"] != "atask":
        return {"error": True, "message": "mark chỉ áp dụng cho task aTask"}
    task = _envelope_rows(_atask_tool("tasks.py", ["get", item["task_id"], "--format", "json"]))
    task = task[0] if task else {}
    list_id = task.get("listTaskId")
    if not list_id:
        return {"error": True, "message": "không đọc được listTaskId của task"}
    current = (task.get("statusType") or "").lower()
    if current == args.to.lower():
        return {"ok": True, "unchanged": True, "statusType": current}
    rows = _envelope_rows(_atask_tool("search.py", ["tasks", "--list", list_id,
                                                    "--format", "json"]))
    status_id = next((x.get("status") for x in rows
                      if x.get("listTaskId") == list_id and x.get("status")
                      and (x.get("statusType") or "").lower() == args.to.lower()), None)
    if not status_id:
        return {"error": True,
                "message": f"list này không có cột statusType='{args.to}' (hoặc chưa có "
                           f"task nào ở cột đó để tra ID) — đổi tay trên aTask 1 lần"}
    upd = _atask_tool("tasks.py", ["update", item["task_id"], "--status", str(status_id)])
    if upd.get("error") or upd.get("success") is False:
        return {"error": True, "message": f"update status failed: {upd.get('message')}"}
    if args.comment:
        _atask_tool("checklists.py", ["add-comment", item["task_id"], args.comment])
    item["notes"].append(f"atask status: {current or '?'} -> {args.to} (id={status_id})")
    item["atask_status"] = args.to
    _save(item)
    return {"ok": True, "from": current, "to": args.to, "status_id": status_id,
            "commented": bool(args.comment)}


def cmd_answer(args):
    item = _load(args.qid)
    if not item:
        return {"error": True, "message": f"no item {args.qid}"}
    if item["state"] not in ("needs_clarification", "ready"):
        return {"error": True, "message": f"cannot answer in state {item['state']}"}

    import clarify
    answers_file = args.answers_file
    tmp_answers = None
    if args.accept_proposed:
        # One-click confirm: every stored question's `proposed` becomes the assumption.
        tmp_answers = os.path.join(_runs_dir(), f"{item['qid']}_answers.json")
        with open(tmp_answers, "w", encoding="utf-8") as f:
            json.dump([{"ask": q["ask"], "answer": "",
                        "proposed": q.get("proposed") or q.get("assumption") or ""}
                       for q in item.get("questions", [])], f, ensure_ascii=False)
        answers_file = tmp_answers
    if not answers_file:
        return {"error": True, "message": "pass --answers-file or --accept-proposed"}

    brief = clarify.cmd_brief(_ns(
        title=item.get("title"), desc=None,
        desc_file=item["artifacts"].get("pack"),
        answers_file=answers_file,
        out=os.path.join(_runs_dir(), f"{item['qid']}_brief.md")))
    item["artifacts"]["brief"] = brief.get("brief_path")
    item["ac_seeds"] = list(dict.fromkeys(
        (item.get("ac_seeds") or []) + (brief.get("acceptance_seeds") or [])))
    item["clarify_verdict"] = "pass"
    item["state"] = "ready"
    item["notes"].append(
        f"answered: {brief.get('resolved_count', 0)} resolved, "
        f"{brief.get('assumed_count', 0)} assumed"
        + (" (accept-proposed)" if args.accept_proposed else ""))
    if not getattr(args, "no_sync", False):
        _sync_brief_to_atask(item, brief.get("brief") or "", item["notes"])
    _save(item)
    return {"ok": True, "item": _summary(item), "brief_path": brief.get("brief_path"),
            "brief_synced": item.get("brief_synced")}


def cmd_approve(args):
    """Luồng CHUẨN BỊ kết thúc ở đây: solution (plan + kịch bản verify) đã được người
    duyệt -> item sang `approved`, đủ điều kiện cho luồng THỰC THI nhặt."""
    item = _load(args.qid)
    if not item:
        return {"error": True, "message": f"no item {args.qid}"}
    if item["state"] != "ready":
        return {"error": True,
                "message": f"chỉ approve được từ 'ready' (đang: {item['state']}) — "
                           f"needs_clarification thì answer trước"}
    if args.plan:
        item["artifacts"]["plan"] = args.plan
    if args.verify:
        item["artifacts"]["verify"] = args.verify
    item["state"] = "approved"
    item["notes"].append("solution approved" + (f": {args.note}" if args.note else ""))
    _save(item)
    return {"ok": True, "item": _summary(item)}


def cmd_next(args):
    owner = getattr(args, "owner", None) or DEFAULT_OWNER
    lock = _read_lock(owner)
    if lock:
        return {"error": True, "locked_by": lock,
                "message": f"flow '{owner}' busy: {lock.get('qid')} is processing since "
                           f"{lock.get('ts')} — finish it (`done`) or `release` first"}
    # Luồng thực thi CHỈ nhặt task đã duyệt solution — ready/needs_clarification
    # thuộc luồng chuẩn bị, không bao giờ được code khi chưa chốt.
    ready = [i for i in _all_items() if i["state"] == "approved"]
    if not ready:
        return {"ok": True, "message": "queue empty (no APPROVED items — task ready thì "
                                       "chạy luồng chuẩn bị /atask-prep để duyệt solution)",
                "item": None}
    ready.sort(key=lambda i: (i.get("priority", 2), i.get("created", "")))
    item = ready[0]
    item["state"] = "processing"
    item["owner"] = owner
    item["started"] = _now()
    _save(item)
    if not try_claim(owner, item["qid"]):
        item["state"] = "ready"  # lost the race to another `next`
        _save(item)
        return {"error": True, "message": "lost lock race; retry `next`"}
    return {
        "ok": True,
        "item": item,
        "run_id": f"{item.get('project') or item['source']}-{item['task_id']}",
        "pipeline_hint": (
            "confirm với người dùng rồi `mark <qid> --to processing` (báo team task đang làm); "
            "run auto-dev from here: run_log.py init with --tier/--mode from triage; "
            "use artifacts.brief (or .pack) as --desc-file, artifacts.corrections for "
            "debate --corrections-file, ac_seeds -> run_log ac-add; "
            "MR xong: `mark <qid> --to approved --comment '<mr_url>'` theo workflow; "
            "when finished: task_queue.py done <qid> --result ok|fail"),
    }


def cmd_done(args):
    item = _load(args.qid)
    if not item:
        return {"error": True, "message": f"no item {args.qid}"}
    if item["state"] != "processing":
        return {"error": True, "message": f"{args.qid} is not processing (state={item['state']})"}
    item["state"] = "done" if args.result == "ok" else "failed"
    item["finished"] = _now()
    if args.note:
        item["notes"].append(f"done({args.result}): {args.note}")
    _save(item)
    release_claim(item.get("owner") or getattr(args, "owner", None) or DEFAULT_OWNER,
                  qid=args.qid)
    return {"ok": True, "item": _summary(item)}


def cmd_release(args):
    owner = getattr(args, "owner", None) or DEFAULT_OWNER
    lock = _read_lock(owner)
    if not lock:
        return {"ok": True, "message": f"no lock held for flow '{owner}'"}
    item = _load(lock.get("qid") or "")
    if item and item["state"] == "processing":
        item["state"] = "ready"
        item["notes"].append(f"lock released (was processing since {lock.get('ts')})")
        _save(item)
    if not release_claim(owner):
        return {"error": True, "message": "cannot remove lock"}
    return {"ok": True, "released": lock}


def cmd_requeue(args):
    item = _load(args.qid)
    if not item:
        return {"error": True, "message": f"no item {args.qid}"}
    if item["state"] not in ("failed", "done"):
        return {"error": True, "message": f"requeue only from failed/done (state={item['state']})"}
    # đã có plan duyệt -> quay lại thẳng hàng thực thi; chưa -> quay lại luồng chuẩn bị
    if item.get("artifacts", {}).get("plan") or "solution approved" in " ".join(item.get("notes", [])):
        item["state"] = "approved"
    else:
        item["state"] = "ready" if item.get("clarify_verdict") == "pass" else "needs_clarification"
    item["notes"].append("requeued")
    _save(item)
    return {"ok": True, "item": _summary(item)}


def cmd_remove(args):
    item = _load(args.qid)
    if not item:
        return {"error": True, "message": f"no item {args.qid}"}
    os.remove(_item_path(args.qid))
    return {"ok": True, "removed": args.qid}


def main():
    ap = argparse.ArgumentParser(
        description="Serial task queue: intake + clarify aTask/Azure tasks, process one at a time.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("intake", help="enrich (pack+scout+recall+clarify) then enqueue")
    i.add_argument("--source", required=True, choices=["atask", "azure"])
    i.add_argument("--task", required=True)
    i.add_argument("--project", default=None, help="registry name (enables scout+recall)")
    i.add_argument("--type", default=None, help="bugfix|feature|...")
    i.add_argument("--priority", type=int, default=2, choices=[1, 2, 3], help="1=cao nhất")
    i.add_argument("--backend", default=None, help="clarify qua agent (claude|cursor); bỏ = heuristic")
    i.add_argument("--model", default=None)
    i.add_argument("--post-questions", action="store_true",
                   help="[WRITE] comment câu hỏi blocking + proposed lên task aTask")

    a = sub.add_parser("add", help="enqueue without enrichment (manual/direct request)")
    a.add_argument("--source", default="manual")
    a.add_argument("--task", required=True)
    a.add_argument("--project", default=None)
    a.add_argument("--type", default=None)
    a.add_argument("--title", default=None)
    a.add_argument("--priority", type=int, default=2, choices=[1, 2, 3])
    a.add_argument("--ready", action="store_true", help="skip clarification state")

    sc = sub.add_parser("scan", help="find my aTask tasks not yet queued; --take N intakes them")
    sc.add_argument("--limit", type=int, default=20)
    sc.add_argument("--take", type=int, default=0)
    sc.add_argument("--project", default=None)
    sc.add_argument("--backend", default=None)

    l = sub.add_parser("list", help="queue overview (sorted by priority, then age)")
    l.add_argument("--state", default=None, choices=list(STATES))

    sh = sub.add_parser("show", help="full item detail (questions, artifacts)")
    sh.add_argument("qid")

    an = sub.add_parser("answer", help="fold answers -> brief (ready) + sync brief lên aTask")
    an.add_argument("qid")
    an.add_argument("--answers-file", default=None, help="JSON list/map (clarify format)")
    an.add_argument("--accept-proposed", action="store_true",
                    help="chấp nhận toàn bộ câu trả lời đề xuất (one-click)")
    an.add_argument("--no-sync", action="store_true",
                    help="KHÔNG comment brief lên task aTask (mặc định có — [WRITE], team thấy)")

    at = sub.add_parser("ask-tg", help="gửi câu hỏi làm rõ qua Telegram (trả lời tự do bằng comment)")
    at.add_argument("qid")

    rp = sub.add_parser("reply", help="nạp comment trả lời ('1: ...; 2: ok') -> brief + ready")
    rp.add_argument("qid")
    rp.add_argument("--text", required=True, help="nguyên văn tin nhắn trả lời của người dùng")
    rp.add_argument("--no-sync", action="store_true", help="không comment brief lên aTask")

    mk = sub.add_parser("mark", help="[WRITE] chuyển trạng thái task aTask theo statusType")
    mk.add_argument("qid")
    mk.add_argument("--to", required=True,
                    help="statusType đích: todo|processing|approved|completed|closed|custom")
    mk.add_argument("--comment", default=None, help="comment kèm theo (vd link MR)")

    av = sub.add_parser("approve", help="solution đã duyệt -> item vào hàng THỰC THI")
    av.add_argument("qid")
    av.add_argument("--plan", default=None, help="path plan.xml đã duyệt")
    av.add_argument("--verify", default=None, help="path <RID>_verify.json đã duyệt")
    av.add_argument("--note", default=None)

    n = sub.add_parser("next", help="claim task ĐÃ DUYỆT đầu hàng (flow lock, serial per owner)")
    n.add_argument("--owner", default=DEFAULT_OWNER,
                   help=f"luồng xử lý giữ lock (mặc định {DEFAULT_OWNER})")

    d = sub.add_parser("done", help="finish the processing item, release the flow lock")
    d.add_argument("qid")
    d.add_argument("--result", required=True, choices=["ok", "fail"])
    d.add_argument("--note", default=None)
    d.add_argument("--owner", default=None, help="mặc định: owner ghi trên item")

    rl = sub.add_parser("release", help="force-drop a stale flow lock (item goes back to ready)")
    rl.add_argument("--owner", default=DEFAULT_OWNER)

    rq = sub.add_parser("requeue", help="failed/done -> back into the queue")
    rq.add_argument("qid")

    rm = sub.add_parser("remove", help="delete an item [confirm with the user first]")
    rm.add_argument("qid")

    args = ap.parse_args()
    dispatch = {"intake": cmd_intake, "add": cmd_add, "scan": cmd_scan, "list": cmd_list,
                "show": cmd_show, "answer": cmd_answer, "ask-tg": cmd_ask_tg,
                "reply": cmd_reply, "approve": cmd_approve, "mark": cmd_mark,
                "next": cmd_next, "done": cmd_done, "release": cmd_release,
                "requeue": cmd_requeue, "remove": cmd_remove}
    try:
        out = dispatch[args.cmd](args)
    except (OSError, ValueError) as e:
        out = {"error": True, "message": str(e)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if isinstance(out, dict) and out.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
