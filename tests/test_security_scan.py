"""F.23 security audit: rules, Spring authorization inventory, config flattening, masking, baseline."""

import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(str(ROOT / ".claude" / "skills" / "_runtime" / "bootstrap.py"))["activate"]()
from agent_skill_toolkit.workflows.security import scan as S  # noqa: E402
from agent_skill_toolkit.workflows.security import spring  # noqa: E402

sys.path.insert(0, str(ROOT / ".claude" / "skills" / "security-audit" / "tools"))
import security_scan as T  # noqa: E402

GL = "glpat-" + "A1b2C3d4E5f6G7h8I9j0"          # built at runtime: no token literal in this file
JWT = "eyJ" + "a" * 20 + ".eyJ" + "b" * 20 + "." + "c" * 20

SECURITY = '''package x;
public class SecurityConfiguration {
    void configure(HttpSecurity http) {
        http.authorizeRequests()
            .antMatchers("/api/public/**").permitAll()
            // .antMatchers("/api/old/**").permitAll()
            .antMatchers("/api/admin/**").hasAuthority(AuthoritiesConstants.ADMIN)
            .antMatchers("/api/**").authenticated();
    }
}
'''
CONTROLLER = '''package x;
/** Javadoc that mentions a class word: this class handles tasks. */
@RestController
@RequestMapping("/api")
public class TaskResource {
    @GetMapping("/tasks/{id}")
    public Task get(@PathVariable Long id) { return null; }

    @PreAuthorize("hasAuthority('ROLE_ADMIN')")
    @DeleteMapping("/tasks/{taskId}")
    public void delete(@PathVariable Long taskId) { }

    @PostMapping("/public/hook")
    public void hook(@RequestBody String b) { }

    @GetMapping("/public/info")
    public String info() { return ""; }

    public List<Task> find(String sort) {
        return em.createQuery("select t from Task t order by " + sortField, Task.class).getResultList();
    }
}
'''
LEGACY = '''@RestController
public class LegacyResource {
    @GetMapping("/legacy/data/{id}")
    public String data(@PathVariable String id) { return ""; }
}
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="secscan-")

    def put(self, rel, text):
        p = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def rules_of(self, res):
        return [f["rule"] for f in res["findings"]]


class SpringTest(Base):
    def test_inventory_and_authorization_classes(self):
        self.put("src/main/java/x/SecurityConfiguration.java", SECURITY)
        self.put("src/main/java/x/TaskResource.java", CONTROLLER)
        self.put("src/main/java/x/LegacyResource.java", LEGACY)
        res = S.scan(self.repo)
        eps = {(e["method"], e["path"]): e for e in res["endpoints"]}
        self.assertIn(("GET", "/api/tasks/{id}"), eps)                  # class mapping despite the Javadoc word
        self.assertEqual(eps[("GET", "/api/tasks/{id}")]["access"], "authenticated")
        self.assertTrue(eps[("DELETE", "/api/tasks/{taskId}")]["method_auth"])
        by = {}
        for f in res["findings"]:
            by.setdefault(f["rule"], []).append(f["text"])
        self.assertTrue(any("/api/tasks/{id}" in t for t in by["AUTHZ-003"]))        # BOLA candidate
        self.assertFalse(any("{taskId}" in t for t in by.get("AUTHZ-003", [])))       # has @PreAuthorize
        self.assertTrue(any("/api/public/hook" in t for t in by["AUTHZ-004"]))       # public write
        self.assertTrue(any("/api/public/info" in t for t in by["AUTHZ-001"]))       # public read
        self.assertTrue(any("/legacy/data" in t for t in by["AUTHZ-002"]))           # matched by no rule
        self.assertIn("INJ-004", by)                                                  # order by + sortField

    def test_commented_rules_do_not_protect(self):
        rules = spring.url_rules({"S.java": SECURITY})
        self.assertNotIn(["/api/old/**"], [r["patterns"] for r in rules])

    def test_strip_comments_keeps_strings_and_lines(self):
        src = 'String u = "http://x/*y*/"; // c\n/* a\n b */ int z;'
        out = spring.strip_comments(src)
        self.assertIn('"http://x/*y*/"', out)
        self.assertEqual(out.count("\n"), src.count("\n"))
        self.assertNotIn("// c", out)


class RulesTest(Base):
    def test_secrets_are_found_and_masked(self):
        self.put("src/main/resources/config/application-prod.yml",
                 "spring:\n  datasource:\n    password: Sup3rS3cret!\n    username: app\n"
                 "jhipster:\n  security:\n    authentication:\n      jwt:\n        base64-secret: ${JWT_SECRET}\n"
                 "logging:\n  level:\n    ROOT: DEBUG\n"
                 "management:\n  endpoints:\n    web:\n      exposure:\n        include: '*'\n"
                 "  liquibase:\n    contexts: prod, faker\n")
        self.put("docs/api.postman_collection.json", '{"value": "' + JWT + '", "k": "' + GL + '"}\n')
        res = S.scan(self.repo)
        rules = self.rules_of(res)
        for r in ("SEC-003", "SEC-002", "CFG-006", "CFG-001"):
            self.assertIn(r, rules, r)
        blob = json.dumps(res, ensure_ascii=False)
        for secret in ("Sup3rS3cret", JWT, GL):
            self.assertNotIn(secret, blob)                                  # never in any output
        sec3 = [f for f in res["findings"] if f["rule"] == "SEC-003"]
        self.assertEqual(len(sec3), 1)                                      # ${JWT_SECRET} is not a secret
        self.assertIn("spring.datasource.password: Sup…", sec3[0]["text"])

    def test_dev_profile_and_tests_are_not_prod(self):
        self.put("src/main/resources/config/application-dev.yml", "logging:\n  level:\n    ROOT: DEBUG\n")
        self.put("src/test/java/x/FooTest.java", 'class FooTest { void a(){ e.printStackTrace(); } }\n')
        rules = self.rules_of(S.scan(self.repo))
        self.assertNotIn("CFG-006", rules)
        self.assertNotIn("GEN-002", rules)

    def test_false_positive_guards(self):
        self.put("src/main/java/x/TaskTemplateResource.java",
                 '@RestController\npublic class TaskTemplateResource {\n'
                 '  private static final String TOKEN_PREFIX = "etask_pat_";\n'
                 '  void a(){ log.info("Password configured: {}", pwd != null ? "Yes" : "No");\n'
                 '    em.createNativeQuery(OWNER_SQL + " FOR UPDATE"); }\n}\n')
        self.put("docs/task-closed-status-api-with-a-very-long-name-here.md", "see task-closed-status-api-x\n")
        rules = self.rules_of(S.scan(self.repo))
        for r in ("GEN-001", "SEC-004", "LOG-001", "INJ-001", "SEC-002"):
            self.assertNotIn(r, rules, r)

    def test_inline_ignore(self):
        self.put("src/main/java/x/A.java",
                 "class A { void a(){\n  // security-audit: ignore INJ-001 table from an enum\n"
                 '  em.createNativeQuery("DELETE FROM " + table); } }\n')
        self.assertNotIn("INJ-001", self.rules_of(S.scan(self.repo)))

    def test_flatten_yaml_and_properties(self):
        flat = S.flatten_config("a.yml", "a:\n  b:\n    c: 1\n  d: x # note\n---\ne: 2\n")
        self.assertEqual([(k, v) for _, k, v in flat], [("a.b.c", "1"), ("a.d", "x"), ("e", "2")])
        self.assertEqual(S.flatten_config("a.properties", "x.y=1\n# c\n")[0][1:], ("x.y", "1"))


class HistoryAndBaselineTest(Base):
    def test_secret_deleted_later_is_still_found_in_history(self):
        g = ["git", "-C", self.repo, "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        self.put("app.properties", "db.password=" + "Hunter2Hunter2\n")
        subprocess.run(g + ["add", "."], check=True)
        subprocess.run(g + ["commit", "-qm", "a"], check=True)
        self.put("app.properties", "db.password=${DB_PASSWORD}\n")
        subprocess.run(g + ["commit", "-qam", "b"], check=True)
        res = S.scan(self.repo, history=True)
        hist = [f for f in res["findings"] if f["rule"].startswith("HIST-")]
        self.assertEqual(len(hist), 1)
        self.assertNotIn("Hunter2", json.dumps(hist))
        self.assertNotIn("SEC-003", self.rules_of(res))                  # current file is clean

    def test_baseline_gates_only_new_findings(self):
        self.put("src/main/java/x/A.java", 'class A { void a(){ e.printStackTrace(); new ObjectInputStream(in); } }\n')
        self.assertEqual(T.main(["scan", "--repo", self.repo]), 1)        # DESER-001 high, new
        self.assertEqual(T.main(["baseline", "--repo", self.repo, "--note", "t"]), 0)
        self.assertEqual(T.main(["scan", "--repo", self.repo]), 0)        # accepted
        self.put("src/main/java/x/B.java", 'class B { void b(){ mapper.enableDefaultTyping(); } }\n')
        self.assertEqual(T.main(["scan", "--repo", self.repo]), 1)        # a NEW high
        with open(os.path.join(self.repo, ".agent", "security-baseline.json"), encoding="utf-8") as f:
            self.assertNotIn("ObjectInputStream", f.read())               # fingerprints, no code

    def test_missing_repo_is_an_error_not_clean(self):
        self.assertEqual(T.main(["scan", "--repo", os.path.join(self.repo, "nope")]), 2)


class CodexRound1Test(Base):
    """Regressions of Codex security review round 1 (one test per finding)."""

    def test_r1_1_any_finding_text_is_redacted(self):
        self.put("src/main/java/x/A.java",
                 'class A { void a(){ String password = "Hunter2Abc9"; e.printStackTrace(); } }\n')
        res = S.scan(self.repo)
        self.assertIn("GEN-002", self.rules_of(res))
        self.assertNotIn("Hunter2Abc9", json.dumps(res, ensure_ascii=False))

    def test_r1_2_history_failure_is_a_gap_not_clean(self):
        self.put("src/main/java/x/A.java", "class A {}\n")          # not a git repo
        res = S.scan(self.repo, history=True)
        self.assertTrue(res["gaps"])
        self.assertEqual(T.main(["scan", "--repo", self.repo, "--history"]), 3)

    def test_r1_3_long_lines_scanned_and_oversized_is_a_gap(self):
        self.put("dist2/app.js", "x" * 9000 + " k='" + GL + "' " + "y" * 3000 + "\n")
        res = S.scan(self.repo)
        self.assertIn("SEC-002", self.rules_of(res))
        self.assertNotIn(GL, json.dumps(res))
        old = S.MAX_FILE
        S.MAX_FILE = 100
        try:
            res = S.scan(self.repo)
        finally:
            S.MAX_FILE = old
        self.assertTrue(any(g["path"] == "dist2/app.js" for g in res["gaps"]))
        self.assertEqual(T.main(["scan", "--repo", self.repo]), 1)   # the long-line token, high

    def test_r1_4_env_and_json_credentials(self):
        self.put("deploy/.env." + "prod", "export DB_PASSWORD=RealPass123\n")
        self.put("config/creds.json", '{\n  "client_secret": "S3cr3tValue99"\n}\n')
        res = S.scan(self.repo)
        files = {f["file"] for f in res["findings"] if f["rule"] == "SEC-003"}
        self.assertEqual(files, {"deploy/.env.prod", "config/creds.json"})
        self.put("package-lock.json", '{\n  "requires": {\n    "registry-auth-token": "^3.4.0"\n  }\n}\n')
        self.put("web/package.json", '{\n  "dependencies": {"auth-token": "^3.4.0"}\n}\n')
        res = S.scan(self.repo)
        files = {f["file"] for f in res["findings"] if f["rule"] == "SEC-003"}
        self.assertEqual(files, {"deploy/.env.prod", "config/creds.json"})       # lockfile / version: not secrets
        blob = json.dumps(res)
        self.assertNotIn("RealPass123", blob)
        self.assertNotIn("S3cr3tValue99", blob)

    def test_r1_5_placeholder_default_is_evaluated(self):
        self.put("src/main/resources/config/application.yml",
                 "spring:\n  datasource:\n    password: ${DB_PASSWORD:RealDefault9}\n"
                 "    username: ${DB_USER}\napp:\n  token: ${APP_TOKEN:}\n")
        sec = [f for f in S.scan(self.repo)["findings"] if f["rule"] == "SEC-003"]
        self.assertEqual(len(sec), 1)
        self.assertNotIn("RealDefault9", json.dumps(sec))

    def test_r1_6_any_method_mapping_judged_per_method(self):
        self.put("src/main/java/x/SecurityConfiguration.java",
                 'class SecurityConfiguration { void c(HttpSecurity http) { http.authorizeRequests()\n'
                 '  .antMatchers(HttpMethod.POST, "/api/hook/**").permitAll()\n'
                 '  .antMatchers("/api/**").authenticated(); } }\n')
        self.put("src/main/java/x/HookResource.java",
                 '@RestController\npublic class HookResource {\n'
                 '    @RequestMapping("/api/hook/run")\n    public void run() { }\n}\n')
        res = S.scan(self.repo)
        authz = [f for f in res["findings"] if f["rule"].startswith("AUTHZ")]
        self.assertEqual([f["rule"] for f in authz], ["AUTHZ-004"])     # its POST is public
        self.assertIn("POST", authz[0]["detail"])

    def test_r1_7_identical_lines_get_distinct_fingerprints(self):
        self.put("src/main/java/x/A.java",
                 "class A {\n void a(){ e.printStackTrace(); }\n void b(){ e.printStackTrace(); }\n}\n")
        fps = [f["fp"] for f in S.scan(self.repo)["findings"] if f["rule"] == "GEN-002"]
        self.assertEqual(len(set(fps)), 2)

    def test_r1_8_yaml_lists_and_block_scalars(self):
        flat = S.flatten_config("a.yml", "a:\n  list:\n    - x\n    - y\n  s: |\n    l1\n    l2\n"
                                         "users:\n  - name: u\n    password: Pw12345678\nb: [1, 2]\n")
        self.assertEqual([(k, v) for _, k, v in flat],
                         [("a.list", "x"), ("a.list", "y"), ("a.s", "l1 l2"), ("users.name", "u"),
                          ("users.password", "Pw12345678"), ("b", "1"), ("b", "2")])

    def test_r1_9_prod_region_profile_is_prod(self):
        self.assertTrue(S._is_prod_config("src/main/resources/config/application-prod-us.yml"))
        self.assertFalse(S._is_prod_config("src/main/resources/config/application-dev.yml"))

    def test_r1_10_tls_flag_polarity(self):
        self.put("src/main/resources/config/application.yml",
                 "a:\n  verify-ssl: false\nb:\n  insecure-skip-tls-verify: true\n"
                 "c:\n  insecure-skip-tls-verify: false\nd:\n  verify-ssl: true\n")
        hits = sorted((f["rule"], f["line"]) for f in S.scan(self.repo)["findings"]
                      if f["rule"] in ("CFG-007", "CFG-009"))
        self.assertEqual(hits, [("CFG-007", 2), ("CFG-009", 4)])

    def test_r1_11_xxe_hardening_must_be_near_the_factory(self):
        far = "\n".join(["    void pad%d() { }" % i for i in range(30)])
        self.put("src/main/java/x/X.java",
                 "class X {\n  void a() { DocumentBuilderFactory f = DocumentBuilderFactory.newInstance(); }\n"
                 + far + '\n  void b(DocumentBuilderFactory g) { g.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true); }\n'
                 "  void c() { SAXParserFactory s = SAXParserFactory.newInstance();\n"
                 '    s.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true); }\n}\n')
        xxe = [f["line"] for f in S.scan(self.repo)["findings"] if f["rule"] == "XXE-001"]
        self.assertEqual(xxe, [2])


class CodexRound2Test(Base):
    def test_r2_1_escaped_quote_does_not_leak_the_tail(self):
        self.put("src/main/java/x/A.java",
                 'class A { void a(){ String password = "abc\\"TAILSECRET9"; e.printStackTrace(); } }\n')
        res = S.scan(self.repo)
        self.assertIn("GEN-002", self.rules_of(res))
        self.assertNotIn("TAILSECRET", json.dumps(res))
        self.assertNotIn("XYZSECRET", S.redact('token = "abcXYZSECRET unterminated'))

    def test_r2_2_overlapping_spans_are_merged(self):
        self.assertEqual(S.redact("k=" + "A" * 20 + "B" * 20, [(2, 30), (10, 42)]), "k=AAA…")

    def test_r2_3_r2_4_very_long_lines_are_matched_whole(self):
        pad = "p" * 20000
        self.put("config/big.json", '{"pad": "' + pad + '", "password": "RealPass987"}\n')
        self.put("src/main/java/x/Q.java",
                 'class Q { void a(){ em.createQuery("select t from T t where ' + "x" * 4500
                 + '" + request.getParameter("q")); } }\n')
        self.put("web/app.js", "var k = '" + "glpat-" + "Z" * 5000 + "';\n")
        res = S.scan(self.repo)
        rules = self.rules_of(res)
        self.assertIn("SEC-003", rules)
        self.assertIn("INJ-004", rules)
        self.assertIn("SEC-002", rules)
        blob = json.dumps(res)
        self.assertNotIn("RealPass987", blob)
        self.assertNotIn("Z" * 10, blob)                       # long token not leaked past the mask

    def test_r2_5_xxe_needs_the_protective_value_on_that_factory(self):
        self.put("src/main/java/x/X.java",
                 "class X {\n"
                 "  void a() { DocumentBuilderFactory f = DocumentBuilderFactory.newInstance();\n"
                 "    f.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, false); }\n"
                 "  void b() { DocumentBuilderFactory g = DocumentBuilderFactory.newInstance();\n"
                 "    DocumentBuilderFactory h = DocumentBuilderFactory.newInstance();\n"
                 "    h.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true); }\n"
                 "  void c() { DocumentBuilderFactory.newInstance().newDocumentBuilder(); }\n"
                 "}\n")
        xxe = [f["line"] for f in S.scan(self.repo)["findings"] if f["rule"] == "XXE-001"]
        self.assertEqual(xxe, [2, 4, 7])                        # h (line 5) is hardened

    def test_r2_6_version_shaped_password_is_a_secret(self):
        self.put("src/main/resources/app.properties", "db.password=12.34.56\n")
        self.assertIn("SEC-003", self.rules_of(S.scan(self.repo)))


class CodexRound3Test(Base):
    def test_r3_1_trailing_backslash_string_is_masked(self):
        self.put("web/a.js", 'eval(input); const password = "abc TAILSECRET\\\ncontinued";\n')
        res = S.scan(self.repo)
        self.assertIn("JS-001", self.rules_of(res))
        self.assertNotIn("TAILSECRET", json.dumps(res))

    def test_r3_2_each_factory_on_a_line_is_judged(self):
        self.put("src/main/java/x/X.java",
                 "class X { void a() {\n"
                 "  DocumentBuilderFactory safe = DocumentBuilderFactory.newInstance(); "
                 "DocumentBuilderFactory unsafe = DocumentBuilderFactory.newInstance();\n"
                 "  safe.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true); unsafe.newDocumentBuilder(); }\n}\n")
        self.assertIn("XXE-001", self.rules_of(S.scan(self.repo)))

    def test_r3_3_manifest_exemption_only_for_dependency_entries(self):
        self.put("web/package.json",
                 '{"name": "app", "config": {"password": "12.34.56"},\n'
                 ' "dependencies": {"registry-auth-token": "^3.4.0"}}\n')
        sec = [f for f in S.scan(self.repo)["findings"] if f["rule"] == "SEC-003"]
        self.assertEqual([f["text"].split(":")[0] for f in sec], ["password"])


class CodexRound4Test(Base):
    def test_r4_1_template_literal_is_masked(self):
        self.put("web/a.js", "eval(input); const password = `abc TAILSECRET9`;\n"
                             "eval(input); const token = `${prefix}TAILSECRET8${suffix}`;\n")
        res = S.scan(self.repo)
        self.assertIn("JS-001", self.rules_of(res))
        self.assertNotIn("TAILSECRET", json.dumps(res))
        self.assertEqual(S.redact('password: "${DB_PASSWORD}"'), 'password: "${DB_PASSWORD}"')


class CodexRound6Test(Base):
    def test_r6_1_whole_assigned_expression_is_masked(self):
        self.put("web/a.js", 'eval(input); const password = "abc" + "TAILSECRET9";\n'
                             "eval(input); const apiKey = cfg.k || 'TAILSECRET8' + x;\n")
        res = S.scan(self.repo)
        self.assertEqual(self.rules_of(res).count("JS-001"), 2)
        self.assertNotIn("TAILSECRET", json.dumps(res))
        self.assertEqual(S.redact('x.password = "${DB_PASSWORD}";'), 'x.password = "${DB_PASSWORD}";')
        self.assertEqual(S.redact("token: abc"), "token: abc")
        self.assertEqual(S.redact('password = "Hunter2xyz"; e.printStackTrace();'), 'password = "Hun…"')

    def test_r7_1_bracket_call_and_flag_forms_are_masked(self):
        self.put("web/a.js", 'eval(input); credentials[ "password" ] = "Hunter2Secret9";\n')
        res = S.scan(self.repo)
        self.assertIn("JS-001", self.rules_of(res))
        self.assertNotIn("Secret9", json.dumps(res))
        self.put("src/main/java/x/M.java",
                 'class M { void a() { mailSender.setPassword("Hunter2Secret9"); '
                 "// TODO security: load password from vault\n } }\n")
        res = S.scan(self.repo)
        self.assertIn("GEN-003", self.rules_of(res))
        self.assertNotIn("Secret9", json.dumps(res))
        self.assertEqual(S.redact("String p = user.getPassword();"), "String p = user.getPassword();")
        for line in ('credentials["password"] = "Hunter2Secret9";',
                     "creds[ 'token' ]\t=\t'Hunter2Secret9'",
                     'builder.apiKey ( "Hunter2Secret9" )',
                     'auth(token -> "Hunter2Secret9")',
                     'new Creds(user, /* password */ "Hunter2Secret9")',
                     'm.put( "password" , "Hunter2Secret9" )',
                     "cfg['api_key'] = 'Hunter2Secret9'",
                     'headers.put("X-Auth-Token", "Hunter2Secret9");',
                     "props.setProperty(\"db.password\", \"Hunter2Secret9\");",
                     "mysql --password Hunter2Secret9 -h db",
                     "run --client-secret=Hunter2Secret9",
                     "this.password = 'Hunter2Secret9'"):
            self.assertNotIn("Secret9", S.redact(line), line)

    def test_r4_2_dependency_context_is_per_occurrence(self):
        self.put("web/package.json",
                 '{"dependencies": {"auth-token": "1.2.3"},\n "config": {"auth-token": "1.2.3"}}\n')
        sec = [f["line"] for f in S.scan(self.repo)["findings"] if f["rule"] == "SEC-003"]
        self.assertEqual(sec, [2])
        pairs = S._json_pairs('{"a": {"b": "x", "c": [{"d": "y"}]}, "e": "z"}')
        self.assertEqual([(k, v, p) for _, k, v, p in pairs], [("b", "x", "a"), ("d", "y", "c"), ("e", "z", None)])

    def test_r4_3_hardening_before_a_reassigned_factory_does_not_count(self):
        self.put("src/main/java/x/X.java",
                 "class X { void parse(InputStream input) throws Exception { "
                 "DocumentBuilderFactory f = DocumentBuilderFactory.newInstance(); "
                 "f.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true); f = DocumentBuilderFactory.newInstance(); "
                 "f.newDocumentBuilder().parse(input); } }\n")
        self.assertIn("XXE-001", self.rules_of(S.scan(self.repo)))
        self.put("src/main/java/x/X.java",
                 "class X { void parse() { DocumentBuilderFactory f = DocumentBuilderFactory.newInstance();\n"
                 "  f.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true); } }\n")
        self.assertNotIn("XXE-001", self.rules_of(S.scan(self.repo)))


if __name__ == "__main__":
    unittest.main()
