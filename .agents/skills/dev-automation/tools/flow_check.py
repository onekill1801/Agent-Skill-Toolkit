#!/usr/bin/env python3
"""End-to-end flow runner for the stack-verify toolkit.

Reads a declarative JSON scenario and runs its steps in order across components
(api / db / redis / kafka / jenkins / wait), passing values between steps via
`save`, and substituting {var} placeholders. Stops at the first failed step
(unless that step sets "continue_on_fail": true) and reports the whole run.

Zero external dependencies — orchestrates the probe modules in-process (stdlib).

Scenario shape:
{
  "name": "create-user e2e",
  "vars": {"name": "Alice"},                 # optional seed variables
  "steps": [
    {"type":"api","method":"POST","url":"/users","body":{"name":"{name}"},
     "expect":{"status":201}, "save":{"uid":"$.saved.uid"}, "saveFrom":{"uid":"$.id"}},
    {"type":"db","engine":"postgres","sql":"select status from users where id={uid}",
     "expect":{"rows":1,"value":"ACTIVE"}},
    {"type":"kafka","op":"consume","topic":"user.created","timeout":10,
     "expect":{"contains":"\"uid\":{uid}"}},
    {"type":"redis","op":"get","key":"user:{uid}","expect":{"exists":true}}
  ]
}

`saveFrom` (api only) extracts from the API JSON body into vars via the probe's
own save mechanism; `save` extracts from the step *result* object via JSONPath.

Usage:
    python flow_check.py --file scenarios/create-user.json
    python flow_check.py --file s.json --var name=Bob --var env=dev
    echo '{...}' | python flow_check.py --stdin

Output: a single JSON object {"passed":bool,"name":..,"steps":[..],"vars":{..}}.
"""

import argparse
import json
import sys
import time
from argparse import Namespace

import project_config
import probe_common as pc
import probe_api
import probe_db
import probe_redis
import probe_kafka
import jenkins


def _ns(defaults: dict, step: dict, keys: dict) -> Namespace:
    """Build a Namespace: start from defaults, map step[src] -> attr per `keys`."""
    ns = Namespace(**defaults)
    for attr, src in keys.items():
        if src in step:
            setattr(ns, attr, step[src])
    return ns


def _run_api(step) -> dict:
    expect = step.get("expect", {})
    ns = _ns(
        {"method": "GET", "url": None, "body": None, "header": None,
         "expect_status": expect.get("status"), "expect_json": None,
         "expect_contains": expect.get("contains"), "save": None, "timeout": 30},
        step, {"method": "method", "url": "url"},
    )
    body = step.get("body")
    ns.body = body if isinstance(body, (str, type(None))) else json.dumps(body)
    if isinstance(step.get("headers"), dict):
        ns.header = [f"{k}: {v}" for k, v in step["headers"].items()]
    if isinstance(expect.get("json"), dict):
        ns.expect_json = [f"{k}={v}" for k, v in expect["json"].items()]
    if isinstance(step.get("saveFrom"), dict):
        ns.save = [f"{k}={v}" for k, v in step["saveFrom"].items()]
    return probe_api.cmd_call(ns)


def _run_db(step) -> dict:
    expect = step.get("expect", {})
    ns = _ns(
        {"engine": "postgres", "sql": None, "host": None, "port": None,
         "user": None, "password": None, "name": None, "schema": None,
         "expect_rows": expect.get("rows"), "expect_empty": bool(expect.get("empty")),
         "expect_value": expect.get("value"), "expect_contains": expect.get("contains"),
         "dry_run": False, "timeout": 60},
        step, {"engine": "engine", "sql": "sql", "host": "host", "port": "port",
               "user": "user", "password": "password", "name": "database", "schema": "schema"},
    )
    # The DB name comes from the step's "database" key — NOT "name", which is the
    # universal step LABEL (mapping it here would connect to a DB named after the label).
    return probe_db.cmd_query(ns)


def _run_redis(step) -> dict:
    expect = step.get("expect", {})
    ns = _ns(
        {"op": "get", "key": None, "value": None, "ex": None,
         "host": None, "port": None, "password": None, "db": None,
         "expect_exists": bool(expect.get("exists")),
         "expect_missing": bool(expect.get("missing")),
         "expect_value": expect.get("value"), "expect_ttl_min": expect.get("ttl_min"),
         "expect_count": expect.get("count"), "timeout": 10},
        step, {"op": "op", "key": "key", "value": "value", "ex": "ex",
               "host": "host", "port": "port", "password": "password", "db": "db"},
    )
    return probe_redis.run(ns)


def _run_kafka(step) -> dict:
    expect = step.get("expect", {})
    op = step.get("op", "consume")
    if op == "produce":
        ns = _ns({"topic": None, "value": None, "key": None, "dry_run": False},
                 step, {"topic": "topic", "key": "key"})
        val = step.get("value")
        ns.value = val if isinstance(val, str) else json.dumps(val)
        return probe_kafka.cmd_produce(ns)
    ns = _ns(
        {"topic": None, "group": "stack-verify-probe", "timeout": 15,
         "expect_contains": expect.get("contains"), "expect_json": expect.get("json"),
         "min_count": expect.get("min_count"), "dry_run": False},
        step, {"topic": "topic", "group": "group", "timeout": "timeout"},
    )
    return probe_kafka.cmd_consume(ns)


def _run_jenkins(step) -> dict:
    ns = _ns(
        {"job": None, "path": None, "param": None, "wait": bool(step.get("wait", True)),
         "timeout": step.get("timeout", 1800), "poll": 5, "dry_run": False},
        step, {"job": "job", "path": "path"},
    )
    if isinstance(step.get("params"), dict):
        ns.param = [f"{k}={v}" for k, v in step["params"].items()]
    return jenkins.cmd_build(ns)


def _run_wait(step) -> dict:
    secs = step.get("seconds", 1)
    time.sleep(secs)
    return {"passed": True, "waited": secs}


_RUNNERS = {
    "api": _run_api, "db": _run_db, "redis": _run_redis,
    "kafka": _run_kafka, "jenkins": _run_jenkins, "wait": _run_wait,
}


def _step_mutates(step: dict) -> bool:
    t = step.get("type")
    if t == "api":
        return step.get("method", "GET").upper() not in ("GET", "HEAD", "OPTIONS")
    if t == "db":
        return probe_db._is_write_sql(step.get("sql", ""))
    if t == "redis":
        return step.get("op", "get") in ("set", "del")
    if t == "kafka":
        return step.get("op", "consume") == "produce"
    if t == "jenkins":
        return True
    return False


def flow_mutates(scenario: dict) -> bool:
    return any(_step_mutates(s) for s in scenario.get("steps", []))


def run_flow(scenario: dict, seed_vars: dict) -> dict:
    variables = dict(scenario.get("vars", {}))
    variables.update(seed_vars)
    results = []
    passed = True

    for i, raw_step in enumerate(scenario.get("steps", [])):
        stype = raw_step.get("type")
        runner = _RUNNERS.get(stype)
        label = raw_step.get("name", f"{stype}#{i}")
        if not runner:
            results.append({"name": label, "passed": False, "error": f"unknown step type '{stype}'"})
            passed = False
            break

        step = pc.substitute(raw_step, variables)  # resolve {var} before running
        try:
            result = runner(step)
        except Exception as e:  # a probe blew up — record and stop
            results.append({"name": label, "type": stype, "passed": False, "error": repr(e)})
            passed = False
            break

        # Merge saved values (api auto-saves into result["saved"]).
        if isinstance(result.get("saved"), dict):
            variables.update(result["saved"])
        # Explicit save: extract from the result object via JSONPath.
        for var, path in (step.get("save") or {}).items():
            try:
                variables[var] = pc.jsonpath_get(result, path)
            except (KeyError, IndexError, TypeError):
                result.setdefault("save_errors", []).append({var: path})

        step_passed = bool(result.get("passed", False)) and not result.get("error")
        results.append({"name": label, "type": stype, "passed": step_passed, "result": result})

        if not step_passed and not step.get("continue_on_fail"):
            passed = False
            break

    return {"passed": passed, "name": scenario.get("name"), "steps": results, "vars": variables}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an e2e stack-verify scenario")
    parser.add_argument("--file", help="path to a scenario JSON file")
    parser.add_argument("--stdin", action="store_true", help="read scenario JSON from stdin")
    parser.add_argument("--var", action="append", help="seed variable key=value (repeatable)")
    parser.add_argument("--project", help="load this project's stack config from ./work/projects.json")
    parser.add_argument("--env", help="environment (local|dev|uat|sandbox|prod); default from project")
    parser.add_argument("--allow-prod", action="store_true", help="permit a scenario with writes against a protected env")
    args = parser.parse_args()

    try:
        if args.stdin:
            scenario = json.load(sys.stdin)
        elif args.file:
            with open(args.file, encoding="utf-8") as f:
                scenario = json.load(f)
        else:
            raise ValueError("provide --file <path> or --stdin")
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return pc.emit({"error": True, "passed": False, "message": f"cannot load scenario: {e}"})

    # Load per-project, per-env stack config (flag wins over scenario fields).
    args.project = args.project or scenario.get("project")
    args.env = args.env or scenario.get("env")
    err = project_config.apply_args(args, mutating=flow_mutates(scenario))
    if err:
        return pc.emit(err)
    seed = dict(kv.split("=", 1) for kv in (args.var or []))
    return pc.emit(run_flow(scenario, seed))


if __name__ == "__main__":
    sys.exit(main())
