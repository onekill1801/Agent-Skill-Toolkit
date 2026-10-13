#!/usr/bin/env python3
"""Reusable headless-agent runner — one turn in, raw text out.

Several pipeline steps (triage, grounding, pre-MR review) need the same thing the
debate engine already does: invoke a subscription CLI agent (or the Claude API)
headlessly and capture its stdout. That logic used to live tangled inside
`debate_engine.CliBackend`; this module factors out the single-turn primitive so
those steps don't each re-implement subprocess wrangling.

Backends (no ANTHROPIC_API_KEY needed except `api`):
    claude   ->  claude -p --model <m> --dangerously-skip-permissions   (stdin prompt)
    cursor   ->  agent  -p --output-format text --model <m> --force --trust (arg prompt)
    agy      ->  agy    --model <m> --print --dangerously-skip-permissions
                 (WARNING: agy does not run headless reliably — see debate_engine notes)
    codex    ->  codex exec -m <m> -s workspace-write --skip-git-repo-check -   (stdin prompt)
    custom   ->  your own argv via cmd_template "<cmd> {model}"
    api      ->  Claude API via urllib (this one needs ANTHROPIC_API_KEY)
    dry-run  ->  returns dry_run_text, calls nothing (for tests)

Stdlib only.

Usage (CLI, mostly for smoke tests):
    python agent_runner.py --backend dry-run --prompt "hi" --dry-run-text "<x>ok</x>"
    echo "review this diff" | python agent_runner.py --backend claude
    echo "review this diff" | python agent_runner.py --role review --avoid codex

Roles (work/agents.json — gitignored, machine-specific; see agents.sample.json):
    run_role(prompt, role) tries each "backend:model@account" of the role in order,
    skipping a backend in `avoid`, an account with no free slot (limits.max_parallel),
    and moving on when the CLI hits a usage/rate limit or an auth error. Accounts map to
    per-account config dirs via env (claude: CLAUDE_CONFIG_DIR, codex: CODEX_HOME).
Every agent started here gets TOOLKIT_HEADLESS=1 (the PreToolUse guard then DENIES
outward-facing writes instead of asking a human who isn't there).
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Reuse the repo's .env loader + UTF-8 stdout fix (config lives in dev-automation/tools).
_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS = os.path.dirname(os.path.dirname(_HERE))  # .../.claude/skills
sys.path.insert(0, os.path.join(_SKILLS, "dev-automation", "tools"))
import config  # noqa: E402
import runpy  # noqa: E402

runpy.run_path(os.path.join(_SKILLS, "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import proc as proc_mod  # noqa: E402
from agent_skill_toolkit.core import winexec  # noqa: E402
from agent_skill_toolkit.workflows.execution import quota  # noqa: E402
from agent_skill_toolkit.workflows.execution import usage as usage_mod  # noqa: E402

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_API_MODEL = config.get("ANTHROPIC_MODEL") or "claude-opus-5-5"
DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MAX_TOKENS = 4000
CLI_TIMEOUT = 600
API_TIMEOUT = 300

# argv templates per CLI; {model} is replaced by the model (the --model pair is
# dropped entirely when no model is given). Flags verified against each CLI --help.
CLI_PRESETS = {
    "claude": {
        "argv": ["claude", "-p", "--model", "{model}", "--dangerously-skip-permissions"],
        "default_model": "sonnet",
        "prompt_via": "stdin",
    },
    "cursor": {
        "argv": ["agent", "-p", "--output-format", "text", "--model", "{model}",
                 "--force", "--trust"],
        "default_model": None,
        "prompt_via": "arg",
    },
    "codex": {
        "argv": ["codex", "exec", "-m", "{model}", "-s", "workspace-write",
                 "--skip-git-repo-check", "-"],
        "default_model": None,
        "prompt_via": "stdin",
    },
    "agy": {
        "argv": ["agy", "--model", "{model}", "--print", "--dangerously-skip-permissions"],
        "default_model": "gemini-3-pro-preview",
        "prompt_via": "arg",
    },
}


class AgentRunError(Exception):
    """Unrecoverable error invoking the agent (missing CLI, timeout, refusal, ...)."""

    def __init__(self, message: str, classification: dict = None):
        Exception.__init__(self, message)   # not super(): AgentQuotaError's MRO continues into QuotaExceeded
        self.classification = classification or quota.classify(message)


class AgentQuotaError(AgentRunError, quota.QuotaExceeded):
    """The provider refused for a limit (usage / rate / overloaded). Also a
    quota.QuotaExceeded, so the step engine PAUSES the task (no attempt used) until resume_at."""

    def __init__(self, message: str, classification: dict = None, backend: str = ""):
        AgentRunError.__init__(self, message, classification)
        self.resume_at = float(self.classification.get("resume_at") or 0)
        self.backend, self.reason = backend, message


def _cli_failure(message: str, output: str) -> AgentRunError:
    c = quota.classify(output)
    return AgentQuotaError(message, c) if c["kind"] == "quota" else AgentRunError(message, c)


def _resolve_exec(argv: list) -> list:
    """Make argv[0] directly executable across OSes — never through cmd.exe with untrusted
    arguments (a prompt carries MR content; cmd.exe would re-parse it). Windows .cmd/.bat
    launchers are resolved to what they run (node + script, powershell -File x.ps1, sibling
    .exe); see agent_skill_toolkit.core.winexec. No-op on macOS/Linux."""
    if not argv or not shutil.which(argv[0]):
        return argv  # let subprocess raise FileNotFoundError with the original name
    try:
        return winexec.command(argv)
    except winexec.UnsafeLauncher as e:
        raise AgentRunError(str(e), {"kind": "error", "retry_other_backend": True})


# Credentials of the systems the TOOLKIT talks to. An agent we start reads untrusted content
# (MR diffs, chat text), so it does not get them (Codex review F.4b #1 — interim until the
# isolated executor, design §10.2; the agent CLIs keep their OWN auth: config dirs / API key).
_SECRET_ENV = ("GITLAB_PRIVATE_TOKEN", "GITLAB_TOKEN", "AZURE_DEVOPS_PAT", "ATASK_PAT_TOKEN",
               "ATASK_BEARER_TOKEN", "FCHAT_TOKEN", "FCHAT_BEARER_TOKEN", "TELEGRAM_BOT_TOKEN",
               "TELEGRAM_APPROVAL_BOT_TOKEN", "JENKINS_TOKEN", "JENKINS_API_TOKEN", "DB_PASSWORD",
               "FORTIVPN_PASSWORD", "KUBECONFIG_PROD")
_SECRET_RX = None


def agent_env(env_extra: dict = None) -> dict:
    """Environment for an agent subprocess: headless guard on, toolkit service secrets removed."""
    import re
    global _SECRET_RX
    if _SECRET_RX is None:
        _SECRET_RX = re.compile(r"(TOKEN|_PAT|PASSWORD|PASSWD|SECRET|PRIVATE_KEY|COOKIE|SESSION)", re.I)
    keep = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}  # agent's own auth
    env = {k: v for k, v in os.environ.items()
           if k in keep or not (k in _SECRET_ENV or _SECRET_RX.search(k))}
    env.update(env_extra or {})
    env["TOOLKIT_HEADLESS"] = "1"
    env["TOOLKIT_AGENT_SANDBOXED"] = "1"     # marker: toolkit tools may refuse writes when set
    return env


def _combine_prompt(system: str | None, prompt: str) -> str:
    """CLI print-mode has no separate system slot → prepend the system framing."""
    if not system:
        return prompt
    return f"{system}\n\n=== REQUEST ===\n{prompt}"


def _build_argv(argv_tmpl: list, model: str, prompt: str, prompt_via: str) -> list:
    argv = []
    for tok in argv_tmpl:
        if tok == "{model}":
            if not model:
                if argv and argv[-1] in ("--model", "-m"):
                    argv.pop()
                continue
            argv.append(model)
        else:
            argv.append(tok)
    if prompt_via == "arg":
        argv.append(prompt)
    return argv


def _run_cli(prompt, system, backend, model, cmd_template, prompt_via, timeout,
             cwd=None, env_extra=None) -> str:
    if cmd_template:
        argv_tmpl = shlex.split(cmd_template)
        default_model = None
        prompt_via = prompt_via or "stdin"
    elif backend in CLI_PRESETS:
        preset = CLI_PRESETS[backend]
        argv_tmpl = list(preset["argv"])
        default_model = preset["default_model"]
        prompt_via = prompt_via or preset["prompt_via"]
    else:
        raise AgentRunError(f"unknown CLI backend '{backend}'; pass cmd_template instead.")

    full_prompt = _combine_prompt(system, prompt)
    argv = _resolve_exec(_build_argv(argv_tmpl, model or default_model or "",
                                     full_prompt, prompt_via))
    stdin_data = full_prompt if prompt_via == "stdin" else None
    try:
        env = agent_env(env_extra)
        # proc.run kills the whole tree on timeout: a CLI launcher's agent child must not keep
        # editing files after we gave up (Codex review F.4b #5)
        proc = proc_mod.run(argv, input=stdin_data, timeout=timeout, cwd=cwd, env=env)
    except FileNotFoundError:
        raise AgentRunError(
            f"CLI '{argv[0]}' not found on PATH. Install it, change backend, or pass "
            f"cmd_template.")
    except subprocess.TimeoutExpired:
        raise AgentRunError(f"CLI '{argv[0]}' did not respond within {timeout}s.")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise _cli_failure(f"CLI '{argv[0]}' exit {proc.returncode}: {tail}",
                           (proc.stdout or "") + "\n" + (proc.stderr or ""))
    out = (proc.stdout or "").strip()
    if not out:
        raise _cli_failure(f"CLI '{argv[0]}' returned empty stdout.", proc.stderr or "")
    return out


def _run_api(prompt, system, model, max_tokens, timeout) -> str:
    api_key = config.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise AgentRunError("backend 'api' needs ANTHROPIC_API_KEY; use --backend claude "
                            "(CLI subscription) or --backend dry-run.")
    base_url = config.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL
    payload = {"model": model or DEFAULT_API_MODEL, "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": prompt}]}
    if system:
        payload["system"] = system
    req = urllib.request.Request(base_url.rstrip("/") + "/v1/messages",
                                 data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("x-api-key", api_key)
    req.add_header("anthropic-version", ANTHROPIC_VERSION)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        hint = f" retry-after: {e.headers.get('retry-after')}" if e.headers.get("retry-after") else ""
        raise _cli_failure(f"Claude API HTTP {e.code}: {body}", f"HTTP {e.code} {body}{hint}")
    except urllib.error.URLError as e:
        raise AgentRunError(f"network error calling Claude API: {e.reason}")
    if data.get("stop_reason") == "refusal":
        raise AgentRunError(f"model refusal: {data.get('stop_details')}")
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


def run_turn(prompt: str, *, system: str = None, backend: str = "claude",
             model: str = None, cmd_template: str = None, prompt_via: str = None,
             timeout: int = CLI_TIMEOUT, max_tokens: int = DEFAULT_MAX_TOKENS,
             dry_run_text: str = None, cwd: str = None, env_extra: dict = None) -> str:
    """Run one headless agent turn; return raw stdout text. Raises AgentRunError.

    `dry_run_text` (or backend='dry-run') short-circuits to a canned response so
    callers and their tests can exercise the parsing path without a real agent.
    `cwd` runs the CLI agent inside that directory — required when the agent must
    EDIT files there (e.g. fix_loop repairing code in a project clone).
    """
    if backend == "dry-run" or dry_run_text is not None:
        if dry_run_text is None:
            raise AgentRunError("backend 'dry-run' requires dry_run_text.")
        return dry_run_text
    if backend == "api":
        return _run_api(prompt, system, model, max_tokens, timeout)
    return _run_cli(prompt, system, backend, model, cmd_template, prompt_via, timeout,
                    cwd=cwd, env_extra=env_extra)


# --------------------------------------------------------------------------- role routing

def _agents_config() -> dict:
    work = os.environ.get("WORK_DIR") or os.path.join(os.path.dirname(_SKILLS.rstrip(os.sep)), "..", "work")
    for path in (os.path.join(work, "agents.json"), os.path.join(_HERE, "agents.sample.json")):
        try:
            with open(os.path.normpath(path), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            continue
    return {}


def parse_candidate(spec: str) -> dict:
    """'codex:gpt-5.x@main' -> {backend, model, account}; model/account optional."""
    head, _, account = spec.partition("@")
    backend, _, model = head.partition(":")
    return {"backend": backend.strip(), "model": model.strip() or None,
            "account": account.strip() or None, "spec": spec}


def _account_env(cfg: dict, backend: str, account: str) -> dict:
    acct = ((cfg.get("accounts") or {}).get(backend) or {}).get(account or "") or {}
    return {k: os.path.expanduser(str(v)) for k, v in (acct.get("env") or {}).items()}


class _Slot:
    """Cross-process slot for one account (O_EXCL lock files) — caps parallel sessions."""

    def __init__(self, key: str, limit: int):
        self.key, self.limit, self.path = key, max(1, limit), None

    def __enter__(self):
        d = os.path.join(os.path.dirname(_SKILLS.rstrip(os.sep)), "..", "temp", "agent_slots")
        os.makedirs(d, exist_ok=True)
        for i in range(self.limit):
            p = os.path.join(d, f"{self.key.replace('/', '_')}.{i}.lock")
            try:
                if os.path.exists(p) and time.time() - os.path.getmtime(p) > CLI_TIMEOUT * 3:
                    os.remove(p)                      # stale slot from a crashed run
                fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.path = p
                return self
            except FileExistsError:
                continue
        return None

    def __exit__(self, *exc):
        if self.path:
            try:
                os.remove(self.path)
            except OSError:
                pass


def usage_tier() -> str:
    """Current throttling tier (F.11): TOOLKIT_USAGE_TIER overrides; else the level reported to the
    store (stale / missing / unreadable store = normal — throttling never blocks an agent turn)."""
    forced = os.environ.get("TOOLKIT_USAGE_TIER", "").strip().lower()
    if forced in ("normal", "save", "critical"):
        return forced
    try:
        from agent_skill_toolkit.core import store as store_mod
        if not os.path.exists(store_mod.default_path()):
            return "normal"
        with store_mod.Store() as st:
            return usage_mod.level(st)["tier"]
    except Exception:  # noqa: BLE001
        return "normal"


def run_role(prompt: str, role: str, *, system: str = None, cwd: str = None,
             timeout: int = CLI_TIMEOUT, avoid: tuple = ()) -> dict:
    """Run one turn for `role` using the first usable candidate. Returns
    {"text", "used": spec, "tried": [...]}; raises AgentRunError if all fail."""
    cfg = _agents_config()
    roles = cfg.get("roles") or {}
    specs = roles.get(role) or roles.get("default") or ["claude"]
    if isinstance(specs, str):
        specs = [specs]
    tried = []
    tier = usage_tier()
    for spec in specs:
        c = parse_candidate(spec)
        if c["backend"] == "claude" and c["model"]:
            down = usage_mod.model_for(role, c["model"], tier)       # model per step under throttling
            if down != c["model"]:
                tried.append({"spec": spec, "model": f"{c['model']} -> {down} ({tier})"})
                c["model"] = down
        if c["backend"] in avoid:
            tried.append({"spec": spec, "skipped": "avoided"})
            continue
        key = f"{c['backend']}@{c['account'] or 'default'}"
        limit = int(((cfg.get("limits") or {}).get(key) or {}).get("max_parallel", 99))
        slot = _Slot(key, limit)
        if slot.__enter__() is None:
            tried.append({"spec": spec, "skipped": "no free slot"})
            continue
        try:
            text = run_turn(prompt, system=system, backend=c["backend"], model=c["model"],
                            timeout=timeout, cwd=cwd,
                            env_extra=_account_env(cfg, c["backend"], c["account"]))
            return {"text": text, "used": spec, "tried": tried}
        except AgentRunError as e:
            msg = str(e)
            c = e.classification
            tried.append({"spec": spec, "error": msg[:200], "kind": c.get("kind"),
                          "resume_at": c.get("resume_at")})
            if not c.get("retry_other_backend"):
                raise AgentRunError(f"{spec}: {msg}", c)
        finally:
            slot.__exit__()
    quotas = [t["resume_at"] for t in tried if t.get("kind") == "quota" and t.get("resume_at")]
    if quotas:                       # every candidate is limited: pause until the EARLIEST reset
        raise AgentQuotaError(f"all candidates for role '{role}' are rate/usage limited: {tried}",
                              {"kind": "quota", "resume_at": min(quotas), "retry_other_backend": False})
    raise AgentRunError(f"no candidate could run role '{role}': {tried}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one headless agent turn, print raw text.")
    ap.add_argument("--prompt", default=None, help="prompt text (else read from stdin)")
    ap.add_argument("--system", default=None)
    ap.add_argument("--backend", default="claude",
                    choices=["claude", "codex", "cursor", "agy", "custom", "api", "dry-run"])
    ap.add_argument("--role", default=None, help="route via work/agents.json roles")
    ap.add_argument("--avoid", default="", help="comma list of backends to skip (with --role)")
    ap.add_argument("--cmd-template", default=None, help="for --backend custom; may contain {model}")
    ap.add_argument("--prompt-via", default=None, choices=[None, "stdin", "arg"])
    ap.add_argument("--model", default=config.get("ANTHROPIC_MODEL") or None)
    ap.add_argument("--timeout", type=int, default=CLI_TIMEOUT)
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--dry-run-text", default=None)
    ap.add_argument("--cwd", default=None, help="run the agent inside this directory")
    args = ap.parse_args()

    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    backend = "custom" if args.cmd_template else args.backend

    def _err(e: AgentRunError) -> int:
        c = e.classification or {}
        # kind/resume_at let a calling workflow PAUSE on a provider limit instead of failing
        print(json.dumps({"error": True, "message": str(e), "kind": c.get("kind"),
                          "resume_at": c.get("resume_at")}, ensure_ascii=False))
        return 1
    if args.role:
        try:
            res = run_role(prompt, args.role, system=args.system, timeout=args.timeout, cwd=args.cwd,
                           avoid=tuple(x for x in args.avoid.split(",") if x))
        except AgentRunError as e:
            return _err(e)
        print(json.dumps({"used": res["used"], "tried": res["tried"]}, ensure_ascii=False), file=sys.stderr)
        sys.stdout.write(res["text"].rstrip("\n") + "\n")
        return 0
    try:
        out = run_turn(prompt, system=args.system, backend=backend, model=args.model,
                       cmd_template=args.cmd_template, prompt_via=args.prompt_via,
                       timeout=args.timeout, max_tokens=args.max_tokens,
                       dry_run_text=args.dry_run_text, cwd=args.cwd)
    except AgentRunError as e:
        return _err(e)
    sys.stdout.write(out)
    if not out.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
