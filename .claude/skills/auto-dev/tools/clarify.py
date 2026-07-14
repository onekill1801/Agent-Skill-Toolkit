#!/usr/bin/env python3
"""Intake clarify — đào sâu yêu cầu mơ hồ TRƯỚC khi Plan/debate.

Hầu hết yêu cầu vào pipeline đều mơ hồ (thiếu scope, input/output, edge case, tiêu chí
nghiệm thu). Debate dù tốt mấy mà input mơ hồ thì vẫn lên plan cho sai thứ. Bước này chạy ở
Intake, ngay sau khi đọc task và trước triage/AC/debate:

    analyze  -> phát hiện điểm mơ hồ, sinh CÂU HỎI làm rõ (phân nhóm) + GIẢ ĐỊNH đề xuất,
                kèm verdict {pass | needs_clarification}. Mỗi câu gắn cờ blocking:
                  blocking=true  -> không trả lời thì gần như chắc code sai (scope / I-O /
                                    định-nghĩa-done) -> HỎI người.
                  blocking=false -> có thể chạy với giả định mặc định -> ghi vào brief để
                                    người liếc lại, KHÔNG bắt buộc hỏi.
    brief    -> gấp câu trả lời (+ giả định) thành một bản yêu cầu đã sắc (Markdown) cho
                người duyệt và làm `--desc` cho debate; verdict=pass.

GATE (evidence-gated, đồng bộ run_log.policy): `analyze` trả verdict=needs_clarification khi
còn câu blocking. Orchestrator hỏi người -> `brief` -> record-gate clarity pass trên stage plan.
Auto mode: clarity fail CHẶN debate. Checkpoint mode: chỉ INFORM người duyệt.

Heuristic-first (không tốn token). `--backend` escalate cho agent (câu hỏi sát task hơn); mọi
lỗi agent/parse -> fallback heuristic, clarify không bao giờ làm sập pipeline.

Stdlib only. Output: một JSON object trên stdout.

Usage:
    python clarify.py analyze --type feature --title "Export báo cáo" --desc "..."
    python clarify.py analyze --type bugfix --desc-file task.txt --backend claude
    python clarify.py brief --desc-file task.txt --answers-file ans.json --out ../../../temp/runs/123_brief.md
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(_SKILLS, "fork-terminal", "tools"))
sys.path.insert(0, _HERE)
import agent_parser  # noqa: E402
import agent_runner  # noqa: E402

# --- Tín hiệu mơ hồ theo nhóm (EN + VI). Mỗi rule: nếu khớp -> sinh câu hỏi nhóm đó. -----------

# Từ "kêu to nhưng rỗng nghĩa" về phi chức năng (cần con số mục tiêu).
VAGUE_NONFUNC = [
    "improve", "optimize", "optimise", "faster", "fast", "performance", "scalable",
    "efficient", "better", "robust", "smooth",
    "cải thiện", "tối ưu", "nhanh hơn", "nhanh", "hiệu năng", "mượt", "ổn định hơn", "tốt hơn",
]
# Tín hiệu scope co giãn / liệt kê không trọn.
VAGUE_SCOPE = [
    "etc", "and so on", "similar", "various", "some", "a few", "stuff", "things",
    "v.v.", "vv", "tương tự", "một số", "vài", "các thứ", "linh hoạt", "đại loại",
]
# Có nhắc giao diện dữ liệu nhưng dễ thiếu đặc tả input/output.
IO_MARKERS = [
    "api", "endpoint", "request", "response", "payload", "field", "param", "form",
    "report", "export", "import", "upload", "download", "webhook", "schema",
    "báo cáo", "biểu mẫu", "trường", "tham số", "tải lên", "tải xuống", "kết xuất",
]
# Định nghĩa "done" / cách nghiệm thu.
DONE_MARKERS = [
    "acceptance", "done when", "expected", "criteria", "result should", "must return",
    "tiêu chí", "kết quả mong đợi", "nghiệm thu", "phải trả", "kỳ vọng", "định nghĩa hoàn thành",
]
# Có xử lý nhánh / lỗi chưa.
EDGE_MARKERS = [
    "when", "if", "case", "error", "fail", "invalid", "empty", "null", "timeout", "retry",
    "nếu", "trường hợp", "lỗi", "thất bại", "rỗng", "không hợp lệ", "quá hạn", "thử lại",
]

FEATURE_TYPES = ("feature", "story", "epic", "feat")
SHORT_DESC_WORDS = 12  # mô tả ngắn hơn -> coi như thiếu scope


def _has(text, terms):
    return any(t in text for t in terms)


def detect(type_, title, desc):
    """Heuristic thuần -> (questions, signals). questions: list dict đã chuẩn hoá.

    Cờ `blocking` type-aware để KHÔNG chặn oan: một bugfix rõ ràng (mô tả ngắn là bình thường,
    AC = bug hết repro) vẫn auto chạy; phần lớn câu blocking dồn vào FEATURE mơ hồ — đúng thực tế
    "yêu cầu hay mơ hồ" mà người dùng nêu. Bugfix chỉ bị chặn khi mô tả gần như rỗng nghĩa.
    """
    text = f"{title or ''} {desc or ''}".lower()
    words = len((desc or "").split())
    t = (type_ or "").lower()
    is_feature = t in FEATURE_TYPES
    q = []

    def add(category, ask, why, blocking, assumption):
        # `proposed` = a concrete answer to show the human for one-click confirm. The
        # heuristic path can't ground it, so it defaults to the generic assumption; the
        # agent path (with --context-file) overrides it with a repo-grounded proposal.
        q.append({"category": category, "ask": ask, "why": why,
                  "blocking": blocking, "assumption": assumption, "proposed": assumption})

    # 1. SCOPE — mô tả quá ngắn hoặc liệt kê co giãn => không rõ làm gì, tới đâu.
    #    Blocking khi: gần như rỗng nghĩa (<8 từ) HOẶC có từ co giãn ('v.v./tương tự').
    if words < SHORT_DESC_WORDS or _has(text, VAGUE_SCOPE):
        scope_blocking = words < 8 or _has(text, VAGUE_SCOPE)
        add("scope",
            "Phạm vi chính xác là gì — liệt kê cụ thể hạng mục cần làm và thứ KHÔNG nằm trong lần này?",
            "Mô tả ngắn/co giãn ('v.v.', 'tương tự'...) dễ khiến code làm thừa hoặc thiếu.",
            scope_blocking,
            "Làm đúng phần nêu tường minh; mọi mục 'tương tự/v.v.' để phase sau.")

    # 2. I/O — có nhắc API/dữ liệu nhưng dễ thiếu đặc tả input/output.
    #    Blocking với FEATURE (contract mới phải chốt); bugfix sửa contract sẵn có -> non-blocking.
    if _has(text, IO_MARKERS):
        add("io",
            "Input/output cụ thể: tên trường, kiểu, bắt buộc/không, định dạng và mã lỗi trả về?",
            "Thiếu đặc tả I/O -> contract sai, test không bám được.",
            is_feature,
            "Theo convention sẵn có của repo (DTO/format hiện hành); lỗi trả mã chuẩn của service.")

    # 3. DONE — không có định nghĩa nghiệm thu.
    #    Blocking với FEATURE (cần done-definition tường minh); bugfix -> AC ngầm là 'hết repro'.
    if not _has(text, DONE_MARKERS):
        add("acceptance",
            "Định nghĩa 'xong': kết quả mong đợi nào chứng minh task hoàn thành (để viết AC + test)?",
            "Không có tiêu chí nghiệm thu thì Deliver không có gì để đối chiếu.",
            is_feature,
            "Bugfix: xong = bug hết tái hiện + có test hồi quy. Feature: theo hành vi mô tả + unit test.")

    # 4. EDGE — không nhắc nhánh lỗi/biên.
    if not _has(text, EDGE_MARKERS):
        add("edge_case",
            "Các trường hợp biên/lỗi cần xử lý (rỗng, null, quá hạn, trùng, phân trang lớn...)?",
            "Bỏ sót edge case là nguồn bug phổ biến nhất khi yêu cầu mơ hồ.",
            False,
            "Xử lý các biên hiển nhiên (input rỗng/null -> báo lỗi rõ); chưa làm retry/đồng thời.")

    # 5. NON-FUNCTIONAL — có từ 'tối ưu/nhanh hơn' mà không có con số.
    if _has(text, VAGUE_NONFUNC):
        add("non_functional",
            "Mục tiêu phi chức năng có con số không (p95 < ? ms, chịu ? req/s, dữ liệu ? bản ghi)?",
            "'Tối ưu/nhanh hơn' không có ngưỡng thì không biết khi nào đạt.",
            False,
            "Không hồi quy hiệu năng so với hiện tại; chưa đặt mục tiêu định lượng mới.")

    signals = {"desc_words": words, "is_feature": t in FEATURE_TYPES,
               "io_markers": _has(text, IO_MARKERS),
               "has_done": _has(text, DONE_MARKERS)}
    return q, signals


# --- Đường agent: hỏi sát task hơn heuristic --------------------------------------------------

_AGENT_SYSTEM = (
    "You are a requirements-clarification assistant for a coding pipeline. The incoming task is "
    "usually vague. Surface the ambiguities that would make an engineer build the WRONG thing. "
    "If a <context> block is provided (task comments/checklist and candidate code files from the "
    "repo), USE IT: ask sharper task-specific questions and propose concrete answers grounded in "
    "that context. Reply ONLY inside one <clarify> block containing <question> children; each "
    "<question> has <category>scope|io|acceptance|edge_case|non_functional</category>"
    "<blocking>true|false</blocking><ask>the question</ask><why>why it matters</why>"
    "<proposed>your best concrete answer from the context (a real file/field/behaviour), or '-' "
    "if the context is insufficient</proposed>"
    "<assumption>a sane generic default if unanswered</assumption>. blocking=true only when a "
    "wrong answer means wrong code (scope, I/O contract, definition of done). No Markdown, nothing "
    "outside the block. Ask at most 6 questions; fewer is better."
)


def _parse_questions(block):
    out = []
    for qb in agent_parser.extract_all_tag_contents(block, "question"):
        ask = (agent_parser.extract_tag_content(qb, "ask") or "").strip()
        if not ask:
            continue
        cat = (agent_parser.extract_tag_content(qb, "category") or "scope").strip().lower()
        blk = (agent_parser.extract_tag_content(qb, "blocking") or "").strip().lower()
        assumption = (agent_parser.extract_tag_content(qb, "assumption") or "").strip()
        proposed = (agent_parser.extract_tag_content(qb, "proposed") or "").strip()
        # A "-" proposal means the context couldn't ground it -> fall back to assumption.
        if proposed in ("", "-", "—"):
            proposed = assumption
        out.append({
            "category": cat,
            "ask": ask,
            "why": (agent_parser.extract_tag_content(qb, "why") or "").strip(),
            "blocking": blk.startswith(("true", "có")),
            "assumption": assumption,
            "proposed": proposed,
        })
    return out


def _detect_via_agent(type_, title, desc, backend, model, dry_run_text, context=None):
    ctx_block = f"<context>{context.strip()}</context>\n" if context else ""
    prompt = (f"<task><type>{type_}</type><title>{title or ''}</title>"
              f"<description>{desc or ''}</description></task>\n"
              f"{ctx_block}"
              f"Surface clarifying questions in a <clarify> block.")
    raw = agent_runner.run_turn(prompt, system=_AGENT_SYSTEM, backend=backend, model=model,
                                dry_run_text=dry_run_text)
    block = agent_parser.extract_tag_content(raw, "clarify") or raw
    questions = _parse_questions(block)
    if not questions:
        raise ValueError("agent returned no parseable <question> entries")
    return questions


def cmd_analyze(args) -> dict:
    desc = args.desc
    if args.desc_file:
        with open(args.desc_file, encoding="utf-8") as f:
            desc = f.read().strip()

    context = None
    context_note = None
    context_file = getattr(args, "context_file", None)
    if context_file:
        try:
            with open(context_file, encoding="utf-8") as f:
                context = f.read().strip()
        except OSError as e:
            # non-fatal, but never silent: the caller must know clarify ran blind.
            context_note = f"context-file unreadable ({e}); clarify ran without repo context"

    source = "heuristic"
    fallback_note = None
    _, signals = detect(args.type, args.title, desc)
    if args.backend:
        try:
            questions = _detect_via_agent(args.type, args.title, desc,
                                          args.backend, args.model, args.dry_run_text,
                                          context=context)
            source = "agent"
        except (agent_runner.AgentRunError, ValueError) as e:
            questions, _ = detect(args.type, args.title, desc)
            fallback_note = f"agent clarify failed ({e}); used heuristic"
    else:
        questions, _ = detect(args.type, args.title, desc)

    blocking = [q for q in questions if q["blocking"]]
    verdict = "needs_clarification" if blocking else "pass"
    out = {
        "verdict": verdict,
        "blocking_count": len(blocking),
        "question_count": len(questions),
        "questions": questions,
        "source": source,
        "signals": signals,
    }
    notes = [n for n in (fallback_note, context_note) if n]
    if notes:
        out["note"] = "; ".join(notes)
    return out


# --- brief: gấp câu trả lời thành bản yêu cầu đã sắc -----------------------------------------

def _load_answers(path):
    """answers-file: JSON list [{ask|q, answer|a, assumption?}] hoặc map {ask: answer}."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    items = []
    if isinstance(data, dict):
        for k, v in data.items():
            items.append({"ask": k, "answer": v})
    elif isinstance(data, list):
        for d in data:
            # `proposed` (grounded default from analyze) beats a generic `assumption`.
            items.append({
                "ask": d.get("ask") or d.get("q") or d.get("question") or "",
                "answer": d.get("answer") or d.get("a") or "",
                "assumption": d.get("proposed") or d.get("assumption") or "",
            })
    return items


def cmd_brief(args) -> dict:
    desc = args.desc
    if args.desc_file:
        with open(args.desc_file, encoding="utf-8") as f:
            desc = f.read().strip()
    answers = _load_answers(args.answers_file) if args.answers_file else []

    resolved, assumed = [], []
    for a in answers:
        if (a.get("answer") or "").strip():
            resolved.append(a)
        elif (a.get("assumption") or "").strip():
            assumed.append(a)

    lines = [f"# Yêu cầu đã làm rõ — {args.title or '(không tiêu đề)'}", ""]
    lines += ["## Mô tả gốc", desc or "(trống)", ""]
    if resolved:
        lines += ["## Đã chốt (từ trả lời của người dùng)"]
        lines += [f"- **{a['ask']}** → {a['answer']}" for a in resolved]
        lines.append("")
    if assumed:
        lines += ["## Giả định mặc định (chưa hỏi — liếc lại nếu sai)"]
        lines += [f"- **{a['ask']}** → _giả định:_ {a['assumption']}" for a in assumed]
        lines.append("")
    brief = "\n".join(lines).rstrip() + "\n"

    brief_path = None
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(brief)
        brief_path = args.out

    # AC gợi ý: mỗi câu trả lời 'chốt' là một ứng viên tiêu chí nghiệm thu.
    acceptance_seeds = [a["answer"] for a in resolved if a.get("answer")]
    return {
        "verdict": "pass",
        "brief_path": brief_path,
        "brief": brief,
        "resolved_count": len(resolved),
        "assumed_count": len(assumed),
        "acceptance_seeds": acceptance_seeds,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Làm rõ yêu cầu mơ hồ ở Intake (trước Plan).")
    sub = ap.add_subparsers(dest="action", required=True)

    a = sub.add_parser("analyze", help="Phát hiện điểm mơ hồ -> câu hỏi + verdict")
    a.add_argument("--type", default=None, help="bugfix | feature | ...")
    a.add_argument("--title", default=None)
    a.add_argument("--desc", default=None)
    a.add_argument("--desc-file", default=None)
    a.add_argument("--context-file", default=None,
                   help="ngữ cảnh thêm (scout candidates / context pack) cho agent đề xuất câu trả lời")
    a.add_argument("--backend", default=None,
                   help="escalate cho agent (claude|cursor|api|dry-run); bỏ = heuristic")
    a.add_argument("--model", default=None)
    a.add_argument("--dry-run-text", default=None, help="cho --backend dry-run (test)")

    b = sub.add_parser("brief", help="Gấp câu trả lời -> bản yêu cầu đã sắc (Markdown)")
    b.add_argument("--title", default=None)
    b.add_argument("--desc", default=None)
    b.add_argument("--desc-file", default=None)
    b.add_argument("--answers-file", default=None, help="JSON câu trả lời (xem docstring)")
    b.add_argument("--out", default=None, help="ghi brief ra file (vd temp/runs/<id>_brief.md)")

    args = ap.parse_args()
    try:
        out = cmd_analyze(args) if args.action == "analyze" else cmd_brief(args)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        out = {"error": True, "message": str(e)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if isinstance(out, dict) and out.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
