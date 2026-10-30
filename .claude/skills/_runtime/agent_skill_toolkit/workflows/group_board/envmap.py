"""Env map, read side (design §5, phase 2a): what each environment of a repo runs, and whether an MR is in it.

Inputs are injected (k8s workload JSON from `k8s.py workload`, GitLab lookups) so this module stays pure.
Every answer carries its SOURCE and a verification level, and a link to check by hand:

  ✅ đã xác minh   pod digest == digest of a Jenkins build whose commit is known  (NOT implemented yet:
                   needs build provenance — never claimed by this version)
  🟡 suy ra        every pod runs the spec image, ready; the image tag maps to a git tag (same NAME — the same
                   artifact is assumed, not proven)
  ❔ chưa xác minh no k8s access / no workload declared / no git tag for that version
  ⚠ lệch           pods run different digests, an image other than the spec, or are not ready (rollout?)

"MR in env X?" (§5.2): `có` (MR commit is an ancestor of the env commit) · `nghi port` (not an ancestor, but a
merged MR with the same title into the env's build branch is) · `không thấy commit gốc` (never "chưa có":
a cherry-pick may carry the change) · `không xác định` (env commit unknown) · `chưa merge`.
"""

import re
import time

from agent_skill_toolkit.core.board_store import parse_ts

LEVEL = {"verified": "✅ đã xác minh", "inferred": "🟡 suy ra", "unknown": "❔ chưa xác minh", "drift": "⚠ lệch"}
SKIP_ENVS = ("local",)


def env_targets(project: dict, show_prod: bool = False) -> list:
    """Per environment of a registry project: k8s workload, main container, git tag prefix, build branch.
    Read from environments.<env>.envmap (explicit) or what jenkins_scan / the k8s block already hold."""
    protected = {p.lower() for p in (project.get("protected_envs") or ("prod", "production"))}
    out = []
    for name, e in (project.get("environments") or {}).items():
        if name in SKIP_ENVS or (name.lower() in protected and not show_prod) or not isinstance(e, dict):
            continue
        jen, k8s, em = e.get("jenkins") or {}, e.get("k8s") or {}, e.get("envmap") or {}
        target = em.get("workload") or jen.get("resource") or (f"deployment/{k8s['deploy']}" if k8s.get("deploy") else "")
        if not target and not jen:
            continue
        out.append({"env": name, "target": target, "container": em.get("container") or jen.get("container") or "",
                    "tag_prefix": em.get("git_tag_prefix") or jen.get("git_tag_prefix") or "",
                    "branch": em.get("branch") or jen.get("branch") or e.get("deployed_branch") or "",
                    "scanned_at": jen.get("scanned_at") or "", "job": jen.get("job") or ""})
    return out


def _digest(image_id: str) -> str:
    """FULL content digest of a running container ('' when the runtime did not report one)."""
    d = (image_id or "").rsplit("@", 1)[-1] if "@" in (image_id or "") else ""
    return d if _SHA256.match(d) else ""


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _version(image: str) -> str:
    if not image or "@" in image.rsplit("/", 1)[-1]:
        return ""
    last = image.rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else ""


def snapshot(t: dict, workload: dict, tag_lookup) -> dict:
    """t = one env_targets() item; workload = `k8s.py workload` JSON or an error dict;
    tag_lookup(name) -> {commit_id, created_at} | error dict."""
    s = {"env": t["env"], "branch": t.get("branch") or "", "scanned_at": t.get("scanned_at") or "",
         "level": "unknown", "version": "", "commit": "", "tag": "", "notes": [], "observed_at": time.time()}
    if not t.get("target"):
        s["notes"].append("chưa khai workload k8s cho env này")
        return s
    if not isinstance(workload, dict) or workload.get("error"):
        s["notes"].append(f"không đọc được k8s ({(workload or {}).get('kind') or 'lỗi'})")
        return s
    spec = workload.get("spec_images") or {}
    cname = t.get("container") or next(iter(spec), "")
    spec_img = spec.get(cname, "")
    pods = workload.get("pods") or []
    running = [(p, c) for p in pods for c in p.get("containers") or [] if c.get("name") == cname]
    s["version"] = _version(spec_img)
    if not pods:
        s["notes"].append("không có pod nào đang chạy")
        return s
    # evidence per pod: a status for the main container, a valid FULL digest, ready, the spec image
    digests = {_digest(c.get("image_id")) for _, c in running}
    ready = sum(1 for _, c in running if c.get("ready"))
    on_spec = [p for p, c in running if c.get("image") == spec_img]
    rollouts = sorted(p.get("owner_created") for p in on_spec if p.get("owner_created"))
    s.update(ready=f"{ready}/{len(pods)}", digest=",".join(sorted(d[7:19] for d in digests if d)),
             rs_created_at=rollouts[-1] if rollouts else "")
    drift = (len(running) != len(pods) or "" in digests or len(digests) != 1
             or len(on_spec) != len(pods) or ready != len(pods))
    if drift:
        s["notes"].append("pod chạy khác nhau, thiếu digest hoặc chưa sẵn sàng — có thể đang rollout")
    if not t.get("tag_prefix") or not s["version"]:
        s["notes"].append("chưa khai tiền tố git tag" if not t.get("tag_prefix") else "image không có tag phiên bản")
        s["level"] = "drift" if drift else "unknown"
        return s
    s["tag"] = t["tag_prefix"] + s["version"]
    tag = tag_lookup(s["tag"]) or {}
    if tag.get("error") or not tag.get("commit_id"):
        s["notes"].append(f"không có git tag {s['tag']}")
        s["tag"] = ""
        s["level"] = "drift" if drift else "unknown"
        return s
    s["commit"], s["commit_at"] = tag["commit_id"], tag.get("created_at") or ""   # build time: unknown
    s["level"] = "drift" if drift else "inferred"
    return s


def mr_commit(mr: dict) -> str:
    """The commit that carries a merged MR, by merge method: merge commit | squash commit | (ff) head."""
    return mr.get("merge_commit_sha") or mr.get("squash_commit_sha") or mr.get("sha") or ""


def mr_where(mr: dict, snaps: list, is_ancestor, merged_mrs) -> list:
    """[{env, verdict, detail, via}] — is_ancestor(a, b) -> bool|None; merged_mrs(branch, title) -> [mr]."""
    if mr.get("state") != "merged":
        return [{"env": s["env"], "verdict": "chưa merge", "detail": f"MR đang {mr.get('state') or '?'}", "via": None}
                for s in snaps]
    commit, out = mr_commit(mr), []
    for s in snaps:
        if s.get("level") == "drift":
            out.append({"env": s["env"], "verdict": "không xác định", "via": None,
                        "detail": "env đang lệch (pod chưa đồng nhất) — " + "; ".join(s.get("notes") or [])})
            continue
        if not s.get("commit"):
            out.append({"env": s["env"], "verdict": "không xác định",
                        "detail": "; ".join(s.get("notes") or []) or "không biết commit của env", "via": None})
            continue
        anc = is_ancestor(commit, s["commit"])
        if anc is True:
            out.append({"env": s["env"], "verdict": "có", "via": None, "detail": "commit của MR nằm trong commit env"})
            continue
        if anc is None:
            out.append({"env": s["env"], "verdict": "không xác định", "detail": "GitLab không trả lời", "via": None})
            continue
        port = None
        if s.get("branch") and mr.get("title"):
            for c in merged_mrs(s["branch"], mr["title"][:80]):
                if c.get("iid") != mr.get("iid") and c.get("title") == mr.get("title") \
                        and is_ancestor(mr_commit(c), s["commit"]) is True:
                    port = c
                    break
        if port:
            out.append({"env": s["env"], "verdict": "nghi port", "via": port,
                        "detail": f"MR cùng tiêu đề !{port.get('iid')} vào {s['branch']} đã có — kiểm tay"})
        else:
            out.append({"env": s["env"], "verdict": "không thấy commit gốc", "via": None,
                        "detail": "không phải tổ tiên, không thấy MR port cùng tiêu đề (cherry-pick đổi tên vẫn có thể có)"})
    return out


# ----------------------------------------------------------------------------- text

def _when(iso: str) -> str:
    ts = parse_ts(iso)
    return time.strftime("%d/%m %H:%M", time.localtime(ts)) if ts else ""


def _observed(snaps: list) -> str:
    ts = [s.get("observed_at") for s in snaps if s.get("observed_at")]
    return time.strftime("%d/%m %H:%M", time.localtime(min(ts))) if ts else "?"


def env_text(repo: str, web: str, snaps: list) -> str:
    """Reply to `@bot env <repo>` — one block per env, links to check by hand."""
    lines = [f"🌐 {repo} — đang chạy ở từng môi trường (quan sát {_observed(snaps)}; bot chỉ đọc, tự kiểm bằng link)"]
    for s in snaps:
        head = f"{s['env'].capitalize()}: {LEVEL[s['level']]}"
        bits = [s.get("version") or "", f"commit {s['commit'][:8]}" if s.get("commit") else "",
                f"ReplicaSet tạo {_when(s['rs_created_at'])}" if s.get("rs_created_at") else "",
                f"pod {s['ready']}" if s.get("ready") else "",
                f"nhánh build {s['branch']}" + (f" (cấu hình quét {s['scanned_at'][:10]})" if s.get("scanned_at") else "")
                if s.get("branch") else ""]
        lines.append(" · ".join([head] + [b for b in bits if b]))
        if s.get("notes"):
            lines.append("  ↳ " + "; ".join(s["notes"]))
        if s.get("tag") and web:
            lines.append(f"  🔗 {web}/-/tags/{s['tag']}")
    lines.append("Mức: 🟡 = khớp tên tag ảnh ↔ git tag (chưa đối chiếu digest với bản build) · ❔ thiếu nguồn/quyền · ⚠ lệch."
                 " Giờ tạo ReplicaSet ≈ giờ triển khai (rollback dùng lại bản cũ thì không đúng)")
    return "\n".join(lines)


def mr_text(repo: str, web: str, mr: dict, rows: list, snaps: list) -> str:
    commit = mr_commit(mr)
    title = " ".join(str(mr.get("title") or "").split())[:80]
    head = f"🌐 {repo}!{mr.get('iid')} · {title} (quan sát {_observed(snaps)})"
    if mr.get("state") == "merged":
        head += f" — đã merge vào {mr.get('target_branch')} (commit {commit[:8]})"
    lines = [head]
    by_env = {s["env"]: s for s in snaps}
    for r in rows:
        level = LEVEL[(by_env.get(r["env"]) or {}).get("level") or "unknown"]
        line = f"{r['env'].capitalize()}: {r['verdict']} — {r['detail']} · mức env: {level}"
        lines.append(line)
        env_commit = (by_env.get(r["env"]) or {}).get("commit")
        if web and commit and env_commit and r["verdict"] in ("có", "không thấy commit gốc"):
            lines.append(f"  🔗 {web}/-/compare/{env_commit}...{commit}")
        if r.get("via") and r["via"].get("web_url"):
            lines.append(f"  🔗 {r['via']['web_url']}")
    if mr.get("web_url"):
        lines.append(f"MR: {mr['web_url']}")
    return "\n".join(lines)
