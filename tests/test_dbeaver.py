#!/usr/bin/env python3
"""dbeaver.py (metadata only) — parsing, lookup, prod read-only guard. No DB, no DBeaver needed."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_TOOLS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".claude", "skills", "dev-automation", "tools")
sys.path.insert(0, os.path.abspath(_TOOLS))

import dbeaver  # noqa: E402


def _ws(conns: dict) -> str:
    root = tempfile.mkdtemp(prefix="dbeaver-ws-")
    d = os.path.join(root, "General", ".dbeaver")
    os.makedirs(d)
    with open(os.path.join(d, "data-sources.json"), "w") as f:
        json.dump({"connections": conns}, f)
    return root


CONNS = {
    "pg-1": {"name": "superset", "folder": "Dev", "provider": "postgresql",
             "configuration": {"configurationType": "URL", "host": "localhost", "port": "5432",
                               "url": "jdbc:postgresql://10.1.2.3:5433/app?currentSchema=s1", "type": "dev"}},
    "pg-2": {"name": "superset", "folder": "SIT", "provider": "postgresql",
             "configuration": {"configurationType": "MANUAL", "host": "localhost", "port": "5432",
                               "database": "sit", "type": "dev"}},
    "my-1": {"name": "kyta prd", "folder": "PROD", "provider": "mysql",
             "configuration": {"configurationType": "MANUAL", "host": "10.0.0.9", "port": "3306",
                               "database": "kyta", "type": "dev"}},
}


class DbeaverTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"DBEAVER_WORKSPACE": _ws(CONNS)})
        self.env.start()
        self.fwd = mock.patch.object(dbeaver, "_forwards_by_port", return_value={})
        self.fwd.start()

    def tearDown(self):
        mock.patch.stopall()

    def test_url_type_wins_over_manual_fields(self):
        c = dbeaver.find("Dev/superset")
        self.assertEqual((c["host"], c["port"], c["database"], c["schema"]), ("10.1.2.3", "5433", "app", "s1"))

    def test_ambiguous_name_needs_folder(self):
        with self.assertRaises(ValueError):
            dbeaver.find("superset")
        self.assertEqual(dbeaver.find("SIT/superset")["database"], "sit")

    def test_prod_folder_is_read_only(self):
        self.assertTrue(dbeaver.find("PROD/kyta prd")["protected"])
        for sql in ("update t set a=1", "with x as (delete from t returning *) select * from x", "drop table t"):
            with self.assertRaises(PermissionError):
                dbeaver.probe("PROD/kyta prd", sql, [])

    def test_live_prod_forward_protects_a_dev_label(self):
        fwd = {5432: {"local_port": 5432, "target": "svc/haproxy-db-prod", "context": "x",
                      "lens_status": "Active", "listening": True}}
        c = dbeaver.with_forward(dbeaver.find("SIT/superset"), fwd)
        self.assertTrue(c["protected"])
        fwd[5432]["lens_status"] = "Disabled"
        self.assertFalse(dbeaver.with_forward(dbeaver.find("SIT/superset"), fwd)["protected"])

    def test_prod_bypasses_from_review_are_refused(self):
        bypasses = ["select 1; do $$ begin execute 'dr'||'op table t'; end $$",   # multi-statement
                    "select 1; copy t to program 'x'",
                    "select pg_terminate_backend(123)",
                    "select * from t into outfile '/tmp/x'",
                    "select setval('s', 1)",
                    "select * from t for update"]
        for sql in bypasses:
            with self.assertRaises(PermissionError, msg=sql):
                dbeaver.probe("PROD/kyta prd", sql, [])
        with mock.patch("subprocess.run") as run:
            run.return_value.stdout = '{"rows": []}'
            dbeaver.probe("PROD/kyta prd", "select id, name from t where a = 'x';", [])
            run.assert_called_once()

    def test_retarget_flags_refused(self):
        for extra in (["--host", "prod-db"], ["--port=5432"], ["--name", "other"], ["--engine", "mysql"]):
            with self.assertRaises(PermissionError, msg=extra):
                dbeaver.probe("Dev/superset", "drop table t", extra)

    def test_prd_name_outside_prod_folder_is_protected(self):
        conns = dict(CONNS, **{"my-2": {"name": "kyta prd", "folder": "Misc", "provider": "mysql",
                                        "configuration": {"configurationType": "MANUAL", "host": "h",
                                                          "port": "3306", "database": "k", "type": "dev"}}})
        with mock.patch.dict(os.environ, {"DBEAVER_WORKSPACE": _ws(conns)}):
            self.assertTrue(dbeaver.find("Misc/kyta prd")["protected"])
            self.assertFalse(dbeaver.find("SIT/superset")["protected"])

    def test_no_credentials_in_output(self):
        blob = json.dumps(dbeaver.connections())
        self.assertNotIn("password", blob.lower())


if __name__ == "__main__":
    unittest.main()
