"""Checkpointed step engine — "resume from the exact step" (design §7, §B.4-M5).

A workflow is an ordered list of steps:
    (name, fn)            or    (name, fn, key_fn)
    fn(ctx) -> dict | None      its return value is saved as the step's checkpoint
    key_fn(ctx) -> str          identity of the step's INPUTS (e.g. the MR head SHA, the worktree
                                HEAD). A saved checkpoint is reused only while its key still matches.

On the next run of the same task (after a quota pause, crash, reboot, or by another backend /
account) steps whose checkpoint is valid and whose key matches are skipped, outputs in ctx.outputs.
Once a step actually runs, every LATER step's checkpoint is invalidated and they run again —
a repair after a green build means the build/test steps run again on the new code.

"Exact step" = the last DURABLE checkpoint: a model call cut off mid-turn is re-run from its
saved inputs (§B.4). Steps must be idempotent — check before you create.

While a step runs, a background heartbeat keeps the lease alive; if the lease is lost anyway,
ctx.should_stop() turns True and the step's result is discarded (never checkpointed).

Raise from a step:
    quota.QuotaExceeded (incl. agent_runner.AgentQuotaError) -> paused_quota, no attempt used
    NeedsHuman(reason)                                        -> blocked
    Retry(reason)                                             -> back to queued now, no attempt used
    any other exception                                       -> attempt counted; requeued or failed
"""

import os
import threading
import time
import traceback

from ..core import proc
from ..core import store as store_mod
from .execution import handoff, quota

QuotaExceeded = quota.QuotaExceeded          # one exception type for runners and engine


class NeedsHuman(Exception):
    pass


class Retry(Exception):
    """Not a failure: put the task back in the queue now (no attempt used), e.g. the MR got a new
    revision while we worked. Raise after invalidating whatever must be redone."""


class StepContext:
    def __init__(self, st: store_mod.Store, task: dict, outputs: dict, lease_s: int):
        self.store, self.task, self.outputs, self.lease_s = st, task, outputs, lease_s
        self.lease = task["lease_token"]
        self.step = ""
        self._lost = threading.Event()

    @property
    def data(self) -> dict:
        return self.task.get("data") or {}

    def should_stop(self) -> bool:
        """True once the lease is lost — a long step should stop and return."""
        return self._lost.is_set()

    def run(self, argv: list, timeout: float = 3600, poll: float = 0.5, input: str = None, **kw):
        """Run a subprocess for this step and KILL ITS TREE if the lease is lost (pure-Python work can
        only stop cooperatively via should_stop(); builds/tests/agents should go through here).
        Returns CompletedProcess; raises LeaseLost when killed for losing the lease."""
        # the task / step travel with every subprocess: agent_runner charges its turns to this task's
        # budget (F.11) whoever started it
        env = dict(kw.get("env") or os.environ, TOOLKIT_TASK_ID=self.task["id"], TOOLKIT_STEP=self.step or "")
        try:
            return proc.run(argv, input=input, timeout=timeout, should_stop=self.should_stop, poll=poll,
                            cwd=kw.get("cwd"), env=env)
        except InterruptedError:
            raise store_mod.LeaseLost(f"task {self.task['id']}: lease lost, killed {argv[0]}")

    def handoff(self, **sections):
        return handoff.write(self.task["id"], sections)

    def usage(self, backend: str, model: str = "", tokens_in=None, tokens_out=None, seconds=None):
        self.store.record_usage(self.task["id"], self.step, backend, model, tokens_in, tokens_out, seconds)

    def raise_if_quota(self, output: str, backend: str = ""):
        """Turn an agent failure text into QuotaExceeded when it is a provider limit."""
        c = quota.classify(output)
        if c["kind"] == "quota":
            raise QuotaExceeded(c["resume_at"], backend, f"{c['limit']}: {output.strip()[-200:]}")


kill_tree = proc.kill_tree        # re-exported: callers and tests use engine.kill_tree


class _Heartbeat:
    """Renews the lease from a separate connection while a step blocks the main thread."""

    def __init__(self, path: str, task_id: str, lease: str, lease_s: int, lost: threading.Event):
        self.args, self.lost = (path, task_id, lease, lease_s), lost
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        path, task_id, lease, lease_s = self.args
        interval = max(0.2, lease_s / 3)
        with store_mod.Store(path) as st:
            while not self.stop.wait(interval):
                try:
                    st.heartbeat(task_id, lease, lease_s)
                except store_mod.LeaseLost:
                    self.lost.set()
                    return
                except Exception:  # noqa: BLE001 — a transient DB error: try again next tick
                    continue

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=5)


def _step(spec):
    name, fn = spec[0], spec[1]
    return name, fn, (spec[2] if len(spec) > 2 else None)


def reusable_outputs(st: store_mod.Store, task_id: str) -> dict:
    """{step: checkpoint} — latest VALID checkpoint per step."""
    out = {}
    for cp in st.checkpoints(task_id, valid_only=True):
        out[cp["step"]] = cp
    return out


def run(st: store_mod.Store, task: dict, steps: list, lease_s: int = store_mod.DEFAULT_LEASE_S) -> dict:
    """Run a CLAIMED `task` (it carries lease_token) through `steps`.
    Returns {"status": done|paused_quota|blocked|queued|failed|lease_lost, "step": ...}."""
    tid = task["id"]
    saved = reusable_outputs(st, tid)
    outputs = {}
    ctx = StepContext(st, task, outputs, lease_s)
    names = [_step(s)[0] for s in steps]
    rerun_from = None
    try:
        for i, spec in enumerate(steps):
            name, fn, key_fn = _step(spec)
            ctx.step = name
            t0 = time.time()
            try:
                st.heartbeat(tid, ctx.lease, lease_s)
                with _Heartbeat(st.path, tid, ctx.lease, lease_s, ctx._lost):
                    # the input key may itself block or fail (e.g. reading the MR head from GitLab):
                    # same lease renewal and the same error handling as the step
                    key = str(key_fn(ctx)) if key_fn else ""
                    cp = saved.get(name)
                    if rerun_from is None and cp and (cp.get("input_key") or "") == key:
                        outputs[name] = cp["data"]
                        continue
                    if rerun_from is None:
                        rerun_from = name
                        later = names[i + 1:]
                        if later:
                            st.invalidate(tid, later, reason=f"{name} re-ran")
                    result = fn(ctx) or {}
            except QuotaExceeded as q:
                st.pause_quota(tid, ctx.lease, q.resume_at, q.backend, q.reason or str(q))
                return {"status": "paused_quota", "step": name, "resume_at": q.resume_at}
            except NeedsHuman as h:
                st.block(tid, ctx.lease, str(h))
                return {"status": "blocked", "step": name, "reason": str(h)}
            except Retry as r:
                st.release(tid, ctx.lease)
                return {"status": "queued", "step": name, "reason": str(r)}
            except store_mod.LeaseLost:
                raise
            except Exception as e:  # noqa: BLE001 — every other failure counts an attempt
                status = st.fail(tid, ctx.lease, f"{name}: {type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")
                return {"status": status, "step": name, "error": f"{type(e).__name__}: {e}"}
            if ctx.should_stop():
                raise store_mod.LeaseLost(f"task {tid}: lease lost during {name}")
            outputs[name] = result
            sha = str(result.get("sha", "")) if isinstance(result, dict) else ""
            st.checkpoint(tid, ctx.lease, name, result, sha=sha, input_key=key)
            st.record_usage(tid, name, "step", seconds=round(time.time() - t0, 3))
        st.complete(tid, ctx.lease)
    except store_mod.LeaseLost as e:
        return {"status": "lease_lost", "step": ctx.step, "error": str(e)}
    return {"status": "done", "step": names[-1] if names else "", "outputs": outputs}


def work_once(st: store_mod.Store, owner: str, registry: dict, kinds: tuple = (),
              lease_s: int = store_mod.DEFAULT_LEASE_S) -> dict:
    """Claim one runnable task whose kind is in `registry` {kind: steps} and run it (the shared
    capacity of the store applies)."""
    task = st.claim(owner, kinds or tuple(registry), lease_s)
    if not task:
        return {"status": "idle"}
    steps = registry.get(task["kind"])
    if callable(steps):                    # per-task steps (e.g. a ladder built from the repo's spec)
        try:
            steps = steps(task)
        except Exception as e:  # noqa: BLE001 — bad spec / repo: a human must look
            try:
                st.block(task["id"], task["lease_token"], f"cannot build steps: {type(e).__name__}: {e}")
            except store_mod.LeaseLost:
                return {"status": "lease_lost", "task": task["id"]}
            return {"status": "blocked", "task": task["id"]}
    if steps is None:
        try:
            st.block(task["id"], task["lease_token"], f"no workflow registered for kind {task['kind']!r}")
        except store_mod.LeaseLost:
            return {"status": "lease_lost", "task": task["id"]}
        return {"status": "blocked", "task": task["id"]}
    return {"task": task["id"], **run(st, task, steps, lease_s)}
