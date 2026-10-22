"""jenkins_scan: repo + branch of a job from its inline pipeline / build console; registry plan + apply."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude" / "skills" / "dev-automation" / "tools"))
import jenkins_scan as JS  # noqa: E402

H = "gitlab.x"

COMMENTED_ALT = """pipeline { environment {
    GIT_REPO = "kita/ioc/ioc-gateway.git"
    GIT_BRANCH = "prod"
    // GIT_BRANCH = "develop"
  }
  stages { stage('c') { steps { sh 'git clone -b ${GIT_BRANCH} https://oauth2:${CER_GIT}@gitlab.x/${GIT_REPO}' } } } }"""

MULTI_CLONE = """environment {
  GIT_SOURCE_REPO = "kita/booking"
  GIT_BRANCH = "uat"
  GIT_CONFIG_UAT_REPO = "econtract/config_uat"
  GIT_CONFIG_UAT_BRANCH = "master"
  GIT_IAC_REPO = "devop/helm-charts/iac.git"
}
sh 'git -c http.sslVerify=false clone --depth 1 -b "$GIT_CONFIG_UAT_BRANCH" "https://oauth2:$CER_GIT@gitlab.x/$GIT_CONFIG_UAT_REPO" cfg'
sh 'git -c http.sslVerify=false clone --depth 1 -b "$GIT_BRANCH" "https://oauth2:$CER_GIT@gitlab.x/$GIT_SOURCE_REPO" "$GIT_FOLDER"'
git clone --depth 1 -b main "https://oauth2:${CER_GIT}@gitlab.x/${GIT_IAC_REPO}" iac
"""

FULL_URL_VAR = """environment { GIT_REPO = "gitlab.x/kita/python-utils-gpu.git"
  GIT_BRANCH = "develop" }
  // sh 'git clone --depth 1 -b ${GIT_BRANCH} https://oauth2:${GIT_CRE}@${GIT_REPO}'
  git clone --depth 1 -b "${GIT_BRANCH}" "https://oauth2:${GIT_CRE}@${GIT_REPO}" "${GIT_FOLDER}"
"""

LITERAL_URL_PARAM = """parameters { string(name: 'GIT_BRANCH', defaultValue: 'merge/rfc', description: 'b') }
stages { sh 'git clone -b ${GIT_BRANCH} https://oauth2:${CER_GIT}@gitlab.x/idaas/etask.git' }"""

CONSOLE = """[Pipeline] sh
+ git clone --depth 1 -b main https://oauth2:****@gitlab.x/devop/helm-charts/charts.git charts
+ git clone -b hotfix-tenant-guard https://oauth2:****@gitlab.x/kita/eclm.git
Cloning into 'eclm'...
"""


class ParseTest(unittest.TestCase):
    def test_commented_alternative_is_ignored(self):
        r = JS.parse_script(COMMENTED_ALT, H)
        self.assertEqual(r["repos"], [(H, "kita/ioc/ioc-gateway")])
        self.assertEqual(r["branches"], ["prod"])

    def test_main_clone_is_the_git_branch_one_not_config_or_iac(self):
        r = JS.parse_script(MULTI_CLONE, H)
        self.assertEqual(r["repos"], [(H, "kita/booking")])
        self.assertEqual(r["branches"], ["uat"])
        self.assertIn("econtract/config_uat", r["aux"])

    def test_repo_variable_holding_a_full_url(self):
        r = JS.parse_script(FULL_URL_VAR, H)
        self.assertEqual(r["repos"], [("gitlab.x", "kita/python-utils-gpu")])
        self.assertEqual(r["branches"], ["develop"])

    def test_literal_url_and_parameter_default(self):
        r = JS.parse_script(LITERAL_URL_PARAM, H)
        self.assertEqual(r["repos"], [(H, "idaas/etask")])
        self.assertEqual(r["branches"], ["merge/rfc"])

    def test_console_skips_charts_and_masks_nothing_sensitive(self):
        r = JS.parse_console(CONSOLE, H)
        self.assertEqual(r["repos"], [(H, "kita/eclm")])
        self.assertEqual(r["branches"][0], "hotfix-tenant-guard")

    def test_r1_host_kept_ambiguous_suffix_helper_and_console(self):
        r = JS.parse_script('environment { GIT_REPO = "g/app"\n GIT_BRANCH = "prod" }\n'
                            "git clone -b ${GIT_BRANCH} https://oauth2:${CER_GIT}@gitlab.foreign/${GIT_REPO}", H)
        self.assertEqual(r["repos"], [("gitlab.foreign", "g/app")])                    # R1 #1
        r = JS.parse_script('GIT_REPO = "g/app"\nGIT_BRANCH = "old"\nGIT_BRANCH = "prod"\n'
                            "git clone -b ${GIT_BRANCH} https://gitlab.x/${GIT_REPO}", H)
        self.assertEqual(r["branches"], [])                                             # R1 #2 ambiguous
        r = JS.parse_script('GIT_REPO = "g/app"\nGIT_BRANCH = "release"\n'
                            "git clone -b $GIT_BRANCH-prod https://gitlab.x/$GIT_REPO", H)
        self.assertEqual(r["branches"], ["release-prod"])                               # R1 #5 suffix kept
        r = JS.parse_script('GIT_BRANCH = "uat"\nAPP_BRANCH = "main"\nAPP = "g/app"\nCFG = "g/config_uat"\n'
                            "git clone -b $GIT_BRANCH https://gitlab.x/$CFG cfg\n"
                            "git clone -b $APP_BRANCH https://gitlab.x/$APP app", H)
        self.assertEqual((r["repos"], r["branches"]), ([(H, "g/app")], ["main"]))      # R1 #6 helper excluded
        c = JS.parse_console("+ git clone https://oauth2:****@gitlab.x/g/app.git\n"
                             "+ git clone -b main https://gitlab.x/devop/charts.git\n+ git checkout feature-x\n", H)
        self.assertEqual((c["repos"], c["branches"]), ([(H, "g/app")], []))           # R1 #4 no stray branch

    def test_r2_dynamic_reassignment_and_scm_branch_variable(self):
        r = JS.parse_script('GIT_REPO = "g/app"\nGIT_BRANCH = "prod"\nGIT_BRANCH = params.BRANCH\n'
                            "sh 'git clone -b $GIT_BRANCH https://gitlab.x/$GIT_REPO'", H)
        self.assertEqual(r["branches"], [])                                       # R2 #2 unknown at run time
        r = JS.parse_script('GIT_REPO = "g/app"\nGIT_BRANCH = "prod"\nenv.GIT_BRANCH = params.BRANCH\n'
                            "sh 'git clone -b $GIT_BRANCH https://gitlab.x/$GIT_REPO'", H)
        self.assertEqual(r["branches"], [])                                       # R3 #2 env.X override
        r = JS.parse_script('GIT_REPO = "g/app"\nenv.GIT_BRANCH = "uat"\n'
                            "sh 'git clone -b $GIT_BRANCH https://gitlab.x/$GIT_REPO'", H)
        self.assertEqual(r["branches"], ["uat"])                                  # env.X literal still read
        r = JS.parse_script("checkout([$class: 'GitSCM', branches: [[name: '$GIT_BRANCH']], "
                            "userRemoteConfigs: [[url: 'https://gitlab.x/g/app.git']]])", H)
        self.assertEqual((r["repos"], r["branches"]), ([(H, "g/app")], []))      # R2 #3 never the literal $VAR

    def test_r2_console_branch_only_for_the_same_repo(self):
        cfg = JS.parse_script("sh 'git clone https://gitlab.x/g/app.git'", H)
        con = JS.parse_console("+ git clone -b helper https://gitlab.x/g/other.git", H)
        with mock.patch.object(JS, "_get", side_effect=[
                (200, "<script>" + "sh 'git clone https://gitlab.x/g/app.git'" + "</script>"),
                (200, json.dumps({"lastBuild": {"number": 3, "result": "SUCCESS", "timestamp": 1},
                                  "lastSuccessfulBuild": {"number": 3, "timestamp": 1}})),
                (200, "+ git clone -b helper https://gitlab.x/g/other.git")]):
            rec = JS.scan_job("DEMO/x/app", H)
        self.assertEqual(cfg["repos"], [(H, "g/app")])
        self.assertEqual(con["repos"], [(H, "g/other")])
        self.assertEqual(rec["branches"], [])                                     # R2 #1 not paired with g/app
        with mock.patch.object(JS, "_get", side_effect=[
                (200, "<script>sh 'git clone https://gitlab.x/g/app.git'</script>"),
                (200, json.dumps({"lastBuild": {"number": 3, "result": "SUCCESS", "timestamp": 1},
                                  "lastSuccessfulBuild": {"number": 3, "timestamp": 1}})),
                (200, "+ git clone -b foreign https://gitlab.other/g/app.git")]):
            rec = JS.scan_job("DEMO/x/app", H)
        self.assertEqual(rec["branches"], [])                                     # R3 #1 same path, other host

    def test_line_continuation_clone(self):
        r = JS.parse_script('GIT_REPO = "idaas/ewriter-fe.git"\nGIT_BRANCH = "prod-backup"\n'
                            '// GIT_BRANCH = "binhnb_unilever"\nsh """\n  git clone --depth 1 -b "$GIT_BRANCH" \\\n'
                            '    "https://oauth2:${CER_GIT}@gitlab.x/${GIT_REPO}" app\n"""', H)
        self.assertEqual((r["repos"], r["branches"]), ([(H, "idaas/ewriter-fe")], ["prod-backup"]))

    def test_env_of_top_folder(self):
        self.assertEqual(JS.env_of("PROD-KYTA-NEW/P-KYTA/etask/etask"), "prod")
        self.assertEqual(JS.env_of("Special-SANBOX/P-KYTA/etask/etask"), "sandbox")
        self.assertIsNone(JS.env_of("Special-SonarQube-IDAAS/etask"))
        self.assertIsNone(JS.env_of("DEV-K8S/Example/x"))
        self.assertEqual(JS.env_of("NEW-TOP/x/y"), "?")                    # unknown folder: not guessed


class PlanApplyTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="jks-")
        self.env = mock.patch.dict(os.environ, {"WORK_DIR": self.work})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.reg = {"etask": {"repo_path": "idaas/etask", "environments": {
                        "prod": {"jenkins": {"job": "job/PROD/job/etask", "_note": "keep me"}}}},
                    "eclm": {"repo_path": "kita/eclm"},
                    "mirror": {"repo_path": "kita/eclm", "remote_host": "github.com"}}
        with open(os.path.join(self.work, "projects.json"), "w", encoding="utf-8") as f:
            json.dump(self.reg, f)
        job = lambda path, env, repo, br: {"path": path, "job": JS.job_api_path(path), "env": env,
                                           "repos": [{"host": H, "path": repo}], "branches": [br], "source": "config"}
        self.scan = {"scanned_at": "t", "gitlab_host": H, "jobs": [
            job("PROD/etask", "prod", "idaas/etask", "merge/rfc"),
            job("DEMO/x/eclm", "demo", "kita/eclm", "develop"),
            job("DEMO/x/edlm", "demo", "kita/eclm", "develop_edlm"),
            job("OTHER/etask-copy", "dev", "idaas/etask", "dev"),
            job("SONAR/etask", None, "idaas/etask", "x"),
            {"path": "P/z", "job": "job/P/job/z", "env": "prod", "repos": [{"host": "other.host", "path": "idaas/etask"}],
             "branches": ["evil"]}]}
        self.scan_path = os.path.join(self.work, "scan.json")
        with open(self.scan_path, "w", encoding="utf-8") as f:
            json.dump(self.scan, f)

    def test_plan_primary_job_and_foreign_host_ignored(self):
        p = JS.plan(self.scan, self.reg)
        self.assertEqual(p["projects"]["eclm"]["demo"]["primary"], "job/DEMO/job/x/job/eclm")
        self.assertEqual(p["projects"]["eclm"]["demo"]["branch"], "develop")
        self.assertNotIn("mirror", p["projects"])                               # foreign-host registry entry
        self.assertEqual(p["projects"]["etask"]["prod"]["branch"], "merge/rfc")   # other.host job not counted

    def test_apply_keeps_notes_and_never_replaces_a_different_job(self):
        self.scan["jobs"][0]["job"] = "job/PROD/job/etask-new"
        with open(self.scan_path, "w", encoding="utf-8") as f:
            json.dump(self.scan, f)
        out = JS.apply(self.scan_path)
        self.assertEqual([c["project"] for c in out["conflicts"]], ["etask"])
        with open(os.path.join(self.work, "projects.json"), encoding="utf-8") as f:
            reg = json.load(f)
        self.assertEqual(reg["etask"]["environments"]["prod"]["jenkins"],
                         {"job": "job/PROD/job/etask", "_note": "keep me"})          # untouched
        demo = reg["eclm"]["environments"]["demo"]
        self.assertEqual(demo["deployed_branch"], "develop")
        self.assertEqual(len(demo["jenkins"]["jobs"]), 2)
        self.assertEqual(reg["etask"]["environments"]["dev"]["deployed_branch"], "dev")
        self.assertTrue(os.path.isfile(out["backup"]))

    def test_r1_existing_secondary_job_keeps_its_own_branch_and_jobs_change_is_written(self):
        self.reg["eclm"]["environments"] = {"demo": {"jenkins": {"job": "job/DEMO/job/x/job/edlm"}}}
        with open(os.path.join(self.work, "projects.json"), "w", encoding="utf-8") as f:
            json.dump(self.reg, f)
        JS.apply(self.scan_path)
        with open(os.path.join(self.work, "projects.json"), encoding="utf-8") as f:
            demo = json.load(f)["eclm"]["environments"]["demo"]
        self.assertEqual((demo["jenkins"]["job"], demo["deployed_branch"]),
                         ("job/DEMO/job/x/job/edlm", "develop_edlm"))                 # R1 #3
        self.scan["jobs"] = [j for j in self.scan["jobs"] if j["path"] != "DEMO/x/eclm"]
        with open(self.scan_path, "w", encoding="utf-8") as f:
            json.dump(self.scan, f)
        out = JS.apply(self.scan_path)
        with open(os.path.join(self.work, "projects.json"), encoding="utf-8") as f:
            demo = json.load(f)["eclm"]["environments"]["demo"]
        self.assertTrue(out["written"])                                                 # R1 #7
        self.assertNotIn("jobs", demo["jenkins"])


if __name__ == "__main__":
    unittest.main()
