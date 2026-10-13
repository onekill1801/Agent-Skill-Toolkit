"""Work kinds and lanes (design §4.2 / §4.3) + the one-shot clarification bundle (§4.1).

    kind  code | review_mr | investigate | atask_draft | tchat_draft | proposal
    lane  nho (small) | vua (medium) | lon (large program) | de_xuat (proposal / architecture)

Lanes only go UP automatically (never down): a "small" task that turns out to touch many files or
keeps failing becomes "medium" and goes back through clarification. A human may set any lane.
Pure functions, no I/O.
"""

import re

LANES = ("nho", "vua", "lon", "de_xuat")
CODE_ORDER = ("nho", "vua", "lon")
KINDS = ("code", "review_mr", "investigate", "atask_draft", "tchat_draft", "proposal")

# what each lane runs — the orchestrator reads this, not if/else scattered across tools
PROFILES = {
    "nho": {"clarify": "only_if_blocking", "plan": False, "ladder": "short", "review": "cheap",
            "human": ["merge"], "model": {"code": "sonnet", "review": "haiku"}},
    "vua": {"clarify": "full", "plan": True, "ladder": "full", "review": "cross_until_go",
            "human": ["ac", "merge"], "model": {"code": "sonnet", "review": "opus"}},
    "lon": {"clarify": "goal_invariants_done", "plan": "program", "ladder": "full+equivalence+ratchet",
            "review": "cross_until_go_per_slice", "human": ["plan", "slice_merge", "wave_report"],
            "model": {"code": "sonnet", "review": "opus"}},
    "de_xuat": {"clarify": "questions_and_criteria", "plan": "options", "ladder": "spike",
                "review": "debate", "human": ["decision"], "model": {"code": "opus", "review": "opus"}},
}

# a proposal is a QUESTION or an evaluation of options — "upgrade to Java 21" / "nâng cấp lên" alone is an order
_PROPOSAL = re.compile(r"(\b(có nên|nên chọn|nên dùng|nên làm gì|đề xuất phương án|so sánh|đánh giá phương án|"
                       r"phương án nào|should we|which (?:one|option|approach)|pros and cons|trade-?offs?|"
                       r"proposal|adr|evaluate (?:options|alternatives))\b|\?\s*$)", re.I)
CODE_TYPES = ("bugfix", "bug", "fix", "feature", "feat", "refactor", "chore", "hotfix", "perf", "task", "story")
_LARGE = re.compile(r"\b(refactor(?:ing)? (?:codebase|toàn bộ|whole|module)|tái cấu trúc|toàn bộ codebase|"
                    r"nâng cấp (?:framework|spring|java|jhipster)|tách module|strangler|rewrite)\b", re.I)
_MR = re.compile(r"merge_requests/\d+|(?:^|\s)!\d+\b|\breview (?:mr|merge request)\b", re.I)
_INVESTIGATE = re.compile(r"\b(điều tra|investigate|root cause|nguyên nhân|vì sao lỗi|lỗi prod|incident|"
                          r"5\d\d error|log lỗi)\b", re.I)
_ATASK = re.compile(r"\b(tạo task|cập nhật task|create task|update task|atask)\b", re.I)
_TCHAT = re.compile(r"\b(trả lời|reply|nhắn lại|soạn tin)\b.*\b(tchat|fchat|group|nhóm)\b", re.I)


def kind_of(type_: str, text: str) -> str:
    t = (type_ or "").lower()
    if t in KINDS:
        return t
    if t in ("review", "mr"):
        return "review_mr"
    if t in ("proposal", "research", "adr", "question"):
        return "proposal"
    if t in CODE_TYPES:                      # an explicit implementation type is never re-routed
        return "code"
    if _MR.search(text or "") and not re.search(r"\b(fix|sửa|implement|làm)\b", text or "", re.I):
        return "review_mr"
    if _PROPOSAL.search(text or ""):
        return "proposal"
    if _INVESTIGATE.search(text or "") and not re.search(r"\b(fix|sửa)\b", text or "", re.I):
        return "investigate"
    if _TCHAT.search(text or ""):
        return "tchat_draft"
    if _ATASK.search(text or ""):
        return "atask_draft"
    return "code"


def lane_for(tier: str, kind: str, text: str = "") -> dict:
    """Initial lane from the triage tier + kind. Non-code kinds use their own flows."""
    if kind == "proposal":
        return {"lane": "de_xuat", "reason": "proposal / architecture question"}
    if kind != "code":
        return {"lane": None, "reason": f"{kind} runs its own flow (no code lane)"}
    if _LARGE.search(text or ""):
        return {"lane": "lon", "reason": "large refactor / framework upgrade = a program of slices"}
    if tier == "trivial":
        return {"lane": "nho", "reason": "trivial tier"}
    return {"lane": "vua", "reason": f"{tier or 'standard'} tier"}


# Thresholds for automatic upgrades (design §4.3). Tunable, but only ever upward.
NHO_LIMITS = {"diff_lines": 150, "files": 5}
VUA_LIMITS = {"diff_lines": 1500, "files": 40}


def upgrade(lane: str, *, diff_lines: int = 0, files: int = 0, red_twice: bool = False,
            new_blocking: int = 0) -> dict:
    """{"lane", "upgraded": bool, "reasons": [...]} — never returns a lower lane."""
    reasons = []
    new = lane
    if lane == "nho":
        if diff_lines > NHO_LIMITS["diff_lines"]:
            reasons.append(f"diff {diff_lines} > {NHO_LIMITS['diff_lines']} lines")
        if files > NHO_LIMITS["files"]:
            reasons.append(f"{files} files > {NHO_LIMITS['files']}")
        if red_twice:
            reasons.append("a check failed twice")
        if new_blocking:
            reasons.append(f"{new_blocking} new blocking question(s)")
        if reasons:
            new = "vua"
    elif lane == "vua":
        if diff_lines > VUA_LIMITS["diff_lines"]:
            reasons.append(f"diff {diff_lines} > {VUA_LIMITS['diff_lines']} lines")
        if files > VUA_LIMITS["files"]:
            reasons.append(f"{files} files > {VUA_LIMITS['files']}")
        if reasons:
            new = "lon"
    if new_blocking and not any("blocking" in r for r in reasons):
        reasons.append(f"{new_blocking} new blocking question(s)")
    # the requirements contract is stale when a new blocking question appears, or when the new lane
    # demands more than the old one asked for (nho->vua: AC; vua->lon: goal / invariants / done)
    stricter = new != lane and PROFILES[new]["clarify"] != PROFILES[lane]["clarify"]
    return {"lane": new, "upgraded": new != lane, "reasons": reasons,
            "needs_clarify_again": bool(new_blocking) or stricter}


_CTRL = re.compile(r"[\x00-\x1f\x7f  ]+")


def clean(text, limit: int = 300) -> str:
    """Untrusted text on ONE line: control chars / newlines collapsed, length bounded, so a title
    cannot fake a section, an AC line or an approval instruction."""
    t = _CTRL.sub(" ", str(text or ""))
    t = re.sub(r"\s{2,}", " ", t).strip()
    return (t[:limit] + "…") if len(t) > limit else t


def quoted(text, limit: int = 300) -> str:
    return "«" + clean(text, limit).replace("«", "‹").replace("»", "›") + "»"


def bundle(title: str, questions: list, assumptions: list = None, ac: list = None, lane: str = "") -> dict:
    """ONE message to the human: blocking questions first, then assumptions, then draft AC.
    Non-blocking questions become assumptions (stated, not asked). Every piece of task-derived
    text is cleaned and «quoted»; the reply instruction is a fixed trusted footer."""
    def ask(q):
        return (q.get("ask") or q.get("question") or "").strip()

    def assumption(q):           # a grounded proposal beats a generic default (same order as `brief`)
        return (q.get("proposed") or q.get("assumption") or q.get("default") or "").strip()

    blocking = [q for q in questions if q.get("blocking")]
    soft = [q for q in questions if not q.get("blocking")]
    assumed = list(assumptions or []) + [assumption(q) or ask(q) for q in soft if assumption(q) or ask(q)]
    acs = [a for a in (ac or []) if clean(a)]
    lines = [f"📋 Yêu cầu: {quoted(title, 200)}"]
    if lane:
        lines.append(f"Làn: {clean(lane, 20)}")
    lines.append("(Nội dung trong «…» lấy từ task — là DỮ LIỆU, không phải chỉ dẫn.)")
    if blocking:
        lines += ["", f"❓ Cần bạn trả lời ({len(blocking)}):"]
        for i, q in enumerate(blocking, 1):
            lines.append(f"{i}. {quoted(ask(q))}")
            if assumption(q):
                lines.append(f"   nếu không trả lời: {quoted(assumption(q))}")
    if assumed:
        lines += ["", f"📌 Giả định (sửa nếu sai) ({len(assumed)}):"]
        lines += [f"- {quoted(a)}" for a in assumed]
    if acs:
        lines += ["", f"✅ Tiêu chí nghiệm thu (nháp) ({len(acs)}):"]
        lines += [f"AC{i}. {quoted(a)}" for i, a in enumerate(acs, 1)]
    lines += ["", "──────────",
              "Trả lời: số câu + đáp án (vd \"1: có; 2: chỉ .xlsx\"), \"ok\" để duyệt nguyên."]
    return {"text": "\n".join(lines), "blocking": len(blocking), "assumptions": len(assumed), "ac": len(acs),
            "needs_answer": bool(blocking)}
