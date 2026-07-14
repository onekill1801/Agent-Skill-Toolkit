# Auto-Dev — Giao thức giao tiếp giữa các Agent (Agent Communication Protocol)

> Prompt cấu hình hệ thống cho pipeline `/auto-dev` và các agent con sinh ra qua
> `fork-terminal`. Nạp prompt này khi điều phối nhiều agent (Plan agent, Implement agent,
> Fix agent, Review agent) hoặc khi một agent đọc/ghi dữ liệu trung gian cho agent khác.

## Quy tắc BẮT BUỘC: dữ liệu trung gian dùng thẻ HTML/XML

**KỂ TỪ BÂY GIỜ, tất cả các dữ liệu trung gian — Kế hoạch (Plan), Danh sách file cần sửa
(Target Files), Nhật ký sửa lỗi (Error Log) và Lời thoại tranh luận giữa các Agent — BẮT BUỘC
phải được bao bọc trong các thẻ đóng/mở dạng HTML/XML.** Ví dụ: `<plan>...</plan>`,
`<target_files><file>...</file></target_files>`. **KHÔNG ĐƯỢC sử dụng các ký tự Markdown như
`#`, `**`, `-` trong các phân đoạn dữ liệu này.**

Lý do: dữ liệu trao đổi máy-đọc-máy phải parse được tất định bằng regex (qua
`fork-terminal/tools/agent_parser.py`) thay vì cắt chuỗi Markdown lỏng lẻo dễ vỡ.

## Bộ thẻ chuẩn (canonical tag set)

Mọi agent trong pipeline PHẢI dùng đúng tên thẻ dưới đây để tool và agent khác bóc tách được:

| Phân đoạn | Cấu trúc thẻ |
|---|---|
| Kế hoạch | `<plan>` (chứa `<approach>`, `<test_strategy>` tùy chọn) |
| Danh sách file cần sửa | `<target_files><file>path/đến/file</file>...</target_files>` |
| Nhật ký / ngữ cảnh lỗi | `<error_context>...log thô...</error_context>` |
| Lời thoại tranh luận | `<debate><turn agent="...">...</turn>...</debate>` |
| Agent Debate (xem `tools/debate_engine.py`) | `<dev_proposal>` · `<architect_critique>` (kết bằng `<verdict>APPROVE\|REVISE</verdict>`) · `<dev_rebuttal>` · `<final_specification>` — lặp critique↔rebuttal đến khi APPROVE hoặc hết `--rounds` |
| Quyết định cuối của một vòng | `<decision>...</decision>` |

Ví dụ một plan hợp lệ (KHÔNG Markdown bên trong):

```
<plan>
  <approach>Sửa null-check trong UserService.load() trước khi map DTO.</approach>
  <target_files>
    <file>src/main/java/com/x/UserService.java</file>
    <file>src/test/java/com/x/UserServiceTest.java</file>
  </target_files>
  <test_strategy>Thêm test load() với id không tồn tại, fail trước fix, pass sau fix.</test_strategy>
</plan>
```

## Bóc tách: dùng agent_parser.py, KHÔNG split newline

Khi một agent (hoặc orchestrator) cần đọc dữ liệu trung gian do agent khác sinh ra, dùng
`fork-terminal/tools/agent_parser.py` — KHÔNG tự cắt chuỗi theo dòng:

```bash
# Lấy danh sách file cần sửa từ plan của Plan-agent:
python ../../fork-terminal/tools/agent_parser.py list target_files file --file plan.txt
# Lấy nguyên khối <plan>:
python ../../fork-terminal/tools/agent_parser.py tag plan --file plan.txt
```

Hoặc import trực tiếp: `from agent_parser import extract_tag_content, extract_list_items`.

## Phía Python tool: bọc lỗi trước khi trả cho Agent

Khi một tool (vd `test_runner.py`) gặp lỗi runtime và cần ném log ngược cho Agent phụ sửa
code, log đó PHẢI được bọc trong `<error_context>...</error_context>`. Tool đã tự làm việc
này (trường `error_context` trong JSON trả về) — Agent phụ đọc thẳng thẻ đó làm ngữ cảnh sửa.

## NGOẠI LỆ: đầu ra cho con người vẫn là Markdown

Quy tắc thẻ HTML/XML **chỉ áp dụng cho giao tiếp ngầm Agent↔Agent và Agent↔Tool**. Khi trình
bày kết quả cuối cùng cho **người dùng trên terminal** (tóm tắt plan để xin duyệt, báo cáo
test, mô tả MR, thông báo hoàn thành), agent PHẢI chuyển dữ liệu sang **Markdown sạch, dễ đọc**
— bỏ thẻ, dùng heading/bullet/bảng bình thường. Người dùng không bao giờ nên nhìn thấy thẻ thô
`<plan>` trong câu trả lời cuối.
