#!/usr/bin/env python3
"""Pre-MR review gate — review the real diff, emit a JSON verdict, post NOTHING.

The pipeline's debate happens at plan time; nothing independently reviewed the
*actual code* before it became a merge request. This runs a headless review agent
over `git diff <base>...<branch>` BEFORE create-mr and returns a structured verdict.

It deliberately does not post to GitLab (that would collide with the WRITE-confirm
guardrail and the human checkpoint). Dual-mode falls out of run_log.policy: in
checkpoint mode the verdict informs the approver; in auto mode a failing verdict
blocks the deliver transition.

    python review_gate.py run --root <clone_dir> --base dev --branch bugfix/123 > r.json
    python run_log.py record-gate <RID> review --verdict pass --json r.json   # if passed
    python run_log.py advance <RID> deliver

Stdlib only. Output: one JSON object on stdout.
"""

import argparse
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(_SKILLS, "fork-terminal", "tools"))
sys.path.insert(0, _HERE)
import agent_parser  # noqa: E402
import agent_runner  # noqa: E402

MAX_DIFF_CHARS = 200_000  # keep the prompt bounded for very large MRs

_SYSTEM = (
    "You are a strict senior code reviewer. Review ONLY the provided unified diff. "
    "Flag correctness bugs, security issues, and broken conventions. Distinguish "
    "BLOCKERS (must fix before merge) from WARNINGS (nice to fix). Reply ONLY inside "
    "one <review> block: <verdict>pass|fail</verdict>"
    "<blockers><item>...</item></blockers><warnings><item>...</item></warnings>"
    "<summary>one line</summary>. verdict is 'fail' if there is any blocker. "
    "No Markdown, nothing outside the block."
)


def _git_diff(root, base, branch):
    rng = f"{base}...{branch}" if base and branch else (base or branch or "HEAD")
    try:
        proc = subprocess.run(["git", "-C", root, "diff", rng],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
    except FileNotFoundError:
        raise ValueError("git not found on PATH")
    except subprocess.TimeoutExpired:
        raise ValueError("git diff timed out")
    if proc.returncode != 0:
        raise ValueError(f"git diff failed: {(proc.stderr or '').strip()[:300]}")
    return proc.stdout


def cmd_run(args) -> dict:
    if args.diff_file:
        with open(args.diff_file, encoding="utf-8", errors="replace") as f:
            diff = f.read()
    else:
        diff = _git_diff(os.path.abspath(args.root), args.base, args.branch)

    if not diff.strip():
        return {"error": True, "message": "empty diff — nothing to review (wrong base/branch?)"}

    truncated = len(diff) > MAX_DIFF_CHARS
    prompt = f"<diff>\n{diff[:MAX_DIFF_CHARS]}\n</diff>\nReview it in a <review> block."

    raw = agent_runner.run_turn(prompt, system=_SYSTEM, backend=args.backend,
                                model=args.model, dry_run_text=args.dry_run_text)
    block = agent_parser.extract_tag_content(raw, "review") or raw
    verdict = (agent_parser.extract_tag_content(block, "verdict") or "").lower().strip()
    blockers = agent_parser.extract_list_items(block, "blockers", "item")
    warnings = agent_parser.extract_list_items(block, "warnings", "item")
    summary = (agent_parser.extract_tag_content(block, "summary") or "").strip()

    # Strict: a pass with blockers is still a fail. Unknown verdict => fail (be safe).
    passed = (verdict == "pass") and not blockers
    return {
        "passed": passed,
        "verdict": "pass" if passed else "fail",
        "raw_verdict": verdict or None,
        "blockers": blockers,
        "warnings": warnings,
        "summary": summary or ("review failed" if not passed else "review passed"),
        "diff_truncated": truncated,
        "kind": "review",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Pre-MR diff review gate (no posting).")
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("run")
    p.add_argument("--root", default=".", help="clone_dir to run git diff in")
    p.add_argument("--base", default=None, help="target branch (e.g. dev)")
    p.add_argument("--branch", default=None, help="source branch (default current HEAD)")
    p.add_argument("--diff-file", default=None, help="read diff from file instead of git")
    p.add_argument("--backend", default="claude", help="review agent backend")
    p.add_argument("--model", default=None)
    p.add_argument("--dry-run-text", default=None, help="canned <review> block for tests")

    args = ap.parse_args()
    try:
        out = cmd_run(args)
    except (ValueError, OSError) as e:
        out = {"error": True, "message": str(e)}
    except agent_runner.AgentRunError as e:
        out = {"error": True, "message": f"review agent failed: {e}"}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if isinstance(out, dict) and out.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
