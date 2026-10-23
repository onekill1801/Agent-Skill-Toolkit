#!/usr/bin/env python3
"""TChat — đăng một tin (có thể TAG người) vào group. Dùng cho thông báo tự
động (vd bg_notify.py báo build xong về cho người yêu cầu).

Tách riêng khỏi send.py CLI để các tool ở skill KHÁC gọi như subprocess mà không
phải import (tránh đụng module `config.py`/`client.py` trùng tên giữa các skill).

Cách dùng:
  python notify_group.py --group GID --text "✅ Build xong" \
      [--group-type SUPER_PRIVATE] [--user-id UID --user-name "Tên"]

Có --user-id + --user-name → chèn tiền tố @Tên và metadata.mentions để người đó
nhận thông báo. Thiếu thì gửi text thường. In JSON kết quả; lỗi → exit != 0.
"""

import argparse
import sys

import client
import config
import send


def main() -> int:
    p = argparse.ArgumentParser(prog="notify_group.py")
    p.add_argument("--group", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--group-type", dest="group_type", default=None,
                   help="SUPER_PRIVATE/PRIVATE/PUBLIC (tự dò nếu bỏ trống)")
    p.add_argument("--user-id", dest="user_id", default=None, help="id người cần tag")
    p.add_argument("--user-name", dest="user_name", default=None, help="tên hiển thị để tag")
    a = p.parse_args()

    missing = config.validate()
    if missing:
        print(f'{{"error": true, "message": "thiếu config: {", ".join(missing)}"}}',
              file=sys.stderr)
        return 1

    people = [(a.user_name, a.user_id)] if (a.user_id and a.user_name) else []
    content, metadata = send.with_mentions_prefix(a.text, people)
    res = send.send_text(a.group, content, a.group_type, metadata=metadata)
    client.print_json(res)
    return 0 if not (isinstance(res, dict) and res.get("error")) else 1


if __name__ == "__main__":
    sys.exit(main())
