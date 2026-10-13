# Checklist triển khai — Agent-Skill-Toolkit

> Theo dõi tiến độ kế hoạch trong [`agent-ecosystem.md`](agent-ecosystem.md). Cập nhật **mỗi lần** xong một phần.
> Cập nhật lần cuối: 2026-10-13. Test: 410 xanh, 1 skip (Windows).

## Quy trình mỗi phần

```
code → test (Windows, cả suite — chỉ commit khi xanh) → commit → Codex review (bản chụp sạch, không .env)
     → sửa theo điểm đồng thuận → Codex xác nhận → ✅ → phần tiếp theo
```

Ký hiệu: ✅ xong + Codex đồng thuận · 🔁 đã code, đang review/sửa · 🟡 làm một phần · ⬜ chưa làm ·
⛔ bị chặn (chờ thông tin/thao tác của người dùng — ghi ở cột Ghi chú)

---

## 0. Nền an toàn (Phụ lục A)

| # | Việc | Trạng thái | Commit | Codex | Ghi chú |
|---|---|---|---|---|---|
| 0.1 | `orca_adapter` chỉ gọi `orca.exe` (chống chèn lệnh qua `.cmd`) | ✅ | `20cb944` | review gốc (vòng 1 nhánh) | đã xác nhận lỗ hổng bằng thử nghiệm |
| 0.2 | Duyệt review: board/gate không duyệt; duyệt gắn hash nội dung | ✅ | `20cb944` | — | giới hạn tạm: file cùng user OS → cần 6.x |
| 0.3 | Chống đăng trùng: lock, marker, không đăng lại inline | ✅ | `20cb944` | — | |
| 0.4 | Hook: PowerShell/Grep/agentkit/secret/ghi trạng thái | ✅ | `20cb944` | — | nhược điểm: chặn cả lệnh shell chỉ đọc `review_queue` |
| 0.5 | k8s ẩn mọi Secret; dbeaver chặn ghi PROD | ✅ | `20cb944` | — | lớp chắc nhất vẫn là DB user chỉ đọc |
| 0.6 | `cg.py` chạy được Windows (node + script, tắt telemetry) | ✅ | `20cb944` | — | |
| 0.7 | `agent_runner`: không chạy launcher `.cmd` qua `cmd.exe` (`core/winexec.py`) | ✅ | `317a381`, `4a847a0`, `199a6f5` | GO (vòng 3) | đã thử thật: payload tới PowerShell nguyên văn; codex chạy qua node; fallback `cmd /d /c` chỉ cho token thường |
| 0.8 | MED: clone race `review_ws`, lock bị phá giữa chừng | ⬜ | | | người dùng tạm hoãn |
| 0.9 | MED: `podfwd` (mồ côi, PID, `--insecure`), `vpn` argv, allowlist theo tên, `save_seed` mỗi lần | ⬜ | | | người dùng tạm hoãn |
| 0.10 | Thu hồi / cấp lại token trong `.env` | ⛔ | | | **người dùng tự làm** (đã hoãn) |

## Track R — tái cấu trúc repo (§B.4)

| # | Việc | Trạng thái | Commit | Codex | Ghi chú |
|---|---|---|---|---|---|
| R.M1 | Kiểm kê: đồ thị import + cạnh runtime, API công khai, baseline test | 🟡 | | | đã đo đồ thị import (1 vòng thật), baseline 332 test xanh; chưa có file kiểm kê |
| R.M2 | `_runtime` + bootstrap; kiểm cài plugin | 🟡 | `8520548` | GO (cùng F.4a) | bootstrap + package chạy từ mọi cwd/agentkit; **chưa thử cài marketplace / Orca / Codex** |
| R.M3 | Tách primitive chung vào `core` (env, UTF-8, lock), giữ wrapper | ⬜ | | | |
| R.M4 | Chuyển `mr-review` sang `workflows/mr_review` + `integrations/gitlab` | ⬜ | | | |
| R.M6 | Mở rộng dần + test layering (bánh cóc) | ⬜ | | | |

## Track F — tính năng (§14)

| # | Việc | Trạng thái | Commit | Codex | Ghi chú |
|---|---|---|---|---|---|
| F.4a | Store bền + engine checkpoint + quota pause/resume + handoff | ✅ | `8520548` → `980412a` → `4a847a0` → `199a6f5` | GO (vòng 3) | lease token, heartbeat, input key, kill cây tiến trình; mẫu limit vẫn tự viết — cần câu thông báo thật |
| F.4b | Gắn store/engine vào luồng review MR (`review_flow.py`) | ✅ | `65f923d` → `01fdbae` → `b336e7f` | GO (vòng 3) | quota → dừng; MR đổi head → review lại; chờ duyệt → blocked + reconcile; phát hiện giả mạo → quarantine (chỉ người gỡ). Cô lập OS thật = F.6b |
| F.4c | Chạy thử review 1 MR thật (shadow) | ⛔ | | | **cần project + số MR** |
| F.6a | Luật tự đăng comment GitLab (`review_rules.py`) | ✅ | `20cb944` | — | mặc định `shadow`; cần `REVIEW_AUTO_POST_PROJECTS` |
| F.6b | Executor tách biệt + duyệt Telegram gắn hash | ⛔ | | | **cần user Windows chuẩn + token GitLab chỉ đọc** |
| F.1 | Làm rõ → AC, gom câu hỏi 1 lần, triage 4 làn + tự nâng làn | ✅ | `12b4e42` → `bf35c5d` → `b1c43ab` | GO (vòng 3) | làn quyết định tier/mode/debate; đề xuất chỉ khi là câu hỏi; nội dung task được làm sạch + «trích» + vô hiệu markup |
| F.2 | Thang kiểm local + vòng tự sửa (`workflows/verify/ladder.py`, `auto-dev/tools/verify.py`) | ✅ | `20ececc` … `09f6b76` (8 commit) | GO (vòng 8) | worktree sạch + đúng nhánh trước mỗi bậc; pending fix bền; mọi bậc chạy lệnh khai `protects`; thay đổi test/cấu hình/spec/launcher → người duyệt; chạy thử trên toolkit: done. Repo Java chờ người dùng chọn |
| F.3 | AC → test thật trong repo + bảng AC → bằng chứng | ✅ | `d516752` → `28cae3d` → `e5b5bbd` → `fa4ceb5` | GO (vòng 4) | AC chỉ tính khi testcase JUnit của LẦN CHẠY NÀY pass, từ file test đổi trên nhánh (khớp đủ package, không mơ hồ); thiếu test cho AC → người xử lý (không tự sửa); báo cáo chỉ tin kết quả cùng spec + cùng lần chạy suite; `verify.py start --ac/--ac-file`, `report` |
| F.5 | Cổng vào: `mr_watch` / `atask_watch` / `group_watch` chỉ xếp hàng vào store | ✅ | `32257f7` → `fd921b3` → `0905ba4` | GO (vòng 3) | `intake/gate.py`: nguồn → độ tin cậy + loại việc, công tắc `work/policy.json` (sai định dạng = tắt), allowlist theo user id, link MR đúng host+port, không userinfo; `review_queue add/add-text` cũng qua cổng; người trong group chỉ được agent CHỈ ĐỌC, build chỉ chủ tài khoản; `mr_watch` mặc định xếp vào store; `atask_watch --mode store` (mặc định direct tới F.7). **Cần bạn đổi `FCHAT_REVIEW_ALLOWED` sang user id** |
| F.7 | Điều phối nhiều việc + bảng tiến độ (Orca + aTask/`status.py`) | ✅ | `3c87212` → `80b230b` → `ad62576` → `7c661e7` | GO (vòng 4) | `coordinator.py work --parallel N` chạy mọi loại việc (review_mr, verify, atask_intake); giới hạn song song lưu trong DB (`capacity`, mặc định 2) áp cho mọi vòng work; phụ thuộc + lỗi lan theo chuỗi; khoá repo; lease hết hạn không hồi sinh; GitLab project theo luồng; `board` + `status.py` mục tasks; đề xuất aTask → `accept` (atomic). Chưa đồng bộ aTask "Agent Hub" (cần bạn quyết) |
| F.8 | Thang kiểm mở rộng: deploy dev, health pod, smoke UI, e2e, bench | ⛔ | | | **cần `environments.dev` trong `projects.json`** |
| F.9 | Làn Đề xuất (ADR + debate) + quét chủ động hằng tuần | ⬜ | | | |
| F.10 | Làn Lớn (refactor: khảo sát, lưới an toàn, bánh cóc, lát cắt) | ⬜ | | | |
| F.11 | Điều tiết theo mức dùng (80% / 95%), model theo bước, ngân sách cứng | ✅ | `b26077a` → `0a23507` → `a989e1d` → `b8159f7` → `d59e5fe` | GO (vòng 5) | mức dùng do bạn/script khai (`coordinator.py usage PCT [--resets-at]`, hết hạn 6 h — toolkit không tự đọc được % subscription); ≥80%: dừng làn Đề xuất + quét tuần, opus→sonnet, vai nhẹ→haiku; ≥95%: chỉ review_mr + làn Nhỏ; 100%: không nhận việc, từ chối lượt agent tới reset. Ngân sách giữ chỗ **theo từng lượt agent** (atomic, tính trường hợp xấu nhất): task (lượt/giây) → chặn; trần lượt/ngày → dừng tới 0h; lượt API chạy trong tiến trình con bị giết đúng hạn |
| F.12 | `code_context.py` (CodeGraph mặc định, grep dự phòng) cho grounding / fix_loop | 🟡 | `166bcf7` | | đã có `cg.py` cho review MR |

## Thông tin đang chờ người dùng

| Cần | Dùng cho |
|---|---|
| Project + số MR để chạy thử | F.4c |
| Repo thử cho thang kiểm (repo Java, vd etask) | F.2 |
| Repo được tự đăng (`REVIEW_AUTO_POST_PROJECTS`), giữ `shadow` bao lâu | F.6a |
| Allowlist TChat (user id) | F.5 |
| Agent mặc định (đề xuất: code = Claude, review chéo = Codex) | F.1, F.2 |
| Câu thông báo limit thật của Claude CLI / Codex | F.4a |
| `environments.dev` (job Jenkins, context/namespace k8s, URL dev) | F.8 |
| User Windows chuẩn + token GitLab chỉ đọc | F.6b |

## Nhật ký review Codex

| Ngày | Phần | Vòng | Kết luận |
|---|---|---|---|
| 2026-10-11 | Kế hoạch hệ sinh thái | 2 | đồng thuận (Phụ lục B) |
| 2026-10-12 | Kiến trúc repo | 2 | đồng thuận (Phụ lục C) |
| 2026-10-13 | F.4a store/engine/quota (`8520548`) + 0.7 winexec (`317a381`) | 1 | **NO-GO**: 1 HIGH (thiếu claim token) + 6 MED (heartbeat, quota runner→engine, LeaseLost lọt, checkpoint không gắn SHA, quota bắt nhầm, reset bị cắt 6 h) — đồng ý cả 7, sửa ở `980412a` |
| 2026-10-13 | F.4a + 0.7 (`980412a`) | 2 | **NO-GO**: xác nhận sửa 4/7; HIGH mới (claim đọc sau commit nhận nhầm token), MED key_fn, migration race, winexec quoting, quota còn bắt nhầm — sửa ở `4a847a0` |
| 2026-10-13 | F.4b (`65f923d`) + xác nhận `199a6f5` | 1 | **NO-GO**: 2 HIGH (agent thừa hưởng token/quyền ghi; reaper Orca lấy lại job đang pause) + 3 MED (reconcile thiếu ca stale/head mới, Telegram gửi trùng khi crash, timeout trong không kill cây). `199a6f5` xác nhận FIXED cả 4 |
| 2026-10-13 | F.4b (`01fdbae`) | 2 | **NO-GO**: chấp nhận hoãn cô lập OS sang F.6b; HIGH mới (lời duyệt giả có hash khớp được reconcile tự mở chặn), MED (khoá gửi duyệt chỉ ở flow) — sửa ở `b336e7f` |
| 2026-10-13 | F.2 (`20ececc`) | 1 | **NO-GO**: 4 HIGH (nhánh chỉ kiểm lúc enqueue; `git add -A` gom thay đổi của người; nới test được; lỗi git = diff rỗng) + 4 MED — sửa ở `64613bd` |
| 2026-10-13 | F.2 (`64613bd`) | 2 | **NO-GO**: FIXED 4/8; HIGH còn (commit nhánh khác, danh sách bảo vệ thiếu, pending fix chỉ trong bộ nhớ) + MED — sửa ở `bcf4bef` |
| 2026-10-13 | F.2 (`bcf4bef`) | 3 | **NO-GO**: chấp nhận cách phát hiện commit nhánh khác; HIGH: script do interpreter chạy chưa bảo vệ, protection fail-open khi git lỗi; MED covered_by |
| 2026-10-13 | F.2 (`6aaeb21`, `bbf35af`, `bf842ad`, `1f69504`) | 4–7 | **NO-GO** từng vòng vì đoán script từ argv luôn lọt → đổi thiết kế: mọi bậc chạy lệnh phải khai `protects`, launcher luôn được bảo vệ, chuẩn hoá path, không phân biệt hoa thường, từ chối path ngoài repo |
| 2026-10-13 | F.2 (`09f6b76`) | 8 | **GO** |
| 2026-10-13 | F.1 (`12b4e42`) | 1 | **NO-GO**: HIGH làn lớn vẫn giữ mode auto/skip_debate; MED đề xuất bắt nhầm câu mệnh lệnh, thiếu làm rõ lại, giả mạo mục trong tin nhắn, giả định che đề xuất |
| 2026-10-13 | F.1 (`bf35c5d`) | 2 | **NO-GO**: FIXED 3/5; còn thiếu epic/defect, markup trong «…» vẫn hoạt động |
| 2026-10-13 | F.1 (`b1c43ab`) | 3 | **GO** |
| 2026-10-13 | F.4b (`b336e7f`) | 3 | **GO**: quarantine + khoá gửi duyệt chung đều FIXED; không có lỗi mới. Gợi ý thêm test tranh chấp đồng thời flow/direct |
| 2026-10-13 | F.4a + 0.7 (`4a847a0`) | 3 | **GO**: chấp nhận 2 phản biện (finish khi lease hết hạn an toàn nhờ token; dừng hợp tác cho Python thuần). MED còn: kill cây tiến trình, quota JSON nhúng, tham số rỗng — sửa ở `199a6f5` (sẽ được xác nhận trong vòng review F.4b) |
| 2026-10-13 | F.3 (`d516752`) | 1 | **NO-GO**: marker trong file không phải test/comment được tính; test bị skip / suite không liên quan vẫn ✅; báo cáo diễn giải checkpoint cũ theo spec mới; start trùng làm mất AC; AC1000 bị đánh số lại — sửa ở `28cae3d` |
| 2026-10-13 | F.3 (`28cae3d`) | 2 | **NO-GO**: FIXED 3/5; report JUnit cũ còn được tính, checkpoint coverage/suite lệch commit; HIGH mới: Java khớp theo tên class đơn — sửa ở `e5b5bbd` |
| 2026-10-13 | F.3 (`e5b5bbd`) | 3 | **NO-GO**: HIGH tiền tố package (`com/original.java`) giả làm file test — sửa ở `fa4ceb5` |
| 2026-10-13 | F.3 (`fa4ceb5`) | 4 | **GO** |
| 2026-10-13 | F.5 (`32257f7`) | 1 | **NO-GO**: 3 HIGH (TChat build/freeform lách cổng; `review_queue add/add-text` lách cổng; userinfo lách kiểm host) + 2 MED (mr_watch mất MR khi tiến trình chết; policy sai kiểu = bật) — sửa ở `fd921b3` |
| 2026-10-13 | F.5 (`fd921b3`) | 2 | **NO-GO**: FIXED 4/5; câu hỏi tự do từ group vẫn chạy agent đủ quyền — sửa ở `0905ba4` (agent chỉ đọc) |
| 2026-10-13 | F.5 (`0905ba4`) | 3 | **GO** |
| 2026-10-13 | F.7 (`3c87212`) | 1 | **NO-GO**: 3 HIGH (review song song đổi `GITLAB_PROJECT_ID` toàn tiến trình; lease hết hạn hồi sinh được → 2 holder; task verify cũ không có khoá repo) + 4 MED (giới hạn song song theo từng caller, accept nhận nhầm task bị chặn khác, lỗi phụ thuộc không lan qua chuỗi, board mất việc mở + cháu) — sửa ở `80b230b` |
| 2026-10-13 | F.7 (`80b230b`) | 2 | **NO-GO**: FIXED 4/7; còn backfill khoá repo, chính sách capacity chung, accept chưa atomic — sửa ở `ad62576` |
| 2026-10-13 | F.7 (`ad62576`) | 3 | **NO-GO**: accept vẫn dùng checkpoint đã bị vô hiệu — sửa ở `7c661e7` |
| 2026-10-13 | F.7 (`7c661e7`) | 4 | **GO** |
| 2026-10-13 | F.11 (`b26077a`) | 1 | **NO-GO**: 2 HIGH (ngân sách kiểm theo bước, không giữ chỗ → vượt trần; nhiều lối gọi agent không bị tính) + 5 MED (lượt bị ngắt không tính, chặn cả bước không dùng agent, 100% không dừng, policy hỏng = mở, đổi model sót lối) + LOW DST — thiết kế lại ở `0a23507` |
| 2026-10-13 | F.11 (`0a23507`) | 2 | **NO-GO**: FIXED 6/8; còn làm tròn timeout lên 1 s, API không có hạn chót tổng, mất kiểu QuotaExceeded qua role, NaN — sửa ở `a989e1d` |
| 2026-10-14 | F.11 (`a989e1d`) | 3 | **NO-GO**: API chỉ thôi chờ chứ không huỷ; `proc.run` chạy lố 1 nhịp poll — sửa ở `b8159f7` |
| 2026-10-14 | F.11 (`b8159f7`) | 4 | **NO-GO**: watchdog không huỷ được DNS/kết nối → POST sau hạn — sửa ở `d59e5fe` (tiến trình con) |
| 2026-10-14 | F.11 (`d59e5fe`) | 5 | **GO** (khe hở kiểm-rồi-gửi chấp nhận được cho kiểm soát ngân sách) |
