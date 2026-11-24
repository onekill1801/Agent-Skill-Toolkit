#!/usr/bin/env python3
"""Stdlib unittest for agent_init (the `.agent/` knowledge standard).

Contracts pinned here:
  1. the YAML subset round-trips, so `check` compares knowledge, not formatting;
  2. migrating an existing vendor doc loses nothing silently;
  3. `project.yml` is committed, so it must never carry a secret;
  4. `apply` writes nothing without --yes;
  5. `check` notices when the repo moves and `.agent/` does not.

No pip deps, no network, no agent. A synthetic Maven repo stands in for a real
product repo.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(
    _HERE, "..", ".claude", "skills", "auto-dev", "tools")))
sys.path.insert(0, os.path.abspath(os.path.join(
    _HERE, "..", ".claude", "skills", "dev-automation", "tools")))

import agent_init  # noqa: E402

POM = """<project>
  <artifactId>demo</artifactId>
  <properties>
    <java.version>11</java.version>
    <spring-boot.version>2.1.10.RELEASE</spring-boot.version>
    <jhipster-dependencies.version>3.0.7</jhipster-dependencies.version>
  </properties>
</project>
"""

APP_YML = """spring:
  datasource:
    url: jdbc:mysql://db.internal:3306/demo
    username: demo_user
    password: sup3rs3cret
server:
  port: 8271
"""

VENDOR_DOC = """# Demo — Architecture Guide for Claude

## Module Structure

Base package: `com.acme.demo.module`

## Layer Rules — PHẢI tuân thủ

- web/ khong goi repository

## Naming Conventions

| Loai | Vi du |
|---|---|
| Entity | `Task` |

## Known Tech Debt (can refactor)

- God class in TaskServiceImpl

## Sharding

- project_id la shard key
"""


class Fixture(unittest.TestCase):
    """A synthetic product repo + a registry pointing at it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agentinit_")
        self.repo = os.path.join(self.tmp, "repo")
        pkg = os.path.join(self.repo, "src", "main", "java", "com", "acme", "demo")
        os.makedirs(os.path.join(pkg, "module", "task"))
        os.makedirs(os.path.join(pkg, "module", "billing"))
        os.makedirs(os.path.join(self.repo, "src", "main", "resources", "config"))
        os.makedirs(os.path.join(self.repo, "src", "test", "java"))
        os.makedirs(os.path.join(self.repo, "docs", "adr"))
        self._w("pom.xml", POM)
        self._w("mvnw", "#!/bin/sh\n")
        self._w("checkstyle.xml", "<module/>")
        self._w("src/main/resources/config/application-dev.yml", APP_YML)
        self._w("src/main/java/com/acme/demo/DemoApp.java",
                "@SpringBootApplication\npublic class DemoApp {}\n")
        self._w("src/test/java/DemoTest.java", "class DemoTest {}")
        self._w("docs/adr/0001-x.md", "# ADR-0001\n")

        with open(os.path.join(self.tmp, "projects.json"), "w", encoding="utf-8") as f:
            json.dump({"demo": {"repo_path": "acme/demo", "clone_dir": self.repo,
                                "default_branch": "main", "default_env": "dev"}}, f)
        os.environ["WORK_DIR"] = self.tmp
        self.draft_dir = os.path.join(self.tmp, "draft")

    def tearDown(self):
        os.environ.pop("WORK_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _w(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def draft(self):
        return agent_init.draft("demo", out_dir=self.draft_dir)


class YamlSubsetTest(unittest.TestCase):
    def test_round_trip(self):
        data = {
            "name": "demo", "n": 3, "flag": True, "nothing": None,
            "stack": {"lang": "java", "cmd": "mvn -q test"},
            "modules": ["a", "b"], "empty_list": [], "empty_map": {},
        }
        back = agent_init.yaml_load(agent_init.yaml_dump(data))
        self.assertEqual(back, data)

    def test_colon_value_survives(self):
        data = {"commands": {"lint": "./mvnw -q checkstyle:check"}}
        back = agent_init.yaml_load(agent_init.yaml_dump(data))
        self.assertEqual(back["commands"]["lint"], "./mvnw -q checkstyle:check")

    def test_dump_refuses_what_load_cannot_read(self):
        """The format must stay inside the loader's subset, or `check` would
        report permanent phantom drift."""
        with self.assertRaises(ValueError):
            agent_init.yaml_dump({"datastores": [{"engine": "mysql"}]})

    def test_deep_nesting_round_trips(self):
        data = {"a": {"b": {"c": {"d": "x"}}}, "l": ["alpha", "beta"]}
        self.assertEqual(agent_init.yaml_load(agent_init.yaml_dump(data)), data)

    def test_numeric_strings_come_back_as_ints(self):
        """Known and deliberate: YAML has no way to tell "11" from 11 here. It is
        why `check` compares fields with str(), so java_version "11" vs 11 is not
        reported as drift."""
        back = agent_init.yaml_load(agent_init.yaml_dump({"stack": {"java_version": "11"}}))
        self.assertEqual(back["stack"]["java_version"], 11)
        self.assertEqual(str(back["stack"]["java_version"]), "11")


class SplitTest(unittest.TestCase):
    def test_headings_route_to_the_right_file(self):
        buckets, dropped = agent_init.split_existing(VENDOR_DOC)
        conv = "\n".join(buckets["conventions.md"])
        learned = "\n".join(buckets["learned.md"])
        arch = "\n".join(buckets["architecture.md"])
        self.assertIn("Layer Rules", conv)
        self.assertIn("Naming Conventions", conv)
        self.assertIn("Known Tech Debt", learned)
        self.assertIn("Module Structure", arch)
        self.assertIn("Sharding", arch)

    def test_only_the_title_is_dropped(self):
        _, dropped = agent_init.split_existing(VENDOR_DOC)
        self.assertEqual([d for d in dropped if d.strip()],
                         ["# Demo — Architecture Guide for Claude"])

    def test_nothing_is_lost(self):
        buckets, dropped = agent_init.split_existing(VENDOR_DOC)
        before = len([ln for ln in VENDOR_DOC.split("\n") if ln.strip()])
        after = sum(len([ln for ln in v if ln.strip()]) for v in buckets.values())
        self.assertEqual(before, after + len([d for d in dropped if d.strip()]))


class ScanTest(Fixture):
    def test_derives_stack_and_commands(self):
        f = agent_init.scan("demo")
        self.assertEqual(f["stack"]["framework"], "jhipster-spring-boot")
        self.assertEqual(f["stack"]["java_version"], "11")
        self.assertEqual(f["commands"]["test"], "./mvnw -q test")
        self.assertIn("checkstyle", f["commands"]["lint"])

    def test_finds_modules_and_entrypoint_by_annotation(self):
        f = agent_init.scan("demo")
        self.assertEqual(f["layout"]["modules"], ["billing", "task"])
        self.assertEqual(f["layout"]["base_package"], "com.acme.demo")
        self.assertEqual(f["layout"]["entrypoints"],
                         ["src/main/java/com/acme/demo/DemoApp.java"])

    def test_finds_existing_knowledge(self):
        self._w("CLAUDE.md", VENDOR_DOC)
        f = agent_init.scan("demo")
        self.assertEqual(f["knowledge"]["adr_dir"], "docs/adr")
        self.assertEqual(f["knowledge"]["existing"], ["CLAUDE.md"])
        self.assertFalse(f["knowledge"]["has_agent_dir"])

    def test_unknown_project_is_an_error_not_a_guess(self):
        with self.assertRaises(ValueError):
            agent_init.scan("nope")

    def test_reads_the_other_registry_spelling(self):
        """`new-product/registry.py add` writes gitlab_path + default_target_branch.
        Reading only our own spelling would yield a confident `repo: null`."""
        with open(os.path.join(self.tmp, "projects.json"), "w", encoding="utf-8") as f:
            json.dump({"demo": {"clone_dir": self.repo,
                                "gitlab_path": "acme/demo",
                                "default_target_branch": "release"}}, f)
        f = agent_init.scan("demo")
        self.assertEqual(f["repo"], "acme/demo")
        self.assertEqual(f["default_branch"], "release")

    def test_branch_is_not_taken_from_whatever_is_checked_out(self):
        """A tree parked on a feature branch must not rewrite the project's
        default branch — that is how `merge/rfc` ends up in committed facts."""
        import subprocess
        with open(os.path.join(self.tmp, "projects.json"), "w", encoding="utf-8") as f:
            json.dump({"demo": {"clone_dir": self.repo}}, f)
        for cmd in (["git", "init", "-q", "-b", "feature/x"],
                    ["git", "config", "user.email", "t@t"],
                    ["git", "config", "user.name", "t"],
                    ["git", "add", "-A"],
                    ["git", "commit", "-qm", "init"]):
            subprocess.run(cmd, cwd=self.repo, capture_output=True)
        # no registry hint and no origin -> HEAD is the only thing left to say
        self.assertEqual(agent_init.scan("demo")["default_branch"], "feature/x")
        with open(os.path.join(self.tmp, "projects.json"), "w", encoding="utf-8") as f:
            json.dump({"demo": {"clone_dir": self.repo, "default_branch": "main"}}, f)
        self.assertEqual(agent_init.scan("demo")["default_branch"], "main")


class RepoUrlTest(unittest.TestCase):
    def test_https_ssh_and_nested_groups(self):
        cases = {
            "https://gitlab.fis.vn/kita/eintelligent.git": ("eintelligent", "kita/eintelligent"),
            "https://gitlab.fis.vn/kita/eintelligent": ("eintelligent", "kita/eintelligent"),
            "https://host/a/b/c.git": ("c", "a/b/c"),
            "git@gitlab.fis.vn:kita/eintelligent.git": ("eintelligent", "kita/eintelligent"),
            "https://host/group/name/": ("name", "group/name"),
        }
        for url, expected in cases.items():
            self.assertEqual(agent_init.parse_repo_url(url), expected, url)

    def test_a_url_with_no_project_path_is_an_error(self):
        with self.assertRaises(ValueError):
            agent_init.parse_repo_url("https://gitlab.fis.vn")


class AddTest(Fixture):
    def test_dry_run_clones_nothing(self):
        out = agent_init.add("https://host/acme/newthing.git", yes=False)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["name"], "newthing")
        self.assertEqual([s["step"] for s in out["steps"]],
                         ["clone", "lookup", "register", "draft"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "newthing")))

    def test_a_failed_clone_reports_instead_of_half_registering(self):
        out = agent_init.add("https://invalid.invalid/x/y.git", yes=True)
        self.assertTrue(out.get("error"))
        self.assertEqual(out["step"], "clone")
        with open(os.path.join(self.tmp, "projects.json"), encoding="utf-8") as f:
            self.assertNotIn("y", json.load(f))


class LayoutTest(Fixture):
    """A modular repo and a layered one must both yield usable structure."""

    def test_layered_repo_reports_packages(self):
        base = os.path.join(self.repo, "src", "main", "java", "com", "acme", "demo")
        shutil.rmtree(os.path.join(base, "module"))
        for pkg in ("domain", "service", "web", "repository"):
            os.makedirs(os.path.join(base, pkg))
        f = agent_init.scan("demo")
        self.assertEqual(f["layout"]["base_package"], "com.acme.demo")
        self.assertEqual(f["layout"]["packages"],
                         ["domain", "repository", "service", "web"])
        self.assertNotIn("modules", f["layout"])

    def test_modular_repo_still_reports_modules(self):
        f = agent_init.scan("demo")
        self.assertEqual(f["layout"]["modules"], ["billing", "task"])
        self.assertNotIn("packages", f["layout"])

    def test_new_package_in_a_layered_repo_shows_as_drift(self):
        base = os.path.join(self.repo, "src", "main", "java", "com", "acme", "demo")
        shutil.rmtree(os.path.join(base, "module"))
        os.makedirs(os.path.join(base, "domain"))
        self.draft()
        agent_init.apply("demo", yes=True, draft_dir=self.draft_dir)
        os.makedirs(os.path.join(base, "saga"))
        out = agent_init.check("demo")
        self.assertEqual(out["status"], "drift")
        pkgs = [d for d in out["drift"] if d["field"] == "layout.packages"][0]
        self.assertEqual(pkgs["added"], ["saga"])


class DraftTest(Fixture):
    def test_project_yml_carries_no_secret(self):
        self.draft()
        with open(os.path.join(self.draft_dir, ".agent", "project.yml"),
                  encoding="utf-8") as f:
            text = f.read()
        for leak in ("sup3rs3cret", "demo_user", "db.internal"):
            self.assertNotIn(leak, text, f"{leak} leaked into a committed file")
        self.assertIn("mysql:", text)                 # engine is fine, host is not

    def test_writes_every_pointer(self):
        out = self.draft()
        for path in agent_init.POINTERS:
            self.assertIn(path, out["files"])

    def test_pointers_stay_thin(self):
        self.draft()
        for path, kind in agent_init.POINTERS.items():
            if kind != "pointer":
                continue
            full = os.path.join(self.draft_dir, path)
            with open(full, encoding="utf-8") as f:
                self.assertLessEqual(len(f.read().split("\n")), 12, f"{path} is not thin")

    def test_migrates_existing_vendor_doc(self):
        self._w("CLAUDE.md", VENDOR_DOC)
        out = self.draft()
        self.assertEqual(out["migrated_from"], "CLAUDE.md")
        with open(os.path.join(self.draft_dir, ".agent", "conventions.md"),
                  encoding="utf-8") as f:
            self.assertIn("web/ khong goi repository", f.read())
        with open(os.path.join(self.draft_dir, "CLAUDE.md"), encoding="utf-8") as f:
            body = f.read()
        self.assertIn("pointer", body)
        self.assertNotIn("Layer Rules", body)        # content moved, not copied

    def test_skeleton_when_no_existing_doc(self):
        out = self.draft()
        self.assertIsNone(out["migrated_from"])
        with open(os.path.join(self.draft_dir, ".agent", "architecture.md"),
                  encoding="utf-8") as f:
            self.assertIn("`task`", f.read())        # modules seeded from scan

    def test_ignoring_one_of_a_vendors_two_locations_is_not_a_warning(self):
        """Cursor has two candidate paths. `.cursor/` being ignored is harmless
        while `.cursorrules` survives — flagging it would be noise."""
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=self.repo, capture_output=True)
        self._w(".gitignore", ".cursor/\n")
        out = self.draft()
        self.assertEqual(out["ignored_pointers"], [])

    def test_a_vendor_cut_off_entirely_is_reported(self):
        """Ignore both Cursor locations and the vendor is genuinely unreachable."""
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=self.repo, capture_output=True)
        self._w(".gitignore", ".cursor/\n.cursorrules\n")
        out = self.draft()
        self.assertEqual(sorted(out["ignored_pointers"]),
                         [".cursor/rules/00-agent.mdc", ".cursorrules"])
        applied = agent_init.apply("demo", yes=True, draft_dir=self.draft_dir)
        self.assertIn("warning", applied)
        self.assertEqual(agent_init.check("demo")["status"], "drift")

    def test_no_warning_when_nothing_is_ignored(self):
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=self.repo, capture_output=True)
        out = self.draft()
        self.assertEqual(out["ignored_pointers"], [])

    def test_rerunning_draft_preserves_migrated_knowledge(self):
        """After the first apply, CLAUDE.md is our own pointer. Re-running draft
        must not treat that pointer as the knowledge source and overwrite the
        architecture with five lines of navigation text."""
        self._w("CLAUDE.md", VENDOR_DOC)
        self.draft()
        agent_init.apply("demo", yes=True, draft_dir=self.draft_dir)
        arch = os.path.join(self.repo, ".agent", "architecture.md")
        with open(arch, encoding="utf-8") as f:
            before = f.read()
        self.assertIn("Module Structure", before)

        out = self.draft()                       # second run
        self.assertIsNone(out["migrated_from"])
        self.assertIn(".agent/architecture.md", out["preserved"])
        agent_init.apply("demo", yes=True, draft_dir=self.draft_dir)
        with open(arch, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)   # byte-identical, nothing lost

    def test_a_generated_pointer_is_never_a_knowledge_source(self):
        self._w("CLAUDE.md", agent_init._pointer_doc("CLAUDE.md"))
        out = self.draft()
        self.assertIsNone(out["migrated_from"])

    def test_both_cursor_conventions_get_a_pointer(self):
        """Which one Cursor reads is not worth guessing: four lines each."""
        out = self.draft()
        self.assertIn(".cursorrules", out["files"])
        self.assertIn(".cursor/rules/00-agent.mdc", out["files"])

    def test_decisions_points_at_existing_adrs(self):
        self.draft()
        with open(os.path.join(self.draft_dir, ".agent", "decisions", "README.md"),
                  encoding="utf-8") as f:
            self.assertIn("docs/adr", f.read())


class ApplyTest(Fixture):
    def test_dry_run_writes_nothing(self):
        self.draft()
        out = agent_init.apply("demo", yes=False, draft_dir=self.draft_dir)
        self.assertFalse(out["ok"])
        self.assertTrue(out["dry_run"])
        self.assertFalse(os.path.exists(os.path.join(self.repo, "AGENTS.md")))

    def test_plan_marks_overwrite_vs_create(self):
        self._w("CLAUDE.md", VENDOR_DOC)
        self.draft()
        out = agent_init.apply("demo", yes=False, draft_dir=self.draft_dir)
        actions = {p["file"]: p["action"] for p in out["plan"]}
        self.assertEqual(actions["CLAUDE.md"], "overwrite")
        self.assertEqual(actions["AGENTS.md"], "create")

    def test_apply_writes_into_the_repo(self):
        self.draft()
        out = agent_init.apply("demo", yes=True, draft_dir=self.draft_dir)
        self.assertTrue(out["ok"])
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "AGENTS.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.repo, ".agent", "project.yml")))

    def test_apply_without_draft_is_an_error(self):
        with self.assertRaises(ValueError):
            agent_init.apply("demo", yes=True, draft_dir=os.path.join(self.tmp, "nope"))


class DocsTieringTest(Fixture):
    """Sorting an existing documentation pile into tiers an agent can trust."""

    def _docs(self):
        for rel in (
            "docs/adr/0001-x.md",
            "docs/PHASE2_PROGRESS.md",
            "docs/plan1.md",
            "docs/WAVE1_REPORT.md",
            "docs/domain-model-rules.md",
            "docs/api-contract-freeze.md",
            "docs/perf/remediation-plan.md",
            "docs/api/task-api.md",
        ):
            self._w(rel, "# " + os.path.basename(rel) + os.linesep)

    def test_adr_is_canon(self):
        self._docs()
        plan = {i["src"]: i for i in agent_init.plan_docs(self.repo)}
        adr = plan["docs/adr/0001-x.md"]
        self.assertEqual(adr["bucket"], "canon")
        self.assertEqual(adr["dst"], ".agent/decisions/0001-x.md")

    def test_point_in_time_docs_are_archived(self):
        self._docs()
        plan = {i["src"]: i for i in agent_init.plan_docs(self.repo)}
        for name in ("PHASE2_PROGRESS.md", "plan1.md", "WAVE1_REPORT.md"):
            item = plan["docs/" + name]
            self.assertEqual(item["bucket"], "archive", name)
            self.assertEqual(item["dst"], "docs/archive/" + name)

    def test_cross_cutting_docs_stay_authoritative(self):
        self._docs()
        plan = {i["src"]: i for i in agent_init.plan_docs(self.repo)}
        self.assertEqual(plan["docs/domain-model-rules.md"]["bucket"], "guide")
        self.assertEqual(plan["docs/api-contract-freeze.md"]["dst"],
                         "docs/guides/api-contract-freeze.md")

    def test_curated_subtrees_are_left_alone_by_default(self):
        """Relocating them would rewrite every source comment that links there."""
        self._docs()
        plan = {i["src"]: i for i in agent_init.plan_docs(self.repo)}
        for src in ("docs/perf/remediation-plan.md", "docs/api/task-api.md"):
            self.assertEqual(plan[src]["bucket"], "reference")
            self.assertFalse(plan[src]["moves"])
            self.assertEqual(plan[src]["dst"], src)

    def test_include_reference_opts_into_the_bigger_move(self):
        self._docs()
        plan = {i["src"]: i for i in agent_init.plan_docs(self.repo, include_reference=True)}
        item = plan["docs/perf/remediation-plan.md"]
        self.assertTrue(item["moves"])
        self.assertEqual(item["dst"], "docs/reference/perf/remediation-plan.md")

    def test_dry_run_moves_nothing(self):
        self._docs()
        out = agent_init.docs("demo", yes=False)
        self.assertTrue(out["dry_run"])
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "docs", "plan1.md")))

    def test_apply_moves_and_rewrites_source_comments(self):
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=self.repo, capture_output=True)
        self._docs()
        self._w("src/main/java/com/acme/demo/Svc.java",
                "// see docs/domain-model-rules.md and docs/adr/0001-x.md" + os.linesep)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, capture_output=True)
        self.draft()
        agent_init.apply("demo", yes=True, draft_dir=self.draft_dir)

        out = agent_init.docs("demo", yes=True)
        self.assertTrue(out["ok"], out)
        self.assertFalse(os.path.isfile(os.path.join(self.repo, "docs", "plan1.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "docs", "archive", "plan1.md")))
        self.assertTrue(os.path.isfile(
            os.path.join(self.repo, ".agent", "decisions", "0001-x.md")))
        with open(os.path.join(self.repo, "src", "main", "java", "com", "acme",
                               "demo", "Svc.java"), encoding="utf-8") as f:
            java = f.read()
        self.assertIn("docs/guides/domain-model-rules.md", java)
        self.assertIn(".agent/decisions/0001-x.md", java)

    def test_index_warns_about_the_archive(self):
        self._docs()
        agent_init.docs("demo", yes=True)
        with open(os.path.join(self.repo, ".agent", "index.md"), encoding="utf-8") as f:
            idx = f.read()
        self.assertIn("NOT current intent", idx)
        self.assertIn("docs/archive/plan1.md", idx)

    def test_project_yml_is_refreshed_after_the_move(self):
        self._docs()
        self.draft()
        agent_init.apply("demo", yes=True, draft_dir=self.draft_dir)
        agent_init.docs("demo", yes=True)
        with open(os.path.join(self.repo, ".agent", "project.yml"), encoding="utf-8") as f:
            committed = agent_init.yaml_load(f.read())
        self.assertEqual(committed["knowledge"]["adr_dir"], ".agent/decisions")


class ObservabilityStandardTest(Fixture):
    """The standard is a policy every repo must meet. These tests pin the two
    things that make it worth having: it must not pass a repo that cannot
    actually be watched, and it must not fail one that has nothing to watch."""

    LISTENER = ('@KafkaListener(topics = "t")\n'
                'public void onMessage(String m) {}\n')

    def _obs(self):
        return agent_init.scan("demo")["observability"]

    def _mgmt(self, include):
        self._w("src/main/resources/config/application.yml",
                "management:\n  endpoints:\n    web:\n      exposure:\n"
                "        include: [%s]\n" % include)

    # --- OBS-3: the rule that catches "broken but health is green" ---------

    def test_async_intake_without_a_meter_fails(self):
        self._w("src/main/java/com/acme/demo/L.java", self.LISTENER)
        obs = self._obs()
        self.assertIn("OBS-3", obs["fails"])
        self.assertEqual(obs["async_intake_files"], 1)

    def test_async_intake_with_a_meter_passes(self):
        self._w("src/main/java/com/acme/demo/L.java", self.LISTENER)
        self._w("src/main/java/com/acme/demo/M.java",
                "private final MeterRegistry registry;\n")
        self.assertNotIn("OBS-3", self._obs()["fails"])

    def test_no_async_intake_is_not_a_violation(self):
        """A plain REST service has nothing to instrument — passing it
        vacuously is right; failing it would train people to ignore the gate."""
        obs = self._obs()
        self.assertEqual(obs["async_intake_files"], 0)
        self.assertNotIn("OBS-3", obs["fails"])

    def test_scheduled_job_counts_as_intake(self):
        self._w("src/main/java/com/acme/demo/S.java",
                '@Scheduled(cron = "0 * * * * *")\npublic void run() {}\n')
        self.assertIn("OBS-3", self._obs()["fails"])

    # --- OBS-4: correlation has to be at the boundary, not just anywhere ---

    def test_correlation_away_from_intake_does_not_count(self):
        """A traceId minted inside some unrelated class proves nothing about
        an id surviving a hop through Kafka."""
        self._w("src/main/java/com/acme/demo/L.java", self.LISTENER)
        self._w("src/main/java/com/acme/demo/Unrelated.java",
                "String traceId = UUID.randomUUID().toString();\n")
        obs = self._obs()
        self.assertGreater(obs["correlation_files"], 0)
        self.assertEqual(obs["intake_with_correlation"], 0)
        self.assertIn("OBS-4", obs["fails"])

    def test_correlation_at_intake_counts(self):
        self._w("src/main/java/com/acme/demo/L.java",
                'MDC.put("traceId", h);\n' + self.LISTENER)
        self.assertNotIn("OBS-4", self._obs()["fails"])

    # --- OBS-1/2: read from config, not guessed from dependencies ----------

    def test_health_and_metrics_read_from_exposure_list(self):
        self._mgmt("'health', 'info', 'prometheus'")
        obs = self._obs()
        self.assertTrue(obs["health_endpoint"])
        self.assertTrue(obs["metrics_endpoint"])
        self.assertNotIn("OBS-1", obs["fails"])
        self.assertNotIn("OBS-2", obs["fails"])

    def test_missing_management_block_fails_both(self):
        obs = self._obs()
        self.assertIn("OBS-1", obs["fails"])
        self.assertIn("OBS-2", obs["fails"])

    def test_commented_out_exposure_does_not_count(self):
        self._w("src/main/resources/config/application.yml",
                "#        include: ['health', 'prometheus']\n")
        self.assertIn("OBS-1", self._obs()["fails"])

    # --- OBS-5: the skeleton must not satisfy the requirement -------------

    def test_operations_skeleton_does_not_satisfy_obs5(self):
        self.draft()
        agent_init.apply("demo", yes=True, draft_dir=self.draft_dir)
        ops = os.path.join(self.repo, ".agent", "operations.md")
        self.assertTrue(os.path.isfile(ops))
        self.assertIn("OBS-5", self._obs()["fails"])

    def test_filled_operations_satisfies_obs5(self):
        os.makedirs(os.path.join(self.repo, ".agent"), exist_ok=True)
        self._w(".agent/operations.md", "# demo — Operations\n\n" + ("day. " * 150))
        self.assertNotIn("OBS-5", self._obs()["fails"])

    # --- vendored trees must not be mistaken for our instrumentation ------

    def test_vendored_jdk_tree_is_ignored(self):
        self._w("src/main/java/com/acme/demo/L.java", self.LISTENER)
        self._w("src/main/java/com/acme/demo/jdk/Vendor.java",
                "private final MeterRegistry registry;\n")
        self.assertIn("OBS-3", self._obs()["fails"])


class ComplianceReportTest(Fixture):
    def _applied(self):
        self.draft()
        agent_init.apply("demo", yes=True, draft_dir=self.draft_dir)

    def test_compliance_is_reported_separately_from_drift(self):
        """Two different problems, two different fixes: a repo can be perfectly
        in sync with its knowledge and still not meet the standard."""
        self._applied()
        out = agent_init.check("demo")
        self.assertEqual(out["status"], "in-sync", out.get("drift"))
        self.assertFalse(out["compliance"]["ok"])
        self.assertIn("OBS-5", out["compliance"]["failed_required"])

    def test_failing_requirement_reports_how_to_satisfy_it(self):
        self._applied()
        reqs = agent_init.check("demo")["compliance"]["requirements"]
        self.assertEqual(reqs["OBS-5"]["status"], "fail")
        self.assertIn("how", reqs["OBS-5"])

    def test_uninstrumented_intake_files_are_named(self):
        self._w("src/main/java/com/acme/demo/L.java",
                '@KafkaListener(topics = "t")\npublic void on(String m) {}\n')
        self._applied()
        out = agent_init.check("demo")
        self.assertIn("OBS-3", out["compliance"]["failed_required"])
        self.assertTrue(any("L.java" in p
                            for p in out["compliance"]["uninstrumented_intake"]))

    def test_entry_file_carries_the_warning(self):
        """The entry file is the one thing every agent reads. An unmet
        requirement has to be visible there, not filed away."""
        self.draft()
        with open(os.path.join(self.draft_dir, "AGENTS.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("CHƯA đạt chuẩn quan sát", md)
        self.assertIn("OBS-5", md)

    def test_standard_text_covers_every_rule(self):
        """Text and gate come from one table, so they cannot disagree."""
        self.draft()
        with open(os.path.join(self.draft_dir, ".agent", "standards",
                               "observability.md"), encoding="utf-8") as f:
            doc = f.read()
        for rule in agent_init.OBSERVABILITY_STANDARD:
            self.assertIn(rule["id"], doc)
            self.assertIn(rule["title"], doc)

    def test_observability_block_survives_the_yaml_round_trip(self):
        self._applied()
        with open(os.path.join(self.repo, ".agent", "project.yml"),
                  encoding="utf-8") as f:
            committed = agent_init.yaml_load(f.read())
        obs = committed["observability"]
        self.assertEqual(obs["standard_version"],
                         agent_init.OBSERVABILITY_VERSION)
        self.assertIn("OBS-5", obs["fails"])


class EntryDocTest(Fixture):
    """The entry file is the only thing some agents read. A knowledge file that
    exists on disk but is missing from its table is invisible to them."""

    def _agents_md(self):
        self.draft()
        with open(os.path.join(self.draft_dir, "AGENTS.md"), encoding="utf-8") as f:
            return f.read()

    def test_lists_the_standard_three(self):
        md = self._agents_md()
        for fn in ("architecture.md", "conventions.md", "learned.md"):
            self.assertIn("`.agent/%s`" % fn, md)

    def test_hand_written_knowledge_file_is_listed(self):
        self.draft()
        extra = os.path.join(self.draft_dir, ".agent", "flows.md")
        with open(extra, "w", encoding="utf-8") as f:
            f.write("# demo — Flows\n\nbody\n")
        # draft() preserves existing .agent/*.md, so re-drafting keeps flows.md
        agent_init.apply("demo", yes=True, draft_dir=self.draft_dir)
        self.draft()
        with open(os.path.join(self.draft_dir, "AGENTS.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("`.agent/flows.md`", md)
        self.assertIn("What happens at runtime", md)

    def test_unknown_file_falls_back_to_its_own_heading(self):
        self.draft()
        with open(os.path.join(self.draft_dir, ".agent", "billing.md"),
                  "w", encoding="utf-8") as f:
            f.write("# demo — How invoices are priced\n\nbody\n")
        agent_init.apply("demo", yes=True, draft_dir=self.draft_dir)
        self.draft()
        with open(os.path.join(self.draft_dir, "AGENTS.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("| `.agent/billing.md` | How invoices are priced |", md)

    def test_index_md_is_not_offered_as_knowledge(self):
        self.draft()
        with open(os.path.join(self.draft_dir, ".agent", "index.md"),
                  "w", encoding="utf-8") as f:
            f.write("# demo — document index\n")
        agent_init.apply("demo", yes=True, draft_dir=self.draft_dir)
        self.draft()
        with open(os.path.join(self.draft_dir, "AGENTS.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertNotIn("`.agent/index.md`", md)


class CheckTest(Fixture):
    def _applied(self):
        self.draft()
        agent_init.apply("demo", yes=True, draft_dir=self.draft_dir)

    def test_missing_before_init(self):
        out = agent_init.check("demo")
        self.assertEqual(out["status"], "missing")

    def test_in_sync_right_after_apply(self):
        self._applied()
        out = agent_init.check("demo")
        self.assertEqual(out["status"], "in-sync", out.get("drift"))

    def test_new_module_shows_as_drift(self):
        self._applied()
        os.makedirs(os.path.join(self.repo, "src", "main", "java", "com", "acme",
                                 "demo", "module", "reporting"))
        out = agent_init.check("demo")
        self.assertEqual(out["status"], "drift")
        mods = [d for d in out["drift"] if d["field"] == "layout.modules"][0]
        self.assertEqual(mods["added"], ["reporting"])

    def test_ci_appearing_shows_as_drift(self):
        self._applied()
        self._w(".gitlab-ci.yml", "stages: [test]\n")
        out = agent_init.check("demo")
        fields = [d["field"] for d in out["drift"]]
        self.assertIn("verification.ci", fields)


if __name__ == "__main__":
    unittest.main()
