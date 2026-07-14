#!/usr/bin/env python3
"""Intake triage — classify a task's complexity tier + autonomy mode.

Hybrid autonomy: not every task deserves the full Plan->debate->checkpoint
machinery. Triage runs at Intake and decides:
    tier  = trivial | standard | complex
    mode  = auto | checkpoint        (trivial -> auto; otherwise checkpoint)
    skip_debate = (tier == trivial)

The orchestrator feeds the result into `run_log init --tier <t> --mode <m>`, so
downstream gates (run_log.policy) know whether a failed gate BLOCKS (auto) or just
INFORMS the human (checkpoint).

Heuristic-first (no tokens spent by default). Pass --backend to escalate the call
to a real agent for an ambiguous task; on any agent/parse failure it falls back to
the heuristic so triage never hard-fails the pipeline.

Stdlib only. Output: one JSON object on stdout.

Usage:
    python triage.py classify --type bugfix --title "Fix NPE" --desc "..."
    python triage.py classify --type feature --desc-file task.txt --plan ../../../temp/runs/123_plan.xml
    python triage.py classify --type bugfix --desc "..." --backend claude
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

# Signals that a task touches risky surface area -> force complex (English + Vietnamese).
HIGH_RISK = [
    "schema", "migration", "security", "auth", "encrypt", "password", "payment",
    "concurren", "race condition", "deadlock", "transaction", "rollback",
    "permission", "rbac", "token", "crypto", "vulnerab", "injection",
    "bảo mật", "phân quyền", "thanh toán", "mã hoá", "mã hóa", "đồng thời",
    "di trú", "lược đồ", "giao dịch", "xác thực", "phân tán",
]
FEATURE_TYPES = ("feature", "story", "epic", "feat")
BUG_TYPES = ("bug", "bugfix", "defect", "fix", "hotfix")


def _n_target_files(plan_path):
    if not plan_path or not os.path.isfile(plan_path):
        return None
    with open(plan_path, encoding="utf-8") as f:
        text = f.read()
    files = agent_parser.extract_list_items(text, "target_files", "file")
    return len(files) if files else None


def classify(type_, title, desc, n_files):
    """Pure heuristic classifier -> (tier, mode, reason, signals)."""
    text = f"{title or ''} {desc or ''}".lower()
    desc_len = len(desc or "")
    high = sorted({k for k in HIGH_RISK if k in text})
    signals = {"high_risk_hits": high, "desc_len": desc_len, "target_files": n_files}
    t = (type_ or "").lower()

    if high:
        tier, reason = "complex", f"high-risk signal(s): {', '.join(high[:4])}"
    elif t in FEATURE_TYPES:
        if (n_files is not None and n_files >= 4) or desc_len > 800:
            tier, reason = "complex", "feature with broad scope"
        else:
            tier, reason = "standard", "feature, moderate scope"
    elif t in BUG_TYPES:
        narrow = (n_files is not None and n_files <= 1) or (n_files is None and desc_len < 300)
        tier, reason = ("trivial", "small bugfix, narrow scope") if narrow \
            else ("standard", "bugfix, moderate scope")
    else:
        tier, reason = "standard", "default (unclassified type)"

    mode = "auto" if tier == "trivial" else "checkpoint"
    return tier, mode, reason, signals


_AGENT_SYSTEM = (
    "You are a triage assistant for a coding pipeline. Classify the task by risk and "
    "scope. Reply ONLY inside one <triage> block with children <tier>trivial|standard|"
    "complex</tier><mode>auto|checkpoint</mode><reason>...</reason>. No Markdown, nothing "
    "outside the block. trivial+auto is for tiny, low-risk, single-file changes only; "
    "anything touching schema/security/concurrency/payments is complex."
)


def _classify_via_agent(type_, title, desc, backend, model, dry_run_text):
    prompt = (f"<task><type>{type_}</type><title>{title or ''}</title>"
              f"<description>{desc or ''}</description></task>\nClassify it in a <triage> block.")
    raw = agent_runner.run_turn(prompt, system=_AGENT_SYSTEM, backend=backend, model=model,
                                dry_run_text=dry_run_text)
    block = agent_parser.extract_tag_content(raw, "triage")
    if block is None:
        block = raw
    tier = (agent_parser.extract_tag_content(block, "tier") or "").lower().strip()
    mode = (agent_parser.extract_tag_content(block, "mode") or "").lower().strip()
    reason = (agent_parser.extract_tag_content(block, "reason") or "").strip()
    if tier not in ("trivial", "standard", "complex"):
        raise ValueError(f"agent returned invalid tier '{tier}'")
    if mode not in ("auto", "checkpoint"):
        mode = "auto" if tier == "trivial" else "checkpoint"
    return tier, mode, reason or "agent classification"


def cmd_classify(args) -> dict:
    desc = args.desc
    if args.desc_file:
        with open(args.desc_file, encoding="utf-8") as f:
            desc = f.read().strip()
    n_files = _n_target_files(args.plan)

    source = "heuristic"
    fallback_note = None
    if args.backend:
        try:
            tier, mode, reason = _classify_via_agent(
                args.type, args.title, desc, args.backend, args.model, args.dry_run_text)
            source = "agent"
        except (agent_runner.AgentRunError, ValueError) as e:
            tier, mode, reason, _ = classify(args.type, args.title, desc, n_files)
            fallback_note = f"agent triage failed ({e}); used heuristic"
    else:
        tier, mode, reason, _ = classify(args.type, args.title, desc, n_files)

    # Couple triage with the clarity gate: a task the clarify step flagged as vague
    # (unresolved blocking questions) must NOT run fully autonomous — even if it looks
    # tiny. `auto` requires BOTH a trivial tier AND a clean clarity verdict.
    clarity_downgrade = None
    clarity = (getattr(args, "clarity", None) or "").lower().strip()
    if clarity in ("needs_clarification", "fail", "needs-clarification") and mode == "auto":
        mode = "checkpoint"
        clarity_downgrade = "clarity=needs_clarification -> auto downgraded to checkpoint"

    # Honour explicit operator overrides last (a human can force a stricter/looser run).
    if args.force_tier:
        tier = args.force_tier
    if args.force_mode:
        mode = args.force_mode

    _, _, _, signals = classify(args.type, args.title, desc, n_files)
    out = {
        "tier": tier,
        "mode": mode,
        "reason": reason,
        "skip_debate": tier == "trivial",
        "source": source,
        "signals": signals,
    }
    if clarity_downgrade:
        out["clarity_downgrade"] = clarity_downgrade
    if fallback_note:
        out["note"] = fallback_note
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Classify a task's tier + autonomy mode.")
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("classify")
    p.add_argument("--type", default=None, help="bugfix | feature | ...")
    p.add_argument("--title", default=None)
    p.add_argument("--desc", default=None)
    p.add_argument("--desc-file", default=None)
    p.add_argument("--plan", default=None, help="plan xml to count <target_files>")
    p.add_argument("--backend", default=None,
                   help="escalate to an agent (claude|cursor|api|dry-run); omit = heuristic only")
    p.add_argument("--model", default=None)
    p.add_argument("--dry-run-text", default=None, help="for --backend dry-run (tests)")
    p.add_argument("--clarity", default=None,
                   help="clarify verdict (pass|needs_clarification); needs_clarification blocks auto")
    p.add_argument("--force-tier", default=None, choices=["trivial", "standard", "complex"])
    p.add_argument("--force-mode", default=None, choices=["auto", "checkpoint"])

    args = ap.parse_args()
    try:
        out = cmd_classify(args)
    except (ValueError, OSError) as e:
        out = {"error": True, "message": str(e)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if isinstance(out, dict) and out.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
