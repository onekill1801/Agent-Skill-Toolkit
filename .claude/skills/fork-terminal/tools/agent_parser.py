#!/usr/bin/env python3
"""Agent I/O parser — bóc tách dữ liệu có cấu trúc từ output của Agent.

Giao tiếp NỘI BỘ giữa các Agent (AI↔AI) và giữa Agent↔Python tools trong pipeline
`/auto-dev` + `fork-terminal` dùng định dạng **thẻ HTML/XML nghiêm ngặt** thay cho Markdown
lỏng lẻo (xem `.claude/skills/auto-dev/prompts/SYSTEM_PROMPT.md`). Module này là parser
chuẩn cho định dạng đó.

Vì sao dùng Regex thay vì BeautifulSoup/lxml: toàn bộ toolkit **chỉ dùng stdlib**, không
`pip install`. Dữ liệu Agent sinh ra là thẻ phẳng, có kiểm soát (do chính prompt quy định),
nên một parser regex chịu được lồng nông là đủ — không cần một HTML engine đầy đủ.

API:
    extract_tag_content(text, tag_name)            -> str | None
    extract_all_tag_contents(text, tag_name)       -> list[str]
    extract_list_items(text, parent_tag, child_tag) -> list[str]
    wrap(tag_name, content)                        -> str   (tiện ích cho phía tool)

CLI (để orchestrator gọi mà không cần import):
    python agent_parser.py tag   <tag_name>            < input.txt
    python agent_parser.py all   <tag_name>            < input.txt
    python agent_parser.py list  <parent_tag> <child_tag> < input.txt
    python agent_parser.py wrap  <tag_name> "<content>"

Đọc văn bản từ stdin (hoặc --file <path>); in JSON ra stdout.
Zero external dependencies — Python stdlib only.
"""

import argparse
import json
import re
import sys

# Cross-platform: ép UTF-8 stdout/stderr để không vỡ trên console Windows cp1252
# khi nội dung thẻ chứa tiếng Việt. No-op trên macOS/Linux.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def _tag_pattern(tag_name: str) -> re.Pattern:
    """Regex bắt một thẻ <tag>...</tag>.

    - `re.IGNORECASE`: chấp nhận <Plan> hoặc <plan> (Agent đôi khi đổi hoa/thường).
    - `re.DOTALL`: nội dung được phép chứa newline (plan/log nhiều dòng).
    - Cho phép attribute (vd <file path="x">) — `[^>]*` nuốt phần attribute.
    - `.*?` non-greedy → dừng ở thẻ đóng GẦN NHẤT, tránh nuốt nhiều thẻ cùng tên.
    """
    name = re.escape(tag_name)
    return re.compile(
        rf"<{name}(?:\s[^>]*)?>(.*?)</{name}>",
        re.IGNORECASE | re.DOTALL,
    )


def extract_tag_content(text: str, tag_name: str) -> str | None:
    """Trả về nội dung (đã strip) bên trong thẻ <tag_name> ĐẦU TIÊN, hoặc None nếu không có.

    >>> extract_tag_content("<plan>do X then Y</plan>", "plan")
    'do X then Y'
    """
    if not text:
        return None
    m = _tag_pattern(tag_name).search(text)
    return m.group(1).strip() if m else None


def extract_all_tag_contents(text: str, tag_name: str) -> list[str]:
    """Trả về nội dung (đã strip) của MỌI thẻ <tag_name> theo thứ tự xuất hiện."""
    if not text:
        return []
    return [m.strip() for m in _tag_pattern(tag_name).findall(text)]


def extract_list_items(text: str, parent_tag: str, child_tag: str) -> list[str]:
    """Lấy danh sách item trong cấu trúc thẻ cha/con.

    Chỉ tìm các <child_tag> NẰM TRONG <parent_tag> (không nhặt nhầm thẻ con cùng tên
    ở phân đoạn khác của output). Nếu thiếu thẻ cha → trả [].

    >>> txt = "<target_files><file>a.py</file><file>b.py</file></target_files>"
    >>> extract_list_items(txt, "target_files", "file")
    ['a.py', 'b.py']
    """
    parent = extract_tag_content(text, parent_tag)
    if parent is None:
        return []
    return extract_all_tag_contents(parent, child_tag)


def wrap(tag_name: str, content: str) -> str:
    """Bọc `content` vào thẻ <tag_name>...</tag_name>.

    Tiện ích cho phía Python tool khi cần đóng gói dữ liệu (vd log lỗi) thành thẻ
    trước khi ném ngược cho Agent. Xem `test_runner.py` dùng cho <error_context>.
    """
    return f"<{tag_name}>{content}</{tag_name}>"


# --------------------------------------------------------------------------- CLI


def _read_input(args) -> str:
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            return f.read()
    return sys.stdin.read()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Parse HTML/XML-tagged agent output (stdlib regex, no deps)."
    )
    # `--file` áp dụng cho mọi lệnh đọc input. Đặt qua parent parser để nó nhận diện được
    # CẢ trước lẫn sau tên lệnh con (vd `... list target_files file --file plan.xml`).
    io_parent = argparse.ArgumentParser(add_help=False)
    io_parent.add_argument("--file", help="Đọc văn bản từ file thay vì stdin")
    sub = parser.add_subparsers(dest="action", required=True)

    p = sub.add_parser("tag", parents=[io_parent], help="Nội dung thẻ đầu tiên")
    p.add_argument("tag_name")

    p = sub.add_parser("all", parents=[io_parent], help="Nội dung mọi thẻ cùng tên")
    p.add_argument("tag_name")

    p = sub.add_parser("list", parents=[io_parent], help="Danh sách item trong thẻ cha/con")
    p.add_argument("parent_tag")
    p.add_argument("child_tag")

    p = sub.add_parser("wrap", help="Bọc nội dung vào một thẻ")
    p.add_argument("tag_name")
    p.add_argument("content")

    args = parser.parse_args()

    if args.action == "wrap":
        out = {"result": wrap(args.tag_name, args.content)}
    else:
        text = _read_input(args)
        if args.action == "tag":
            out = {"result": extract_tag_content(text, args.tag_name)}
        elif args.action == "all":
            out = {"result": extract_all_tag_contents(text, args.tag_name)}
        else:  # list
            out = {"result": extract_list_items(text, args.parent_tag, args.child_tag)}

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
