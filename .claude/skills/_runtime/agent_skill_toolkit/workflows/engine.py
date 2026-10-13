"""Checkpointed step engine — "resume from the exact step" (design §7, §B.4-M5).

A workflow is an ordered list of named steps. Each step is a function
    step(ctx: StepContext) -> dict | None
whose return value is saved as that step's checkpoint. On the next run of the same task
(after a quota pause, a crash, a reboot, or by a different backend/account) completed steps are
skipped and their saved outputs are available in ctx.outputs.

"Exact step" means the last DURABLE checkpoint: a model call cut off mid-turn is re-run from
its saved inputs (design §B.4). Steps must therefore be idempotent — check before you create.

Raise from a step:
    QuotaExceeded(resume_at, backend, reason)  -> task paused_quota, resumed later, no attempt used
    NeedsHuman(reason)                          -> task blocked
    any other exception                         -> attempt counted; requeued or failed
"""

import time
import traceback

from ..core import store as store_mod
from .execution import handoff, quota

DONE = "__done__"


class QuotaExceeded(Exception):
    def __init__(self, resume_at: float, backend: str = "", reason: str = ""):
        super().__init__(reason or "provider limit")
        self.resume_at, self.backend, self.reason = float(resume_at), backend, reason


class NeedsHuman(Exception):
    pass


class StepContext:
    def __init__(self, st: store_mod.Store, task: dict, owner: str, outputs: dict, lease_s: int):
        self.store, self.task, self.owner, self.outputs, self.lease_s = st, task, owner, outputs, lease_s
        self.step = ""

    @property
    def data(self) -> dict:
        return self.task.get("data") or {}

    def heartbeat(self):
        self.store.heartbeat(self.task["id"], self.owner, self.lease_s)

    def handoff(self, **sections):
        return handoff.write(self.task["id"], sections)

    def usage(self, backend: str, model: str = "", tokens_in=None, tokens_out=None, seconds=None):
        self.store.record_usage(self.task["id"], self.step, backend, model, tokens_in, tokens_out, seconds)

    def raise_if_quota(self, output: str, backend: str = ""):
        """Turn an agent failure text into QuotaExceeded when it is a provider limit."""
        c = quota.classify(output)
        if c["kind"] == "quota":
            raise QuotaExceeded(c["resume_at"], backend, f"{c['limit']}: {output.strip()[-200:]}")


def completed_steps(st: store_mod.Store, task_id: str) -> dict:
    """{step: output} for every checkpointed step (latest wins)."""
    out = {}
    for cp in st.checkpoints(task_id):
        out[cp["step"]] = cp["data"]
    return out


def run(st: store_mod.Store, task: dict, owner: str, steps: list, lease_s: int = store_mod.DEFAULT_LEASE_S) -> dict:
    """Run `task` (already claimed by `owner`) through `steps` = [(name, fn), ...].
    Returns {"status": done|paused_quota|blocked|queued|failed, "step": last step, ...}."""
    tid = task["id"]
    outputs = completed_steps(st, tid)
    ctx = StepContext(st, task, owner, outputs, lease_s)
    for name, fn in steps:
        if name in outputs:
            continue
        ctx.step = name
        t0 = time.time()
        try:
            st.heartbeat(tid, owner, lease_s)
            result = fn(ctx) or {}
        except QuotaExceeded as q:
            st.pause_quota(tid, owner, q.resume_at, q.backend, q.reason)
            return {"status": "paused_quota", "step": name, "resume_at": q.resume_at}
        except NeedsHuman as h:
            st.block(tid, owner, str(h))
            return {"status": "blocked", "step": name, "reason": str(h)}
        except store_mod.LeaseLost:
            return {"status": "lease_lost", "step": name}
        except Exception as e:  # noqa: BLE001 — every other failure counts an attempt
            status = st.fail(tid, owner, f"{name}: {type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")
            return {"status": status, "step": name, "error": f"{type(e).__name__}: {e}"}
        outputs[name] = result
        st.checkpoint(tid, owner, name, result, sha=str(result.get("sha", "")) if isinstance(result, dict) else "")
        st.record_usage(tid, name, "step", seconds=round(time.time() - t0, 3))
    st.complete(tid, owner)
    return {"status": "done", "step": steps[-1][0] if steps else "", "outputs": outputs}


def work_once(st: store_mod.Store, owner: str, registry: dict, kinds: tuple = (),
              lease_s: int = store_mod.DEFAULT_LEASE_S) -> dict:
    """Claim one runnable task whose kind is in `registry` {kind: steps} and run it."""
    task = st.claim(owner, kinds or tuple(registry), lease_s)
    if not task:
        return {"status": "idle"}
    steps = registry.get(task["kind"])
    if steps is None:
        st.block(task["id"], owner, f"no workflow registered for kind {task['kind']!r}")
        return {"status": "blocked", "task": task["id"]}
    return {"task": task["id"], **run(st, task, owner, steps, lease_s)}
