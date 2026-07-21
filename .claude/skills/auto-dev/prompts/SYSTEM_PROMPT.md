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
| Kế hoạch (Data-Driven) | `<plan>` chứa `<overview>`, `<architecture>`, `<data_flow>` (nhiều `<step>`, mỗi step có `<input>`/`<transform>`/`<output>`), `<target_files>`, `<test_strategy>` |
| Danh sách file cần sửa | `<target_files><file>path/đến/file</file>...</target_files>` |
| Nhật ký / ngữ cảnh lỗi | `<error_context>...log thô...</error_context>` |
| Lời thoại tranh luận | `<debate><turn agent="...">...</turn>...</debate>` |
| Agent Debate (xem `tools/debate_engine.py`) | `<dev_proposal>` · `<architect_critique>` (kết bằng `<verdict>APPROVE\|REVISE</verdict>`) · `<dev_rebuttal>` · `<final_specification>` — lặp critique↔rebuttal đến khi APPROVE hoặc hết `--rounds` |
| Quyết định cuối của một vòng | `<decision>...</decision>` |

Ví dụ một plan hợp lệ (KHÔNG Markdown, KHÔNG code bên trong):

```
<plan>
  <overview>Xuất báo cáo doanh thu theo khoảng ngày ra CSV qua một API mới.</overview>
  <architecture>Controller nhận request; Service điều phối; Repository đọc bảng orders theo cursor. Không đổi Entity.</architecture>
  <data_flow>
    <step><input>GET /api/report/export?from&to</input><transform>validate và parse khoảng ngày</transform><output>DateRange</output></step>
    <step><input>DateRange</input><transform>query orders theo cursor paging</transform><output>luồng OrderRow</output></step>
    <step><input>luồng OrderRow</input><transform>ánh xạ sang dòng CSV, ghi theo batch</transform><output>phản hồi text/csv</output></step>
  </data_flow>
  <target_files>
    <file>src/main/java/com/x/ReportController.java</file>
    <file>src/main/java/com/x/ReportService.java</file>
  </target_files>
  <test_strategy>Unit: ánh xạ OrderRow sang CSV đúng cột. Integration: input ngày độc hại không gây SQLi.</test_strategy>
</plan>
```

## Kế hoạch phải DATA-DRIVEN & HIGH-LEVEL — TUYỆT ĐỐI KHÔNG code

Nỗi đau cần chặn: agent viết code quá sớm, hoặc plan chi tiết tới từng dòng gây ngợp và sai ý.
Vì thế mọi plan (`<dev_proposal>`, `<dev_rebuttal>`, `<final_specification>`, `<plan>`) BẮT BUỘC:

1. Dừng ở mức **tổng quan kiến trúc + biến đổi dữ liệu**. `<data_flow>` là hạt nhân: mỗi `<step>`
   mô tả **Input → Transform → Output** ở mức ý niệm (kiểu dữ liệu, thực thể, endpoint, hàng đợi)
   — KHÔNG phải các bước code.
2. **KHÔNG chứa code**: không thân hàm, không câu lệnh (`;`, `{`, `}`), không chữ ký hàm/annotation
   (`public`, `@Override`, `void`, `return ...`), không đoạn SQL nguyên văn. Nếu định viết code →
   hãy mô tả **dữ liệu biến đổi thế nào** thay vì viết ra.
3. `<target_files>` chỉ liệt kê **đường dẫn file** sẽ chạm, không kèm nội dung code.
4. Chỉ sau khi con người **duyệt plan ở checkpoint `after_plan`** thì agent Implement mới được viết
   code. Không code trước khi duyệt. Tool `debate_engine.py` tự soi và cảnh báo nếu plan lọt code
   (trường `code_flagged` trong JSON) → gate plan phải fail cho tới khi plan được viết lại data-flow.

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
