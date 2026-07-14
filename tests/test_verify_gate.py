#!/usr/bin/env python3
"""Stdlib unittest cho gate VERIFY (chạy thật → soi output → soi DB).

Phủ 3 mảnh:
  1. spring_config — parse application-<env>.yml (nested/placeholder/jdbc pg+mysql/
     profile docs/properties) + project_config gap-fill (registry thắng, spring chỉ lấp chỗ trống).
  2. verify_gen — sinh scenario từ plan+AC (dry-run), validate step, coverage AC,
     heuristic touches_runtime.
  3. run_log — `require` nâng gate thành bắt buộc theo run (auto chặn advance);
     `ac-map --verify-json` chỉ nhận step ĐÃ PASS đúng tên "ACn: ...".
No pip deps. Không gọi mạng/agent thật.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from argparse import Namespace

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(
    _HERE, "..", ".claude", "skills", "auto-dev", "tools")))
sys.path.insert(0, os.path.abspath(os.path.join(
    _HERE, "..", ".claude", "skills", "dev-automation", "tools")))

import local_app       # noqa: E402
import project_config  # noqa: E402
import run_log         # noqa: E402
import spring_config   # noqa: E402
import verify_gen      # noqa: E402


def ns(**kw):
    return Namespace(**kw)


SPRING_YML = """\
spring:
  datasource:
    url: jdbc:postgresql://10.0.0.5:5433/atask_dev?currentSchema=drive
    username: atask_user
    password: ${DB_PASSWORD:s3cret}
server:
  port: 8086
  servlet:
    context-path: /api
"""

SPRING_MYSQL_PROPS = """\
spring.datasource.url=jdbc:mysql://db.local:3307/orders?useSSL=false
spring.datasource.username=root
spring.datasource.password=pw
server.port=9090
"""

SPRING_PROFILE_DOCS = """\
spring:
  datasource:
    url: jdbc:postgresql://base:5432/basedb
---
spring:
  config:
    activate:
      on-profile: uat
  datasource:
    url: jdbc:postgresql://uat-host:5432/uatdb
"""


class SpringParseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="spring_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel, text):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_yml_postgres_full(self):
        self._write("src/main/resources/application-dev.yml", SPRING_YML)
        cfg = spring_config.load(self.tmp, env="dev")
        db = cfg["db"]
        self.assertEqual(db["engine"], "postgres")
        self.assertEqual(db["host"], "10.0.0.5")
        self.assertEqual(db["port"], "5433")
        self.assertEqual(db["name"], "atask_dev")
        self.assertEqual(db["schema"], "drive")
        self.assertEqual(db["user"], "atask_user")
        self.assertEqual(db["password"], "s3cret")   # ${VAR:default} -> default
        self.assertEqual(cfg["server_port"], "8086")
        self.assertEqual(cfg["base_url"], "http://localhost:8086/api")

    def test_placeholder_env_wins_over_default(self):
        self._write("src/main/resources/application-dev.yml", SPRING_YML)
        os.environ["DB_PASSWORD"] = "from-env"
        try:
            cfg = spring_config.load(self.tmp, env="dev")
            self.assertEqual(cfg["db"]["password"], "from-env")
        finally:
            os.environ.pop("DB_PASSWORD", None)

    def test_properties_mysql(self):
        self._write("src/main/resources/application-dev.properties", SPRING_MYSQL_PROPS)
        db = spring_config.load(self.tmp, env="dev")["db"]
        self.assertEqual(db["engine"], "mysql")
        self.assertEqual((db["host"], db["port"], db["name"]),
                         ("db.local", "3307", "orders"))

    def test_profile_doc_of_other_env_skipped(self):
        self._write("src/main/resources/application.yml", SPRING_PROFILE_DOCS)
        db = spring_config.load(self.tmp, env="dev")["db"]
        self.assertEqual(db["host"], "base")     # doc on-profile: uat bị bỏ qua
        db_uat = spring_config.load(self.tmp, env="uat")["db"]
        self.assertEqual(db_uat["host"], "uat-host")

    def test_nothing_found(self):
        self.assertEqual(spring_config.load(self.tmp, env="dev"), {})


class RegistryGapFillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gapfill_")
        self.clone = os.path.join(self.tmp, "clone")
        res = os.path.join(self.clone, "src", "main", "resources")
        os.makedirs(res)
        with open(os.path.join(res, "application-dev.yml"), "w", encoding="utf-8") as f:
            f.write(SPRING_YML)
        os.environ["WORK_DIR"] = self.tmp

    def tearDown(self):
        os.environ.pop("WORK_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _registry(self, block):
        with open(os.path.join(self.tmp, "projects.json"), "w", encoding="utf-8") as f:
            json.dump({"p1": block}, f)

    def test_spring_fills_missing_db(self):
        self._registry({"clone_dir": self.clone, "environments": {"dev": {}}})
        stack = project_config.resolve("p1", "dev")["stack"]
        self.assertEqual(stack["db"]["host"], "10.0.0.5")
        self.assertEqual(stack["db"]["name"], "atask_dev")
        self.assertIn("application-dev.yml", stack["_spring_source"])

    def test_registry_values_win(self):
        self._registry({"clone_dir": self.clone,
                        "environments": {"dev": {"db": {"host": "registry-host"}}}})
        stack = project_config.resolve("p1", "dev")["stack"]
        self.assertEqual(stack["db"]["host"], "registry-host")   # registry thắng
        self.assertEqual(stack["db"]["name"], "atask_dev")       # spring lấp chỗ trống

    def test_full_registry_db_skips_spring(self):
        self._registry({"clone_dir": self.clone, "environments": {
            "dev": {"db": {"host": "h", "name": "n"}}}})
        stack = project_config.resolve("p1", "dev")["stack"]
        self.assertNotIn("_spring_source", stack)


PLAN_RUNTIME = """<final_specification>
<approach>Fix permission check</approach>
<target_files><file>src/main/java/x/ExportController.java</file>
<file>src/main/java/x/ExportService.java</file></target_files>
</final_specification>"""

PLAN_DOCS_ONLY = """<final_specification>
<target_files><file>README.md</file><file>docs/setup.md</file></target_files>
</final_specification>"""

SCENARIO_OK = json.dumps({
    "name": "verify",
    "steps": [
        {"type": "api", "name": "AC1: export returns 200", "method": "GET",
         "url": "/api/export", "expect": {"status": 200}},
        {"type": "db", "name": "AC2: row status updated", "engine": "postgres",
         "sql": "select status from exports", "expect": {"rows": 1}},
    ],
})


class VerifyGenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vgen_")
        self._orig = run_log._runs_dir
        run_log._runs_dir = lambda: self.tmp
        self.plan = os.path.join(self.tmp, "plan.xml")
        with open(self.plan, "w", encoding="utf-8") as f:
            f.write(PLAN_RUNTIME)
        run_log.cmd_init(ns(run_id="r1", task="t", project="p", type="bugfix",
                            title="x", tier="standard", mode="auto"))
        run_log.cmd_ac_add(ns(run_id="r1", text="export trả 200", id="AC1"))
        run_log.cmd_ac_add(ns(run_id="r1", text="row đổi status", id="AC2"))
        run_log.cmd_ac_add(ns(run_id="r1", text="chưa được chứng minh", id="AC3"))

    def tearDown(self):
        run_log._runs_dir = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gen(self, dry=SCENARIO_OK, plan=None):
        return verify_gen.cmd_run(ns(run="r1", plan=plan or self.plan, context_file=None,
                                     base_url=None, out=None, backend="dry-run",
                                     model=None, dry_run_text=dry))

    def test_scenario_written_and_coverage(self):
        out = self._gen()
        self.assertTrue(out["ok"])
        self.assertTrue(os.path.isfile(out["scenario_path"]))
        self.assertEqual(out["acs_covered"], ["AC1", "AC2"])
        self.assertEqual(out["acs_uncovered"], ["AC3"])   # trình người duyệt
        self.assertEqual(out["verdict"], "partial")
        self.assertTrue(out["touches_runtime"])           # Controller/Service trong plan

    def test_docs_only_plan_not_runtime(self):
        p2 = os.path.join(self.tmp, "plan2.xml")
        with open(p2, "w", encoding="utf-8") as f:
            f.write(PLAN_DOCS_ONLY)
        out = self._gen(plan=p2)
        self.assertFalse(out["touches_runtime"])

    def test_bad_step_type_rejected(self):
        bad = json.dumps({"steps": [{"type": "ssh", "name": "x"}]})
        with self.assertRaises(ValueError):
            self._gen(dry=bad)

    def test_markdown_fenced_json_tolerated(self):
        out = self._gen(dry=f"```json\n{SCENARIO_OK}\n```")
        self.assertTrue(out["ok"])


class ResolveCmdTest(unittest.TestCase):
    """local_app: --cmd > registry app_run_cmd > mvn default."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cmd_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_flag_wins(self):
        cmd, src = local_app._resolve_cmd(ns(cmd="java -jar x.jar"),
                                          {"app_run_cmd": "reg-cmd"}, self.tmp)
        self.assertEqual((cmd, src), ("java -jar x.jar", "flag"))

    def test_registry_app_run_cmd(self):
        cmd, src = local_app._resolve_cmd(ns(cmd=None), {"app_run_cmd": "reg-cmd"}, self.tmp)
        self.assertEqual((cmd, src), ("reg-cmd", "registry:app_run_cmd"))

    def test_pom_default_then_none(self):
        cmd, src = local_app._resolve_cmd(ns(cmd=None), {}, self.tmp)
        self.assertEqual((cmd, src), (None, "none"))
        open(os.path.join(self.tmp, "pom.xml"), "w").close()
        cmd, src = local_app._resolve_cmd(ns(cmd=None), {}, self.tmp)
        self.assertEqual((cmd, src), ("mvn -q spring-boot:run", "default:pom.xml"))

    def test_write_argfile(self):
        # cp.txt (maven) + lib scope=system -> target/local_run.args, forward slashes.
        os.makedirs(os.path.join(self.tmp, "target"))
        lib = os.path.join(self.tmp, "src", "main", "resources", "lib")
        os.makedirs(lib)
        open(os.path.join(lib, "extra.jar"), "w").close()
        with open(os.path.join(self.tmp, "target", "cp.txt"), "w", encoding="utf-8") as f:
            f.write(os.pathsep.join([r"C:\m2\a.jar", r"C:\m2\b.jar"]))
        out = local_app._write_argfile(self.tmp)
        self.assertTrue(out["ok"])
        self.assertEqual(out["entries"], 4)   # classes + 2 jar + extra.jar
        with open(out["argfile"], encoding="utf-8") as f:
            content = f.read()
        self.assertTrue(content.startswith('-cp "target/classes;'))
        self.assertIn("C:/m2/a.jar", content)          # backslash -> forward slash
        self.assertIn("src/main/resources/lib/extra.jar", content)
        self.assertNotIn("\\", content)

    def test_write_argfile_requires_cp_txt(self):
        out = local_app._write_argfile(self.tmp)
        self.assertTrue(out["error"])


CONTROLLER_JAVA = """\
package com.x;
@RestController
@RequestMapping("/api/exports")
public class ExportController {
    @GetMapping("/{id}")
    public Dto get(@PathVariable Long id) { return null; }
    @PostMapping("/run")
    public Dto run(@RequestBody Req r) { return null; }
    @DeleteMapping
    public void del() {}
}
"""


class AffectedEndpointsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep_")
        src = os.path.join(self.tmp, "src", "main", "java", "com", "x")
        os.makedirs(src)
        with open(os.path.join(src, "ExportController.java"), "w", encoding="utf-8") as f:
            f.write(CONTROLLER_JAVA)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_class_prefix_plus_method_paths(self):
        eps = verify_gen.affected_endpoints(
            self.tmp, ["src/main/java/com/x/ExportController.java"])
        got = {f"{e['method']} {e['path']}" for e in eps}
        self.assertEqual(got, {"GET /api/exports/{id}", "POST /api/exports/run",
                               "DELETE /api/exports"})

    def test_service_change_maps_to_controller(self):
        # Sửa ExportService -> đầu ra lộ qua ExportController cùng tên.
        eps = verify_gen.affected_endpoints(self.tmp, ["com/x/ExportService.java"])
        self.assertTrue(any(e["path"].startswith("/api/exports") for e in eps))

    def test_no_controller_no_endpoints(self):
        self.assertEqual(verify_gen.affected_endpoints(self.tmp, ["README.md"]), [])

    def test_endpoints_untested_warning(self):
        # Kịch bản không gọi POST /api/exports/run -> phải bị cảnh báo.
        runs = tempfile.mkdtemp(prefix="ep_runs_")
        orig = __import__("run_log")._runs_dir
        import run_log as rl
        rl._runs_dir = lambda: runs
        try:
            rl.cmd_init(ns(run_id="ep1", task="t", project="p", type="bugfix",
                           title="x", tier="standard", mode="auto"))
            plan = os.path.join(runs, "plan.xml")
            with open(plan, "w", encoding="utf-8") as f:
                f.write("<final_specification><target_files>"
                        "<file>src/main/java/com/x/ExportController.java</file>"
                        "</target_files></final_specification>")
            scenario = json.dumps({"steps": [
                {"type": "api", "name": "AC1: get export", "method": "GET",
                 "url": "/api/exports/123", "expect": {"status": 200}}]})
            out = verify_gen.cmd_run(ns(run="ep1", plan=plan, context_file=None,
                                        base_url=None, out=None, backend="dry-run",
                                        model=None, dry_run_text=scenario, root=self.tmp))
            self.assertIn("POST /api/exports/run", out["endpoints_untested"])
            self.assertNotIn("GET /api/exports/{id}", out["endpoints_untested"])
            self.assertEqual(out["verdict"], "partial")
        finally:
            rl._runs_dir = orig
            shutil.rmtree(runs, ignore_errors=True)


class RequireGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="req_")
        self._orig = run_log._runs_dir
        run_log._runs_dir = lambda: self.tmp
        run_log.cmd_init(ns(run_id="r1", task="t", project="p", type="bugfix",
                            title="x", tier="trivial", mode="auto"))
        st = run_log._read("r1")
        st["stages"]["intake"] = "done"
        st["stages"]["plan"] = "done"
        st["stages"]["implement"] = "done"
        run_log._write(st)
        run_log.cmd_record_gate(ns(run_id="r1", gate="test", verdict="pass",
                                   summary=None, json=None, kind=None))
        run_log.cmd_record_gate(ns(run_id="r1", gate="lint", verdict="waived",
                                   summary="no linter", json=None, kind=None))

    def tearDown(self):
        run_log._runs_dir = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_without_require_verify_not_blocking(self):
        out = run_log.cmd_advance(ns(run_id="r1", stage="test", force=False))
        self.assertTrue(out["allowed"])

    def test_require_verify_blocks_auto_until_pass(self):
        run_log.cmd_require(ns(run_id="r1", gate="verify", stage="test"))
        out = run_log.cmd_advance(ns(run_id="r1", stage="test", force=False))
        self.assertFalse(out["allowed"])
        self.assertIn("verify:missing", out["missing"])
        run_log.cmd_record_gate(ns(run_id="r1", gate="verify", verdict="pass",
                                   summary="flow ok", json=None, kind=None))
        out2 = run_log.cmd_advance(ns(run_id="r1", stage="test", force=False))
        self.assertTrue(out2["allowed"])

    def test_require_is_idempotent(self):
        run_log.cmd_require(ns(run_id="r1", gate="verify", stage="test"))
        out = run_log.cmd_require(ns(run_id="r1", gate="verify", stage="test"))
        self.assertEqual(out["required_extra"], ["verify"])


class AcMapVerifyJsonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="acv_")
        self._orig = run_log._runs_dir
        run_log._runs_dir = lambda: self.tmp
        run_log.cmd_init(ns(run_id="r1", task="t", project="p", type="bugfix",
                            title="x", tier="standard", mode="auto"))
        run_log.cmd_ac_add(ns(run_id="r1", text="row updated", id="AC1"))
        self.result = os.path.join(self.tmp, "verify_result.json")
        with open(self.result, "w", encoding="utf-8") as f:
            json.dump({"passed": True, "steps": [
                {"name": "AC1: row status updated", "type": "db", "passed": True},
                {"name": "AC2: api 200", "type": "api", "passed": False},
            ]}, f)

    def tearDown(self):
        run_log._runs_dir = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_passed_step_becomes_evidence(self):
        st = run_log.cmd_ac_map(ns(run_id="r1", id="AC1", evidence=None,
                                   verify_json=self.result))
        ac = st["acceptance_criteria"][0]
        self.assertEqual(ac["status"], "met")
        self.assertIn("AC1: row status updated", ac["evidence"])

    def test_failed_step_refused(self):
        run_log.cmd_ac_add(ns(run_id="r1", text="api ok", id="AC2"))
        with self.assertRaises(ValueError):
            run_log.cmd_ac_map(ns(run_id="r1", id="AC2", evidence=None,
                                  verify_json=self.result))

    def test_missing_step_refused(self):
        run_log.cmd_ac_add(ns(run_id="r1", text="no step", id="AC9"))
        with self.assertRaises(ValueError):
            run_log.cmd_ac_map(ns(run_id="r1", id="AC9", evidence=None,
                                  verify_json=self.result))

    def test_neither_evidence_nor_json_refused(self):
        with self.assertRaises(ValueError):
            run_log.cmd_ac_map(ns(run_id="r1", id="AC1", evidence=None, verify_json=None))


if __name__ == "__main__":
    unittest.main()
