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
    custom   ->  your own argv via cmd_template "<cmd> {model}"
    api      ->  Claude API via urllib (this one needs ANTHROPIC_API_KEY)
    dry-run  ->  returns dry_run_text, calls nothing (for tests)

Stdlib only.

Usage (CLI, mostly for smoke tests):
    python agent_runner.py --backend dry-run --prompt "hi" --dry-run-text "<x>ok</x>"
    echo "review this diff" | python agent_runner.py --backend claude
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

# Reuse the repo's .env loader + UTF-8 stdout fix (config lives in dev-automation/tools).
_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS = os.path.dirname(os.path.dirname(_HERE))  # .../.claude/skills
sys.path.insert(0, os.path.join(_SKILLS, "dev-automation", "tools"))
import config  # noqa: E402

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_API_MODEL = "claude-opus-4-8"
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
    "agy": {
        "argv": ["agy", "--model", "{model}", "--print", "--dangerously-skip-permissions"],
        "default_model": "gemini-3-pro-preview",
        "prompt_via": "arg",
    },
}


class AgentRunError(Exception):
    """Unrecoverable error invoking the agent (missing CLI, timeout, refusal, ...)."""


def _resolve_exec(argv: list) -> list:
    """Make argv[0] directly executable across OSes.

    On Windows a CLI is often a .cmd/.bat launcher (e.g. Cursor `agent.cmd`) or a
    .ps1; subprocess(shell=False) can't exec those directly, so route via cmd.exe /
    powershell. `shutil.which` honours PATHEXT. On macOS/Linux the Windows branch
    is a no-op.
    """
    if not argv:
        return argv
    exe = shutil.which(argv[0])
    if not exe:
        return argv  # let subprocess raise FileNotFoundError with the original name
    low = exe.lower()
    if os.name == "nt":
        if low.endswith((".cmd", ".bat")):
            return ["cmd", "/c", exe] + argv[1:]
        if low.endswith(".ps1"):
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", exe] + argv[1:]
    return [exe] + argv[1:]


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
             cwd=None) -> str:
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
        proc = subprocess.run(argv, input=stdin_data, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace", cwd=cwd)
    except FileNotFoundError:
        raise AgentRunError(
            f"CLI '{argv[0]}' not found on PATH. Install it, change backend, or pass "
            f"cmd_template.")
    except subprocess.TimeoutExpired:
        raise AgentRunError(f"CLI '{argv[0]}' did not respond within {timeout}s.")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise AgentRunError(f"CLI '{argv[0]}' exit {proc.returncode}: {tail}")
    out = (proc.stdout or "").strip()
    if not out:
        raise AgentRunError(f"CLI '{argv[0]}' returned empty stdout.")
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
        raise AgentRunError(f"Claude API HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
    except urllib.error.URLError as e:
        raise AgentRunError(f"network error calling Claude API: {e.reason}")
    if data.get("stop_reason") == "refusal":
        raise AgentRunError(f"model refusal: {data.get('stop_details')}")
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


def run_turn(prompt: str, *, system: str = None, backend: str = "claude",
             model: str = None, cmd_template: str = None, prompt_via: str = None,
             timeout: int = CLI_TIMEOUT, max_tokens: int = DEFAULT_MAX_TOKENS,
             dry_run_text: str = None, cwd: str = None) -> str:
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
                    cwd=cwd)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one headless agent turn, print raw text.")
    ap.add_argument("--prompt", default=None, help="prompt text (else read from stdin)")
    ap.add_argument("--system", default=None)
    ap.add_argument("--backend", default="claude",
                    choices=["claude", "cursor", "agy", "custom", "api", "dry-run"])
    ap.add_argument("--cmd-template", default=None, help="for --backend custom; may contain {model}")
    ap.add_argument("--prompt-via", default=None, choices=[None, "stdin", "arg"])
    ap.add_argument("--model", default=config.get("ANTHROPIC_MODEL") or None)
    ap.add_argument("--timeout", type=int, default=CLI_TIMEOUT)
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--dry-run-text", default=None)
    args = ap.parse_args()

    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    backend = "custom" if args.cmd_template else args.backend
    try:
        out = run_turn(prompt, system=args.system, backend=backend, model=args.model,
                       cmd_template=args.cmd_template, prompt_via=args.prompt_via,
                       timeout=args.timeout, max_tokens=args.max_tokens,
                       dry_run_text=args.dry_run_text)
    except AgentRunError as e:
        print(json.dumps({"error": True, "message": str(e)}, ensure_ascii=False))
        return 1
    sys.stdout.write(out)
    if not out.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
