"""Deploy cards D-n (design §3.4–§3.5, §6, phase 4): a member asks, ANOTHER person approves, the bot runs Jenkins and
waits for the pods — never "thành công" before the cluster runs the image the build pushed.

    @bot deploy <repo> lên <env> | lý do: …   (perm deploy:<env>)        -> D-n "chờ duyệt"
    duyệt D-n                                  (approve:<env>, ≠ requester, ≠ the owner account, verified identity)
    từ chối D-n: <lý do> · huỷ D-n (requester, while waiting) · deploy lại D-n (requester ⇒ a NEW card, new approval)

Only envs opted in by the registry: environments.<env>.chat_deploy = {"allow_unpinned": true} (user decision 30/10:
dev only — the etask jobs take no commit/tag parameter, spike S8). The card says plainly that what runs is the HEAD of
the branch AT RUN TIME; the approval binds to (branch, HEAD at approval, job config hash), checked again before the
trigger; afterwards the commit the build printed is compared with the approved one.

The owner shares the bot's account ⇒ approves via Telegram (payload "tg"), never in chat.

Runner (`step`): one stage per call, each persisted on the card BEFORE the next side effect ⇒ a crash resumes:
    trigger  -> (queue url) -> queued -> (build number) -> building -> (result + pushes) -> rollout -> done
A trigger whose answer was lost is reconciled against the builds started after it — never triggered twice.

Every function takes the board store and an `ops` object (see group_watch._DeployOps / tests' FakeOps) and returns
{"card": card, "send": (text, people)} for a card message, {"reply": (text, people)}, or {}.
"""

import hashlib
import re
import time
import uuid

from agent_skill_toolkit.workflows.group_board import envmap, render

APPROVE_TTL = 2 * 3600
QUEUE_TIMEOUT = 15 * 60
BUILD_TIMEOUT = 45 * 60
ROLLOUT_TIMEOUT = 15 * 60
RECONCILE_SLACK_S = 5          # a lost trigger answer: the build must start within [triggered_at − slack, + QUEUE_TIMEOUT]
RUN_WITHIN_S = 30 * 60         # an approval is good for triggering this long (then: approve again)
CHAT_DEPLOY_ENVS = {"dev"}     # user decision 30/10 + 31/10: chat deploys dev only; prod has its own flow, never here
ALWAYS_PROTECTED = {"prod", "production"}
LIVE = ("waiting", "running")

D_STATE_WORD = {"waiting": "chờ duyệt", "running": "đang chạy", "done": "thành công", "failed": "thất bại",
                "built": "đã build — chưa xác minh rollout", "mismatch": "lỗi", "error": "lỗi",
                "uncertain": "lỗi — không chắc", "rejected": "từ chối", "cancelled": "huỷ", "expired": "huỷ"}


# ---------------------------------------------------------------- registry
def deploy_target(reg: dict, repo: str, env: str):
    """(target, error). target = {repo, project_id, env, job, branch, workload, container, unpinned}."""
    key = next((k for k in reg if k.lower() == (repo or "").lower()), None)
    proj = reg.get(key) if key else None
    if not isinstance(proj, dict):
        return None, f"không có repo '{render.one_line(repo, 40)}' trong registry"
    env = (env or "").lower()
    protected = ALWAYS_PROTECTED | {p.lower() for p in (proj.get("protected_envs") or ())}
    if env in protected:
        return None, f"{env} là môi trường được bảo vệ — không deploy qua chat (prod có luồng riêng)"
    if env not in CHAT_DEPLOY_ENVS:
        return None, f"deploy qua chat chỉ cho {', '.join(sorted(CHAT_DEPLOY_ENVS))} — {env} chưa hỗ trợ"
    e = (proj.get("environments") or {}).get(env)
    if not isinstance(e, dict):
        return None, f"{key} không có môi trường '{env}'"
    cd = e.get("chat_deploy")
    if not isinstance(cd, dict) or not cd:
        return None, (f"{key} @ {env} chưa bật deploy qua chat (job không nhận commit/tag nên không ghim được — "
                      "chủ nhóm bật trong registry nếu chấp nhận deploy HEAD nhánh)")
    if not cd.get("allow_unpinned"):
        return None, f"{key} @ {env}: job chưa nhận tham số commit/tag — chưa hỗ trợ deploy ghim commit"
    jen = e.get("jenkins") or {}
    if not jen.get("job"):
        return None, f"{key} @ {env} chưa khai job Jenkins"
    branch = jen.get("branch") or e.get("deployed_branch") or ""
    tgt = next((t for t in envmap.env_targets(proj) if t["env"] == env), {})
    return {"repo": key, "project_id": proj.get("gitlab_project_id"), "env": env, "job": jen["job"],
            "branch": branch, "workload": tgt.get("target") or "", "container": tgt.get("container") or "",
            "unpinned": True}, ""


# ---------------------------------------------------------------- rendering
def render_deploy(card: dict, note: str = "") -> tuple:
    p = card.get("payload") or {}
    people = []
    head = [f"Trạng thái: {D_STATE_WORD.get(card.get('state'), 'chờ duyệt')}"]
    for label, who in (("Yêu cầu", p.get("requester") or {}), ("Duyệt", p.get("approver") or {})):
        if who.get("name"):
            n = render._value(who["name"], 60)
            head.append(f"{label}: @{n}")
            if who.get("id"):
                people.append((n, who["id"]))
    head.append(f"Môi trường: {p.get('env')}")
    lines = [f"🤖 [{card['id']}] Deploy {p.get('repo')} → {p.get('env')}", " · ".join(head)]
    meta = [f"Nhánh: {p.get('branch') or '?'}"]
    if p.get("commit"):
        meta.append(f"HEAD lúc {'duyệt' if p.get('approved_at') else 'yêu cầu'}: {p['commit'][:8]}")
    if p.get("build_commit"):
        meta.append(f"Build: {p['build_commit'][:8]}")
    if p.get("version"):
        meta.append(f"Phiên bản: {render._value(p['version'], 40)}")
    lines.append(" · ".join(meta))
    if p.get("reason"):
        lines.append("Lý do: " + render._value(p["reason"], 200))
    if p.get("result"):
        lines.append("Kết quả: " + render._value(p["result"], 300))
    if note:
        lines.append("Ghi chú: " + render._value(note, 500))
    if p.get("build_url"):
        lines.append("🔗 " + p["build_url"])
    return render.defuse_body("\n".join(lines)), people


def _card(card, note=""):
    return {"card": card, "send": render_deploy(card, note)}


def _reply(ident: dict, text: str):
    n = render.one_line(ident.get("name") or "?", 60)
    return {"reply": (render.defuse(f"🤖 @{n}: {text}"), [(n, ident.get("fchat_id") or "")])}


def _get(board, group, card_id):
    card = board.card_get(group, (card_id or "").upper())
    return card if card and card.get("kind") == "D" else {}


def approval_hash(p: dict) -> str:
    """What an approval binds to: repo, env, job, branch, HEAD, job config — any change ⇒ a new approval."""
    raw = "|".join(str(p.get(k) or "") for k in ("repo", "env", "job", "branch", "commit", "job_hash"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- request / approve / reject / cancel / redo
def request(board, group: str, ident: dict, target: dict, reason: str, ops, approvers=(), owner_name: str = "",
            now: float = None):
    now = now or time.time()
    for c in board.cards_in(group, "D", LIVE):
        cp = c["payload"]
        if cp.get("repo") == target["repo"] and cp.get("env") == target["env"]:
            return _reply(ident, f"đang có {c['id']} cho {target['repo']} @ {target['env']} — chờ nó xong hoặc huỷ")
    head = ops.branch_head(target["project_id"], target["branch"])
    if not head:
        return _reply(ident, f"không đọc được HEAD nhánh {target['branch']} trên GitLab — thử lại sau")
    job_hash = ops.job_hash(target["job"])
    if not job_hash:
        return _reply(ident, "không đọc được cấu hình job Jenkins — không bảo đảm được lần duyệt đúng cấu hình, chưa nhận")
    payload = {**target, "requester": {"id": ident.get("fchat_id") or "", "name": ident.get("name") or ""},
               "reason": render.one_line(reason, 200), "commit": head, "job_hash": job_hash,
               "requested_at": now, "expires_at": now + APPROVE_TTL,
               "approvers": [{"id": a.get("fchat_id"), "name": a.get("name")} for a in approvers]}
    payload["approval_hash"] = approval_hash(payload)
    card = board.card_open(group, "D", f"{target['repo']}:{target['env']}:{uuid.uuid4().hex}",
                           f"Deploy {target['repo']} → {target['env']}", payload)
    card = board.card_update(group, card["id"], state="waiting")
    who = ", ".join("@" + render._value(a["name"], 60) for a in payload["approvers"] if a.get("name"))
    note = (f"deploy HEAD của nhánh {target['branch']} TẠI LÚC CHẠY (job không ghim được commit); cần một người KHÁC "
            f"có quyền approve:{target['env']} gõ \"duyệt {card['id']}\" trong 2 giờ"
            + (f" ({who})" if who else "") + f"; {owner_name or 'chủ nhóm'} duyệt qua Telegram")
    out = _card(card, note)
    out["send"] = (out["send"][0], out["send"][1] + [(a["name"], a["id"]) for a in payload["approvers"]
                                                      if a.get("name") and a.get("id")])
    return out


def _expire_if_due(board, group, card, now):
    if card["state"] == "waiting" and now > (card["payload"].get("expires_at") or 0):
        return board.card_update(group, card["id"], state="expired", if_revision=card["revision"])
    return None


def approve(board, group: str, card_id: str, approver: dict, ops, now: float = None, via: str = "chat",
            proof: dict = None, requester_ident: dict = None, owner_id: str = ""):
    """approver = identity (chat) or {"name", "fchat_id"} of the owner (via="telegram", proof = the bridge record)."""
    now = now or time.time()
    card = _get(board, group, card_id)
    if not card:
        return _reply(approver, f"không thấy thẻ {card_id.upper()} trong nhóm này")
    p = card["payload"]
    if card["state"] != "waiting":
        return _reply(approver, f"{card['id']} không còn chờ duyệt ({D_STATE_WORD.get(card['state'], card['state'])})")
    exp = _expire_if_due(board, group, card, now)
    if exp:
        return _card(exp, "hết 2 giờ chờ duyệt — gõ \"deploy lại\" để tạo yêu cầu mới")
    if str(approver.get("fchat_id")) == str(p["requester"]["id"]):
        return _reply(approver, f"người yêu cầu không tự duyệt {card['id']} — cần một người khác")
    if via == "chat":
        if str(approver.get("fchat_id")) == str(owner_id):
            return _reply(approver, "tài khoản chủ nhóm dùng chung với bot — chủ nhóm duyệt qua Telegram")
        if f"approve:{p['env']}" not in (approver.get("perms") or ()):
            return _reply(approver, f"chưa có quyền approve:{p['env']} — nhờ chủ nhóm cấp")
        if not approver.get("verified"):
            return _reply(approver, "liên kết danh bạ của bạn chưa được chủ nhóm xác minh — chưa duyệt được")
    if requester_ident is not None and f"deploy:{p['env']}" not in (requester_ident.get("perms") or ()) \
            and "admin" not in (requester_ident.get("perms") or ()):
        card = board.card_update(group, card["id"], state="cancelled")
        return _card(card, f"người yêu cầu không còn quyền deploy:{p['env']} — huỷ")
    head = ops.branch_head(p["project_id"], p["branch"])
    if not head:
        return _reply(approver, f"không đọc được HEAD nhánh {p['branch']} — chưa chạy, thử duyệt lại sau")
    job_hash = ops.job_hash(p["job"])
    if not job_hash:
        return _reply(approver, "không đọc được cấu hình job Jenkins — chưa chạy, thử duyệt lại sau")
    if job_hash != p.get("job_hash"):
        card = board.card_update(group, card["id"], state="cancelled")
        return _card(card, "cấu hình job Jenkins đã đổi sau khi yêu cầu — huỷ, gõ \"deploy lại\" để duyệt theo cấu hình mới")
    if head != p["commit"]:                               # nội dung đổi ⇒ lần duyệt này không tính, xin duyệt lại
        card = board.card_update(group, card["id"], commit=head, approval_hash=approval_hash({**p, "commit": head}),
                                 expires_at=now + APPROVE_TTL)
        return _card(card, f"HEAD nhánh {p['branch']} đã đổi ({p['commit'][:8]} → {head[:8]}) — lần duyệt này không "
                           f"tính, cần duyệt lại \"duyệt {card['id']}\"")
    claimed = board.card_update(group, card["id"], state="running", if_revision=card["revision"], stage="trigger",
                                approved_at=now, approver={"id": approver.get("fchat_id") or "",
                                                           "name": approver.get("name") or ""},
                                approval={"via": via, "by": approver.get("fchat_id") or "", "at": now,
                                          "hash": p["approval_hash"], "proof": proof or {}})
    if not claimed:
        return _reply(approver, f"{card['id']} vừa được xử lý bởi người khác")
    board.event("deploy_approved", group=group, card=card["id"], via=via, by=approver.get("fchat_id"),
                hash=p["approval_hash"])
    return _card(claimed, "đã duyệt — bot kích Jenkins (HEAD nhánh lúc chạy)")


def reject(board, group: str, card_id: str, ident: dict, reason: str, owner_id: str = ""):
    card = _get(board, group, card_id)
    if not card:
        return _reply(ident, f"không thấy thẻ {card_id.upper()} trong nhóm này")
    p = card["payload"]
    if card["state"] != "waiting":
        return _reply(ident, f"{card['id']} không còn chờ duyệt")
    if str(ident.get("fchat_id")) == str(owner_id):
        return _reply(ident, "chủ nhóm từ chối qua Telegram")
    if f"approve:{p['env']}" not in (ident.get("perms") or ()):
        return _reply(ident, f"chưa có quyền approve:{p['env']}")
    card = board.card_update(group, card["id"], state="rejected", if_revision=card["revision"],
                             approver={"id": ident.get("fchat_id") or "", "name": ident.get("name") or ""},
                             result="bị từ chối" + (f": {render.one_line(reason, 200)}" if reason else ""))
    return _card(card) if card else _reply(ident, "thẻ vừa đổi trạng thái — thử lại")


def cancel(board, group: str, card_id: str, ident: dict):
    card = _get(board, group, card_id)
    if not card:
        return _reply(ident, f"không thấy thẻ {card_id.upper()} trong nhóm này")
    if str(card["payload"]["requester"]["id"]) != str(ident.get("fchat_id")):
        return _reply(ident, f"chỉ người yêu cầu mới huỷ được {card['id']}")
    if card["state"] != "waiting":
        return _reply(ident, f"{card['id']} không còn chờ duyệt — đã chạy thì không huỷ được qua chat")
    card = board.card_update(group, card["id"], state="cancelled", if_revision=card["revision"])
    return _card(card, "đã huỷ — chưa chạy gì") if card else _reply(ident, "thẻ vừa đổi trạng thái — thử lại")


# ---------------------------------------------------------------- runner
def _image_repo(ref: str) -> str:
    """'docker-pullable://reg:5000/a/b@sha256:…' / 'reg:5000/a/b:1.2' → 'reg:5000/a/b'."""
    ref = (ref or "").split("://", 1)[-1].split("@", 1)[0]
    head, _, tail = ref.rpartition(":")
    return head if head and "/" not in tail else ref


def rollout_state(workload: dict, container: str, pushes: list):
    """("done" | "rolling" | "error", detail). Done = the template points at the image repository the build pushed,
    and EXACTLY the desired number of pods exist, each running, ready, on (repository, digest) the build pushed."""
    if not isinstance(workload, dict) or workload.get("error"):
        return "error", (workload or {}).get("kind") or "không đọc được k8s"
    spec = workload.get("spec_images") or {}
    cname = container or next(iter(spec), "")
    pushed = {(_image_repo(x.get("image")), x.get("digest")) for x in pushes or [] if x.get("digest")}
    repos = {r for r, _ in pushed}
    if _image_repo(spec.get(cname)) not in repos:
        return "rolling", "template chưa trỏ image build vừa push"
    pods = workload.get("pods") or []
    desired = workload.get("replicas")
    if desired is None:
        return "error", "không đọc được số replica mong muốn"
    desired = int(desired)
    good = 0
    for pod in pods:
        cs = pod.get("containers") or []
        c = next((x for x in cs if x.get("name") == cname), {})
        ref = c.get("image_id") or ""
        pod_ready = pod.get("ready") if pod.get("ready") is not None else (bool(cs) and all(x.get("ready") for x in cs))
        if pod.get("phase") == "Running" and pod_ready and cs and all(x.get("ready") for x in cs) and \
                (_image_repo(ref), envmap._digest(ref)) in pushed:
            good += 1
    if desired and len(pods) == desired and good == desired:
        return "done", f"{good}/{desired} pod chạy đúng image build vừa push"
    return "rolling", f"{good}/{desired} pod chạy image mới (đang có {len(pods)} pod)"


def _lock_key(p):
    return f"deploylock:{p['repo']}:{p['env']}"


def _holder(group, card_id):
    return f"{group}|{card_id}"


def _take_lock(board, group, p, card_id) -> bool:
    """One deploy per repo:env across groups. A holder whose card is no longer running / uncertain (process died
    between finishing and releasing) is stale ⇒ taken over."""
    key, me = _lock_key(p), _holder(group, card_id)
    holder = board.meta_get(key)
    if holder and holder != me:
        g, _, c = str(holder).partition("|")
        hc = board.card_get(g, c) if c else {}
        if hc and hc.get("state") in ("running", "uncertain"):
            return False
    return board.meta_txn(lambda get, put: (put(key, me), True)[1] if get(key) in (None, holder, me) else False)


def _drop_lock(board, group, p, card_id):
    key, me = _lock_key(p), _holder(group, card_id)
    board.meta_txn(lambda get, put: put(key, None) if get(key) == me else None)


def _claim_build(board, job, number) -> bool:
    """A build number can belong to ONE deploy card (reconciliation never adopts a build twice)."""
    key = f"deploybuilds:{job}"

    def claim(get, put):
        taken = list(get(key, []) or [])
        if number in taken:
            return False
        put(key, (taken + [number])[-200:])
        return True
    return board.meta_txn(claim)


def step(board, group: str, card_id: str, ops, now: float = None, recheck=None) -> dict:
    """Advance one running card by ONE stage; {} when nothing to say. Each stage is written before its side effect.
    recheck(payload) -> reason ('' = still allowed): requester's permission + registry opt-in, asked right before the
    trigger (an approval may have waited for the repo:env lock)."""
    now = now or time.time()
    card = _get(board, group, card_id)
    if not card or card["state"] != "running":
        return {}
    p, stage, rev = card["payload"], card["payload"].get("stage"), card["revision"]

    def upd(state=None, **kw):
        return board.card_update(group, card["id"], state=state, if_revision=rev, **kw)

    def unsure(why):                                      # Jenkins may still run it: keep the lock, ask a human
        c = upd(state="uncertain", result=f"{why} — giữ khoá {p['repo']}:{p['env']}; kiểm Jenkins rồi chủ nhóm gõ "
                                          f"\"@bot mở khoá {card['id']}\"")
        return _card(c) if c else {}

    if stage == "trigger" and p.get("triggered_at"):      # an earlier attempt may have reached Jenkins: reconcile
        t0 = p["triggered_at"]
        builds = ops.builds(p["job"], 15)
        if builds is None:
            return unsure("mất phản hồi khi kích Jenkins, không đọc được danh sách build") if now > t0 + QUEUE_TIMEOUT else {}
        me = ops.jenkins_user()
        cands = [b for b in builds if (not me or me in (b.get("users") or []))
                 and t0 - RECONCILE_SLACK_S <= (b.get("timestamp") or 0) / 1000 <= t0 + QUEUE_TIMEOUT]
        taken = set(board.meta_get(f"deploybuilds:{p['job']}", []) or [])
        cands = [b for b in cands if b.get("number") not in taken]
        if len(cands) > 1:
            return unsure(f"mất phản hồi khi kích Jenkins và có {len(cands)} build cùng lúc — không biết build nào của thẻ này")
        if len(cands) == 1 and _claim_build(board, p["job"], cands[0]["number"]):
            b = cands[0]
            c = upd(stage="building", build=b["number"], build_url=b.get("url") or "")
            return _card(c, f"đối soát: build #{b['number']} là build duy nhất tài khoản {me or '?'} kích sau lúc bot kích "
                            f"— nếu ai đó cũng chạy tay cùng tài khoản lúc đó thì có thể nhầm, kiểm Jenkins nếu nghi") if c else {}
        return unsure("mất phản hồi khi kích Jenkins, không thấy build mới") if now > t0 + QUEUE_TIMEOUT else {}

    if stage == "trigger":
        # gate again RIGHT before the side effect: the approval may have waited for the lock
        if now - (p.get("approved_at") or now) > RUN_WITHIN_S:
            c = upd(state="waiting", stage=None, approval=None, approver={}, approved_at=None, tg=None,
                    expires_at=now + APPROVE_TTL)
            return _card(c, f"đã duyệt hơn 30′ mà chưa chạy (chờ deploy khác xong) — cần duyệt lại \"duyệt {card['id']}\"") if c else {}
        why = recheck(p) if recheck else ""
        if why:
            c = upd(state="cancelled", result=why)
            return _card(c) if c else {}
        head = ops.branch_head(p["project_id"], p["branch"])
        if not head:
            return {}                                     # GitLab hiccup: retry; RUN_WITHIN_S bounds the wait
        job_hash = ops.job_hash(p["job"])
        if not job_hash:
            return {}                                     # cannot verify the approved config: never trigger blind
        if job_hash != p.get("job_hash"):
            c = upd(state="cancelled", result="cấu hình job Jenkins đã đổi sau khi duyệt")
            return _card(c, "gõ \"deploy lại\" để duyệt theo cấu hình mới") if c else {}
        if head != p["commit"]:
            c = upd(state="waiting", stage=None, approval=None, approver={}, approved_at=None, tg=None, commit=head,
                    approval_hash=approval_hash({**p, "commit": head}), expires_at=now + APPROVE_TTL)
            return _card(c, f"HEAD nhánh {p['branch']} đổi sau khi duyệt ({p['commit'][:8]} → {head[:8]}) — lần duyệt "
                            f"trước không tính, cần duyệt lại \"duyệt {card['id']}\"") if c else {}
        if not _take_lock(board, group, p, card["id"]):
            return {}                                     # another deploy of repo:env is running: wait our turn
        c = upd(triggered_at=now)                         # durable BEFORE the side effect
        if not c:
            return {}
        rev = c["revision"]
        res = ops.trigger(p["job"])
        if res.get("error") and not res.get("uncertain"):
            _drop_lock(board, group, p, card["id"])
            c = upd(state="failed", result="Jenkins từ chối kích build: " + render.one_line(res.get("message"), 160))
            return _card(c) if c else {}
        if res.get("error"):
            return {}                                     # answer lost: the next step reconciles via builds()
        c = upd(stage="queued", queue_url=res.get("queue_url") or "")
        return _card(c, "đã kích Jenkins, đang chờ trong hàng đợi") if c else {}

    if stage == "queued":
        deadline = (p.get("triggered_at") or now) + QUEUE_TIMEOUT
        if not p.get("queue_url"):
            upd(stage="trigger")                          # no queue url: reconcile through the builds list
            return {}
        q = ops.queue(p["queue_url"])
        if q.get("error"):
            return unsure("không đọc được hàng đợi Jenkins quá 15′") if now > deadline else {}
        if q.get("cancelled"):
            _drop_lock(board, group, p, card["id"])
            c = upd(state="failed", result="build bị huỷ trong hàng đợi Jenkins")
            return _card(c) if c else {}
        if not q.get("number"):
            return unsure("quá 15′ vẫn trong hàng đợi Jenkins (" + render.one_line(q.get("why"), 100) + ")") \
                if now > deadline else {}
        if not _claim_build(board, p["job"], q["number"]):
            return unsure(f"build #{q['number']} đã được một thẻ deploy khác nhận")
        c = upd(stage="building", build=q["number"], build_url=q.get("url") or "")
        return _card(c, f"build #{q['number']} đang chạy") if c else {}

    if stage == "building":
        deadline = (p.get("triggered_at") or now) + BUILD_TIMEOUT
        b = ops.buildinfo(p["job"], p["build"])
        if b.get("error"):
            return unsure(f"không đọc được build #{p['build']} quá lâu") if now > deadline else {}
        if b.get("building"):
            if now > deadline and not p.get("slow_noted"):   # Jenkins still runs it: keep watching, say so once
                c = upd(slow_noted=True)
                return _card(c, f"build #{p['build']} chạy lâu bất thường (> 45′) — bot vẫn theo dõi") if c else {}
            return {}
        pushes, sha = b.get("pushes") or [], b.get("commit_sha") or ""
        failed = b.get("failed_stages")
        version = next((x.get("version") for x in pushes), "")
        if b.get("result") != "SUCCESS" and (failed or not pushes):
            _drop_lock(board, group, p, card["id"])
            where = f" ở bước {', '.join(failed)}" if failed else ""
            c = upd(state="failed", build_commit=sha, version=version,
                    result=f"build #{p['build']} {b.get('result') or '?'}{where}")
            return _card(c, "xem log Jenkins; sửa xong gõ \"deploy lại\"") if c else {}
        c = upd(stage="rollout", build_commit=sha, version=version, pushes=pushes, built_at=now,
                jenkins_result=b.get("result"))
        return _card(c, f"build #{p['build']} xong — chờ pod chạy bản mới") if c else {}

    if stage == "rollout":
        state, detail = rollout_state(ops.workload(p["repo"], p["env"], p.get("workload")), p.get("container"),
                                      p.get("pushes"))
        late = now - (p.get("built_at") or now) > ROLLOUT_TIMEOUT
        if state != "done" and not late:
            return {}
        _drop_lock(board, group, p, card["id"])           # Jenkins has finished: the env is free again
        note_j = (f" (Jenkins ghi {p['jenkins_result']} — lỗi ngoài các bước chính, xem log)"
                  if p.get("jenkins_result") not in (None, "SUCCESS") else "")
        if state == "done":
            if not p.get("build_commit"):
                c = upd(state="built", result=detail + note_j + " — build không in commit SHA: không đối chiếu được "
                                                                "với commit đã duyệt")
                return _card(c) if c else {}
            if p.get("commit") and p["build_commit"] != p["commit"]:
                c = upd(state="mismatch", result=detail + note_j + f" — ⚠ build khác commit đã duyệt "
                                                                   f"({p['commit'][:8]} → {p['build_commit'][:8]}): HEAD nhánh đổi lúc chờ")
                return _card(c, "báo người duyệt kiểm commit build thật") if c else {}
            c = upd(state="done", result=detail + note_j)
            return _card(c) if c else {}
        if state == "error":
            c = upd(state="built", result=f"build xong — CHƯA xác minh rollout (không đọc được k8s: {detail})")
            return _card(c) if c else {}
        c = upd(state="failed", result=f"15′ sau build vẫn chưa thấy pod chạy bản mới ({detail})")
        return _card(c, "kiểm k8s / bước set image của job") if c else {}
    return {}


def unlock(board, group: str, card_id: str, ident: dict):
    """@bot mở khoá D-n (chủ nhóm / admin) — sau khi tự kiểm Jenkins, cho thẻ "không chắc" thả khoá repo:env."""
    card = _get(board, group, card_id)
    if not card:
        return _reply(ident, f"không thấy thẻ {card_id.upper()} trong nhóm này")
    if "admin" not in (ident.get("perms") or ()):
        return _reply(ident, "chỉ chủ nhóm mở khoá được")
    if card["state"] != "uncertain":
        return _reply(ident, f"{card['id']} không ở trạng thái không chắc — không cần mở khoá")
    _drop_lock(board, group, card["payload"], card["id"])
    c = board.card_update(group, card["id"], state="error", if_revision=card["revision"],
                          result=f"chủ nhóm đã kiểm Jenkins và mở khoá {card['payload']['repo']}:{card['payload']['env']}")
    return _card(c) if c else _reply(ident, "thẻ vừa đổi trạng thái — thử lại")


def redo(board, group: str, card_id: str, ident: dict, target_of, ops, approvers=(), owner_name: str = "",
         now: float = None):
    """deploy lại D-n (requester; card finished) ⇒ a NEW request for the same repo/env, a new approval."""
    card = _get(board, group, card_id)
    if not card:
        return _reply(ident, f"không thấy thẻ {card_id.upper()} trong nhóm này")
    p = card["payload"]
    if str(p["requester"]["id"]) != str(ident.get("fchat_id")):
        return _reply(ident, f"chỉ người yêu cầu {card['id']} mới deploy lại được")
    if card["state"] in LIVE:
        return _reply(ident, f"{card['id']} chưa xong")
    target, why = target_of(p["repo"], p["env"])
    if why:
        return _reply(ident, why)
    return request(board, group, ident, target, p.get("reason") or "", ops, approvers, owner_name, now)


def parse_reason(rest: str) -> str:
    m = re.match(r"^\s*\|\s*(?:lý do|ly do|reason)\s*:\s*(.+)$", rest or "", re.I | re.S)
    return render.one_line(m.group(1), 200) if m else ""
