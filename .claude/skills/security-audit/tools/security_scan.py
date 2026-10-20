#!/usr/bin/env python3
"""Offline security scan of a product repo against the toolkit standard (cookbook/standard.md).

    python security_scan.py scan --repo work/etask [--history] [--format text|md|json] [--out F]
                                 [--fail-on high|medium|low] [--include-info] [--baseline F]
    python security_scan.py baseline --repo work/etask [--note "lý do"]   # [WRITE] accept today's findings
    python security_scan.py endpoints --repo work/etask [--format text|json] [--public|--unprotected|--ids]
    python security_scan.py rules                                       # the catalog (ids = the standard)

Exit code of `scan`: 0 = no NEW finding at/above --fail-on (default high) · 1 = there are · 2 = error ·
3 = no failing finding but the scan is INCOMPLETE (a file or the git history could not be analysed).
Baseline: <repo>/.agent/security-baseline.json (fingerprints of accepted findings; a moved line keeps its
fingerprint). Nothing is sent anywhere; credential values are masked in every output.
"""

import argparse
import json
import os
import runpy
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
runpy.run_path(os.path.join(os.path.dirname(os.path.dirname(_HERE)), "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.workflows.security import rules as R  # noqa: E402
from agent_skill_toolkit.workflows.security import scan as S  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(_HERE)), "dev-automation", "tools"))
import probe_common as pc  # noqa: E402,F401  (UTF-8 stdout on Windows)

SEV_ICON = {"high": "🔴", "medium": "🟡", "low": "🔵", "info": "⚪"}


def baseline_path(repo: str, override: str = "") -> str:
    return override or os.path.join(repo, ".agent", "security-baseline.json")


def load_baseline(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def summary(findings: list) -> dict:
    out = {}
    for f in findings:
        k = f"{f['severity']}/{f.get('status', 'new')}"
        out[k] = out.get(k, 0) + 1
    return out


def render_text(res: dict, include_info: bool) -> str:
    fs = [f for f in res["findings"] if include_info or f["severity"] != "info"]
    st = res["stats"]
    new = [f for f in fs if f.get("status") == "new"]
    lines = [f"Repo: {res['repo']}",
             f"Quét {st['files']} file · {st['endpoints']} endpoint ({st['java_controllers']} controller) · "
             f"{st['url_rules']} luật URL" + (f" · {st['history_commits']} commit lịch sử" if st['history_commits'] else "")
             + f" · {st['seconds']} s",
             f"Kết quả: {len(fs)} vấn đề, {len(new)} MỚI (chưa có trong baseline)", ""]
    for sev in ("high", "medium", "low", "info"):
        group = [f for f in fs if f["severity"] == sev]
        if not group:
            continue
        lines.append(f"{SEV_ICON[sev]} {sev.upper()} ({len(group)})")
        for f in group:
            mark = "" if f.get("status") == "new" else " [đã biết]"
            loc = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
            lines.append(f"  {f['rule']}{mark} {loc} — {f['title']}")
            lines.append(f"      {f['text']}" + (f"  ({f['detail']})" if f.get("detail") else ""))
        lines.append("")
    hidden = sum(1 for f in res["findings"] if f["severity"] == "info")
    if hidden and not include_info:
        lines.append(f"(+{hidden} mục info — endpoint nhận id cần kiểm quyền sở hữu, CSRF tắt… — thêm --include-info)")
    lines += render_gaps(res)
    lines.append("Cách sửa từng mã: .claude/skills/security-audit/cookbook/standard.md")
    return "\n".join(lines)


def render_gaps(res: dict) -> list:
    gaps = res.get("gaps") or []
    if not gaps:
        return []
    out = [f"⚠ QUÉT CHƯA ĐỦ — {len(gaps)} chỗ không phân tích được (kết quả KHÔNG có nghĩa là sạch):"]
    out += [f"  {g['path']}: {g['reason']}" for g in gaps[:30]]
    if len(gaps) > 30:
        out.append(f"  … +{len(gaps) - 30}")
    return out + [""]


def render_md(res: dict, include_info: bool) -> str:
    fs = [f for f in res["findings"] if include_info or f["severity"] != "info"]
    st = res["stats"]
    lines = [f"# Báo cáo bảo mật — `{os.path.basename(res['repo'])}`", "",
             f"{st['files']} file · {st['endpoints']} endpoint · {len(fs)} vấn đề "
             f"({sum(1 for f in fs if f.get('status') == 'new')} mới)", "",
             "| Mức | Mã | Vị trí | Vấn đề | Cách sửa |", "|---|---|---|---|---|"]
    for f in fs:
        loc = f"`{f['file']}:{f['line']}`" if f["line"] else f"`{f['file']}`"
        txt = f"{f['title']}<br><code>{f['text'][:120].replace('|', '¦')}</code>" + \
              (f" ({f['detail']})" if f.get("detail") else "")
        lines.append(f"| {SEV_ICON[f['severity']]} {f['severity']}{'' if f.get('status') == 'new' else ' (đã biết)'} "
                     f"| {f['rule']} | {loc} | {txt} | {f['fix']} |")
    gaps = render_gaps(res)
    return "\n".join(lines + ([""] + gaps if gaps else []))


def render_endpoints(eps: list, which: str) -> str:
    sel = {"public": lambda e: (e.get("access") or "").startswith(("permitAll", "anonymous")),
           "unprotected": lambda e: e.get("access") is None and not e["path"].startswith("/${"),
           "ids": lambda e: bool(e["id_params"]) and not e["method_auth"]}.get(which, lambda e: True)
    rows = [e for e in eps if sel(e)]
    lines = [f"{len(rows)} endpoint" + (f" ({which})" if which else "")]
    for e in sorted(rows, key=lambda e: (e["path"], e["method"])):
        auth = "@PreAuthorize" if e["method_auth"] else ""
        lines.append(f"  {e['method']:<6} {e['path']:<60} {e.get('access') or 'KHÔNG CÓ RULE':<28} {auth:<14} "
                     f"{e['file'].rsplit('/', 1)[-1]}:{e['line']}" + (f"  id={','.join(e['id_params'])}" if e["id_params"] else ""))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan")
    p.add_argument("--repo", required=True)
    p.add_argument("--history", action="store_true", help="also credentials added in git history")
    p.add_argument("--max-commits", type=int, default=300)
    p.add_argument("--format", choices=("text", "md", "json"), default="text")
    p.add_argument("--out", default="")
    p.add_argument("--fail-on", choices=("high", "medium", "low"), default="high")
    p.add_argument("--include-info", action="store_true")
    p.add_argument("--baseline", default="")
    p = sub.add_parser("baseline")
    p.add_argument("--repo", required=True)
    p.add_argument("--baseline", default="")
    p.add_argument("--history", action="store_true")
    p.add_argument("--note", default="")
    p = sub.add_parser("endpoints")
    p.add_argument("--repo", required=True)
    p.add_argument("--format", choices=("text", "json"), default="text")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--public", action="store_const", const="public", dest="which")
    g.add_argument("--unprotected", action="store_const", const="unprotected", dest="which")
    g.add_argument("--ids", action="store_const", const="ids", dest="which")
    sub.add_parser("rules")
    a = ap.parse_args(argv)

    if a.cmd == "rules":
        cat = [r.as_dict() for r in R.RULES] + [r.as_dict() for r in R.KEY_RULES] + R.ENDPOINT_RULES
        for r in sorted(cat, key=lambda r: r["id"]):
            print(f"{r['id']:<10} {r['severity']:<7} {r['title']}")
        return 0
    if not os.path.isdir(a.repo):
        print(json.dumps({"error": True, "message": f"no such repo dir: {a.repo}"}, ensure_ascii=False))
        return 2
    try:
        res = S.scan(a.repo, history=getattr(a, "history", False), max_commits=getattr(a, "max_commits", 300))
    except Exception as e:  # noqa: BLE001 — a scan that crashes must not look like "clean"
        print(json.dumps({"error": True, "message": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 2
    if a.cmd == "endpoints":
        if a.format == "json":
            print(json.dumps(res["endpoints"], ensure_ascii=False, indent=2))
        else:
            print(render_endpoints(res["endpoints"], a.which or ""))
        return 0
    path = baseline_path(a.repo, a.baseline)
    if a.cmd == "baseline":
        doc = S.baseline_doc(res["findings"], load_baseline(path), a.note)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(json.dumps({"baseline": path, "accepted": len(doc["accepted"]), "gaps": len(res["gaps"])},
                         ensure_ascii=False))
        return 0
    S.apply_baseline(res["findings"], load_baseline(path))
    failing = S.gate(res["findings"], a.fail_on)
    if a.format == "json":
        out = json.dumps({**res, "summary": summary(res["findings"]), "failing": len(failing)},
                         ensure_ascii=False, indent=2)
    elif a.format == "md":
        out = render_md(res, a.include_info)
    else:
        out = render_text(res, a.include_info)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(out + "\n")
        print(f"Đã ghi báo cáo: {a.out} · {len(failing)} vấn đề MỚI mức ≥ {a.fail_on}"
              + (f" · QUÉT CHƯA ĐỦ ({len(res['gaps'])} chỗ)" if res["gaps"] else ""))
    else:
        print(out)
    return 1 if failing else 3 if res["gaps"] else 0


if __name__ == "__main__":
    sys.exit(main())
