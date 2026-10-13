#!/usr/bin/env python3
"""cg.py — hunk parsing + diff→method mapping on a tiny fake index (no codegraph needed)."""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".claude", "skills",
                                                "dev-automation", "tools")))
import cg  # noqa: E402


def git(d, *a):
    return subprocess.run(["git", "-C", d, *a], capture_output=True, text=True, check=True).stdout.strip()


class CgTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="cg-test-")
        git(self.d, "init", "-q")
        git(self.d, "config", "user.email", "t@t")
        git(self.d, "config", "user.name", "t")
        src = "\n".join(f"line {i}" for i in range(1, 41)) + "\n"
        with open(os.path.join(self.d, "A.java"), "w") as f:
            f.write(src)
        git(self.d, "add", ".")
        git(self.d, "commit", "-qm", "base")
        self.base = git(self.d, "rev-parse", "HEAD")
        with open(os.path.join(self.d, "A.java"), "w") as f:
            f.write(src.replace("line 25\n", "line 25 changed\n"))
        git(self.d, "commit", "-qam", "change")
        self.head = git(self.d, "rev-parse", "HEAD")
        os.makedirs(os.path.join(self.d, ".codegraph"))
        con = sqlite3.connect(os.path.join(self.d, ".codegraph", "codegraph.db"))
        con.execute("create table nodes (kind, name, qualified_name, file_path, start_line, end_line)")
        con.executemany("insert into nodes values (?,?,?,?,?,?)", [
            ("method", "a", "p::A::a", "A.java", 1, 10),
            ("method", "b", "p::A::b", "A.java", 20, 30),
            ("field", "f", "p::A::f", "A.java", 25, 25),
            ("class", "A", "p::A", "A.java", 1, 40)])
        con.commit()
        con.close()

    def test_hunks_new_side(self):
        self.assertEqual(cg._hunks(self.d, self.base, self.head), {"A.java": [(25, 25)]})

    def test_only_touched_methods(self):
        syms = cg.changed_symbols(self.d, self.base, self.head)
        self.assertEqual([s["qualified"] for s in syms], ["p::A::b"])

    def test_no_index_degrades(self):
        os.remove(os.path.join(self.d, ".codegraph", "codegraph.db"))
        self.assertEqual(cg.changed_symbols(self.d, self.base, self.head), [])
        self.assertEqual(cg.analyze(self.d, self.base, self.head)["status"], "no-index")


if __name__ == "__main__":
    unittest.main()
