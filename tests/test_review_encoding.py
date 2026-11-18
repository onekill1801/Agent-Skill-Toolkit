"""review.json written through a non-UTF-8 path (Codex + PowerShell ANSI, 16/11) must not validate."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".claude", "skills", "mr-review", "tools"))
import review_post as rp  # noqa: E402

HEAD = "a" * 40
JOB = {"diff_refs": {"head_sha": HEAD}}


def review(summary, **kw):
    r = {"head_sha": HEAD, "verdict": "COMMENT", "summary": summary, "findings": []}
    r.update(kw)
    return r


class MisEncodedTest(unittest.TestCase):
    def test_lost_letters_rejected(self):
        errs = rp.validate(JOB, review("Ch?a x?c minh b? test Maven trong m?i tr??ng review."))
        self.assertTrue(any("mis-encoded" in e for e in errs), errs)

    def test_replacement_char_rejected(self):
        errs = rp.validate(JOB, review("Ch�a x�c minh b� test"))
        self.assertTrue(any("mis-encoded" in e for e in errs), errs)

    def test_lost_letters_in_finding_rejected(self):
        f = {"severity": "minor", "title": "Thi?u test", "body": "Kh?ng c? test cho nh?nh n?y", "file": "A.java"}
        self.assertTrue(any("mis-encoded" in e for e in rp.validate(JOB, review("Tốt.", findings=[f]))))

    def test_vietnamese_and_code_question_marks_ok(self):
        r = review("Chưa xác minh test Maven. Có nên dùng `a?.b` hay `x ? y : z`? Đúng vậy?",
                   findings=[{"severity": "nit", "title": "Đặt tên", "body": "SQL `where id = ?` ổn.",
                              "file": "A.java"}])
        self.assertEqual([], rp.validate(JOB, r))


if __name__ == "__main__":
    unittest.main()
