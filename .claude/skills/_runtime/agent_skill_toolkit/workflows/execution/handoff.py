"""handoff.md — what the next worker (any account / backend) needs to continue a task.

Lives OUTSIDE the product repo (state_dir/handoff/<task_id>.md) so it is never committed into
an MR (design §5.3, §18 #4). The store keeps the structured truth; this is the readable brief.
"""

import os
import re

from ...core import paths

SECTIONS = ("Mục tiêu", "Đã xong", "Đang dở", "Bước tiếp", "Quyết định", "File liên quan")


def path(task_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", task_id or ""):
        raise ValueError(f"bad task id: {task_id!r}")
    return paths.state_dir("handoff", f"{task_id}.md")


def write(task_id: str, sections: dict) -> str:
    """Write the brief; known sections first in a fixed order, extra keys after."""
    order = [s for s in SECTIONS if s in sections] + [k for k in sections if k not in SECTIONS]
    body = [f"# Handoff {task_id}", ""]
    for k in order:
        v = sections[k]
        if isinstance(v, (list, tuple)):
            v = "\n".join(f"- {x}" for x in v)
        body += [f"## {k}", str(v).strip() or "—", ""]
    p = path(task_id)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(body))
    os.replace(tmp, p)
    return p


def read(task_id: str) -> dict:
    try:
        with open(path(task_id), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}
    out, cur = {}, None
    for ln in text.splitlines():
        if ln.startswith("## "):
            cur = ln[3:].strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(ln)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def resume_preamble(task_id: str, last_step: str = "", extra: str = "") -> str:
    """Prompt prefix for a worker that continues someone else's task."""
    h = read(task_id)
    lines = [f"Bạn TIẾP TỤC task {task_id} (một worker trước đã dừng — hết quota, crash hoặc đổi agent).",
             "Không làm lại bước đã xong. Đọc bàn giao dưới đây và git log của nhánh trước khi sửa gì."]
    if last_step:
        lines.append(f"Checkpoint cuối: bước `{last_step}`.")
    for k, v in h.items():
        lines += [f"\n## {k}", v]
    if extra:
        lines += ["", extra]
    return "\n".join(lines)
