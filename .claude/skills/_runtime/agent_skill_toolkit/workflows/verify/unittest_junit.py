#!/usr/bin/env python3
"""Run `unittest` discovery and write a JUnit XML report — so a stdlib-only Python repo gives the
verify ladder per-test evidence (which test ran, passed, failed, was skipped).

    python unittest_junit.py -s tests [-p "test*.py"] [-t TOP] -o build/junit.xml

Exit 0 when every test passed (skips allowed), 1 otherwise — like `python -m unittest`.
"""

import argparse
import os
import sys
import time
import unittest
import xml.etree.ElementTree as ET


class _Result(unittest.TextTestResult):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.cases = []
        self._t0 = {}

    def startTest(self, test):
        self._t0[test.id()] = time.time()
        super().startTest(test)

    def _add(self, test, status, detail=""):
        tid = test.id()
        cls, _, name = tid.rpartition(".")
        self.cases.append({"classname": cls, "name": name, "status": status, "detail": detail[-2000:],
                           "time": round(time.time() - self._t0.get(tid, time.time()), 3)})

    def addSuccess(self, test):
        super().addSuccess(test)
        self._add(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._add(test, "failure", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._add(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._add(test, "skipped", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._add(test, "skipped", "expected failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._add(test, "failure", "unexpected success")


def write_junit(cases: list, path: str) -> None:
    suite = ET.Element("testsuite", name="unittest", tests=str(len(cases)),
                       failures=str(sum(c["status"] == "failure" for c in cases)),
                       errors=str(sum(c["status"] == "error" for c in cases)),
                       skipped=str(sum(c["status"] == "skipped" for c in cases)))
    for c in cases:
        tc = ET.SubElement(suite, "testcase", classname=c["classname"], name=c["name"], time=str(c["time"]))
        if c["status"] in ("failure", "error"):
            ET.SubElement(tc, c["status"], message=c["detail"][:200]).text = c["detail"]
        elif c["status"] == "skipped":
            ET.SubElement(tc, "skipped", message=c["detail"][:200])
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="unittest_junit.py")
    ap.add_argument("-s", "--start", default=".")
    ap.add_argument("-p", "--pattern", default="test*.py")
    ap.add_argument("-t", "--top", default=None)
    ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args(argv)
    sys.path.insert(0, os.getcwd())
    suite = unittest.defaultTestLoader.discover(a.start, a.pattern, a.top)
    runner = unittest.TextTestRunner(resultclass=_Result, verbosity=1)
    result = runner.run(suite)
    write_junit(result.cases, a.out)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
