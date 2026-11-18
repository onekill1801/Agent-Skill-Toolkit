"""Env map, read side (design §5, phase 2a): what each environment of a repo runs, and whether an MR is in it.

Inputs are injected (k8s workload JSON from `k8s.py workload`, GitLab lookups) so this module stays pure.
Every answer carries its SOURCE and a verification level, and a link to check by hand:

  ✅ đã xác minh   every pod runs ONE digest == the digest Jenkins build N reported when pushing THAT image
                   repository at that version (no other digest pushed for it), build N printed the commit it
                   built, pushed the git tag successfully, and the tag still points at that commit
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

LEVEL = {"verified": "✅ đã xác minh", "inferred": "🟡 suy ra", "unknown": "❔ chưa xác minh", "drift": "⚠ lệch",
         "built": "🔨 theo build Jenkins"}
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


def _vkey(v: str):
    return tuple(int(x) if x.isdigit() else x for x in re.split(r"[.\-]", v or ""))


def newer_tags(prefix: str, version: str, names: list) -> list:
    """Tags `<prefix><v>` with v > version (numeric compare) — built but not running here."""
    out = []
    for n in names or []:
        v = n[len(prefix):] if n.startswith(prefix) else ""
        if v and re.fullmatch(r"[0-9]+(\.[0-9]+)*", v) and _vkey(v) > _vkey(version):
            out.append(n)
    return sorted(out, key=lambda n: _vkey(n[len(prefix):]), reverse=True)


def from_build(s: dict, t: dict, lg: dict, tag_lookup=None) -> dict:
    """No k8s workload declared: the newest SUCCESSFUL build of the env's job (`jenkins.py lastgood`) — what was
    most likely deployed, never proof of what runs (level "built"). Pure, tested."""
    s["notes"].append("chưa khai workload k8s — theo build Jenkins thành công gần nhất, chưa xác minh pod")
    if not isinstance(lg, dict) or lg.get("error"):
        s["notes"].append(f"không đọc được Jenkins ({(lg or {}).get('message') or 'lỗi'})")
        return s
    good, last = lg.get("good") or {}, lg.get("last") or {}
    if not good.get("number"):
        s["notes"].append("job chưa có build thành công nào")
        return s
    versions = sorted({p.get("version") for p in good.get("pushes") or [] if p.get("version")})
    s.update(level="built", version=",".join(versions), commit=good.get("commit_sha") or "",
             built_at=(good.get("timestamp") or 0) / 1000.0,
             build={"number": good["number"], "result": "SUCCESS", "url": good.get("url"),
                    "timestamp": good.get("timestamp")})
    if not s["commit"] and len(versions) == 1 and t.get("tag_prefix") and tag_lookup:
        tag = tag_lookup(t["tag_prefix"] + versions[0]) or {}
        if tag.get("commit_id"):
            s["commit"], s["tag"] = tag["commit_id"], t["tag_prefix"] + versions[0]
    if good.get("log_error"):
        s["notes"].append(f"không đọc được log build #{good['number']} ({good['log_error']})")
    elif not versions:
        s["notes"].append(f"build #{good['number']} không push image nào (theo log)")
    if last.get("number") and last["number"] != good["number"]:
        what = "đang chạy" if last.get("building") else (last.get("result") or "?")
        s["notes"].append(f"build mới hơn #{last['number']} {what}")
    return s


def snapshot(t: dict, workload: dict, tag_lookup, provenance=None, tag_names=None, lastgood=None) -> dict:
    """t = one env_targets() item; workload = `k8s.py workload` JSON or an error dict;
    tag_lookup(name) -> {commit_id, created_at} | error dict;
    provenance(version) -> `jenkins.py provenance` JSON (found, number, result, url, digest, git_tag) | None;
    lastgood() -> `jenkins.py lastgood` JSON, used only when no k8s workload is declared."""
    s = {"env": t["env"], "branch": t.get("branch") or "", "scanned_at": t.get("scanned_at") or "",
         "level": "unknown", "version": "", "commit": "", "tag": "", "notes": [], "observed_at": time.time()}
    if not t.get("target"):
        if lastgood and t.get("job"):
            return from_build(s, t, lastgood(), tag_lookup)
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
    if tag_names and re.fullmatch(r"[0-9]+(\.[0-9]+)*", s["version"]):
        newer = newer_tags(t["tag_prefix"], s["version"], tag_names(t["tag_prefix"]))
        if newer:
            s["newer_tags"] = newer[:3]
            s["notes"].append(f"có bản build mới hơn chưa chạy ở đây: {', '.join(newer[:3])}")
    if drift or not provenance or not t.get("job"):
        return s
    prov = provenance(s["version"]) or {}
    if not prov.get("found"):
        s["notes"].append("không tìm thấy build Jenkins đã push phiên bản này (trong các build gần nhất)")
        return s
    n = prov.get("number")
    s["build"] = {k: prov.get(k) for k in ("number", "result", "url", "timestamp")}
    pod_digest, repo = next(iter(digests)), spec_img.rsplit(":", 1)[0]
    pushed = {p.get("digest") for p in prov.get("pushes") or [] if p.get("image") == repo}
    if prov.get("result") not in (None, "SUCCESS"):
        failed = prov.get("failed_stages")
        where = ("bước lỗi: " + ", ".join(failed)) if failed else             ("mọi stage báo SUCCESS — lỗi nằm ngoài stage, xem log" if failed == [] else "không đọc được danh sách stage")
        s["notes"].append(f"Jenkins ghi build #{n} là {prov.get('result')} ({where})")
    if not pushed:
        s["notes"].append(f"build #{n} không push image này ({repo.rsplit('/', 1)[-1]}) — commit chỉ suy ra")
    elif len(pushed) > 1:
        s["notes"].append(f"build #{n} push nhiều digest cho cùng image — không xác minh được")
    elif pod_digest not in pushed:
        s["notes"].append(f"digest pod ≠ digest build #{n} đã push — image đã bị đẩy lại?")
        s["level"] = "drift"
    elif s["tag"] not in (prov.get("tags_pushed") or []) or not prov.get("commit_sha"):
        s["notes"].append(f"build #{n} không ghi commit hoặc không push được git tag {s['tag']} — commit chỉ suy ra")
    elif prov["commit_sha"] != s["commit"]:
        s["notes"].append(f"git tag {s['tag']} giờ trỏ {s['commit'][:8]} nhưng build #{n} đã build "
                          f"{prov['commit_sha'][:8]} — tag bị dời?")
        s["level"] = "drift"
    else:
        s["level"] = "verified"
    return s


def mr_commit(mr: dict) -> str:
    """The commit that carries a merged MR, by merge method: merge commit | squash commit | (ff) head."""
    return mr.get("merge_commit_sha") or mr.get("squash_commit_sha") or mr.get("sha") or ""


def reverted_in(commits, mr_sha: str, title: str) -> bool:
    """A commit between the MR and the env commit reverts it (git's default revert message / title)."""
    for c in commits or []:
        msg = f"{c.get('title') or ''}\n{c.get('message') or ''}"
        if mr_sha and f"This reverts commit {mr_sha}" in msg:
            return True
        if title and (c.get("title") or "").strip() == f'Revert "{title}"':
            return True
    return False


def mr_where(mr: dict, snaps: list, is_ancestor, merged_mrs, compare=None) -> list:
    """[{env, verdict, detail, via, revert}] — is_ancestor(a, b) -> bool|None; merged_mrs(branch, title) -> [mr];
    compare(frm, to) -> commits | None (revert check: "có" + a later revert = flagged, not denied)."""
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
            row = {"env": s["env"], "verdict": "có", "via": None, "detail": "commit của MR nằm trong commit env"}
            if compare and reverted_in(compare(commit, s["commit"]), commit, mr.get("title") or ""):
                row["detail"] += " — ⚠ có commit revert sau đó, kiểm tay"
                row["revert"] = True
            out.append(row)
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
        if (s.get("build") or {}).get("url"):
            lines.append(f"  🔗 build #{s['build']['number']}: {s['build']['url']}")
    lines.append("Mức: ✅ = digest pod trùng digest build Jenkins đã push và build đó tạo git tag · 🟡 = chỉ khớp tên tag"
                 " ảnh ↔ git tag · ❔ thiếu nguồn/quyền · ⚠ lệch."
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


# ---------------------------------------------------------------- snapshot table (extension "Môi trường" tab)
SNAPSHOT_HEAD = "🌐 [ENV-SNAPSHOT]"
DELTA_HEAD = "🌐 [ENV-DELTA]"
_ICON = {"verified": "✅", "inferred": "🟡", "unknown": "❔", "drift": "⚠", "built": "🔨"}


def _cell(v) -> str:
    return " ".join(str(v or "").replace("|", "/").split()) or "—"


def snapshot_row(repo: str, s: dict) -> str:
    """One table row: repo | env | mức | phiên bản | commit | lên lúc | nhánh | build ('—' = unknown)."""
    return " | ".join(_cell(x) for x in (
        repo, s.get("env"), _ICON.get(s.get("level") or "unknown", "❔"), s.get("version"), s.get("commit"),
        _when(s.get("rs_created_at") or s.get("built_at") or ""), s.get("branch"), (s.get("build") or {}).get("url")))


def _head(kind: str, title: str, now: float, scope: str, seq) -> str:
    return f"{kind} {title} · {time.strftime('%d/%m %H:%M', time.localtime(now))} · {scope}" + (f" · #{seq}" if seq else "")


PART_CHARS = 6000                                      # one chat message; a bigger table travels in parts


def table_parts(kind: str, rows: dict, webs: dict, scope: str, seq: int, now: float = None,
                limit: int = None) -> list:
    """kind "SNAPSHOT" (whole table) or "DELTA" (changed rows only); rows = {"repo|env": row} in display order.
    Split by whole repo blocks into messages of ≤ limit chars; n > 1 ⇒ title "… (i/n)", same time + #seq in every
    part (the board uses a table only once it holds every part of it). The limit counts the header too; a repo
    whose rows alone exceed it is split by rows, its `repo | …` line repeated in each part."""
    now = now or time.time()
    head = (SNAPSHOT_HEAD, "Bảng môi trường") if kind == "SNAPSHOT" else (DELTA_HEAD, "Môi trường đổi")
    by_repo = {}
    for k, row in rows.items():
        by_repo.setdefault(k.split("|", 1)[0], []).append(row)
    limit = limit or PART_CHARS
    budget = limit - len(_head(head[0], head[1] + " (999/999)", now, scope, seq)) - 1
    blocks = []
    for repo in sorted(by_repo):
        top = f"repo | {_cell(repo)} | {_cell((webs or {}).get(repo))}"
        b = [top]
        for row in by_repo[repo]:
            if len(b) > 1 and sum(len(x) + 1 for x in b) + len(row) + 1 > budget:
                blocks.append(b)
                b = [top]
            b.append(row)
        blocks.append(b)
    chunks, cur, size = [], [], 0
    for b in blocks:
        n = sum(len(x) + 1 for x in b)
        if cur and size + n > budget:
            chunks.append(cur)
            cur, size = [], 0
        cur, size = cur + b, size + n
    chunks.append(cur)
    total = len(chunks)
    return ["\n".join([_head(head[0], head[1] + (f" ({i}/{total})" if total > 1 else ""), now, scope, seq)] + c)
            for i, c in enumerate(chunks, 1)]


def table_text(kind: str, rows: dict, webs: dict, scope: str, seq: int, now: float = None) -> str:
    """The whole table as ONE message (tests / small tables); senders use table_parts."""
    return table_parts(kind, rows, webs, scope, seq, now, limit=10 ** 9)[0]


def snapshot_text(repos: list, now: float = None, scope: str = "nhóm", seq: int = 0) -> str:
    """The FULL table (PROTOCOL.md "Ảnh chụp môi trường") — sent on request ('@bot env') or when the bot has no
    baseline yet; afterwards only changed rows travel (delta_text):

        🌐 [ENV-SNAPSHOT] Bảng môi trường · 31/10 08:30 · nhóm · #12
        repo | etask | https://gitlab…/idaas/etask
        etask | sandbox | ✅ | 0.198.45 | <commit 40> | 30/10 01:16 | merge/rfc-into-vitess | <build url>

    repos = [(repo, web, snaps)]. seq = per-scope message number (the board detects a missed delta by a gap).
    scope: "nhóm" (never prod) or "chủ nhóm" (the owner's private copy, prod included)."""
    now = now or time.time()
    lines = [_head(SNAPSHOT_HEAD, "Bảng môi trường", now, scope, seq)]
    for repo, web, snaps in repos:
        lines.append(f"repo | {_cell(repo)} | {_cell(web)}")
        lines += [snapshot_row(repo, s) for s in snaps]
    return "\n".join(lines)


def delta_text(rows: list, now: float = None, scope: str = "nhóm", seq: int = 0, webs: dict = None) -> str:
    """Only the rows that changed since the last message of this scope (same columns as the table):

        🌐 [ENV-DELTA] Môi trường đổi · 31/10 19:20 · nhóm · #13
        etask | sandbox | ✅ | 0.199.45 | … """
    now = now or time.time()
    lines = [_head(DELTA_HEAD, "Môi trường đổi", now, scope, seq)]
    for repo, web in sorted((webs or {}).items()):
        lines.append(f"repo | {_cell(repo)} | {_cell(web)}")
    return "\n".join(lines + list(rows))
