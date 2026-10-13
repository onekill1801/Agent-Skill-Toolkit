# Checklist triển khai — Agent-Skill-Toolkit

> Theo dõi tiến độ kế hoạch trong [`agent-ecosystem.md`](agent-ecosystem.md). Cập nhật **mỗi lần** xong một phần.
> Cập nhật lần cuối: 2026-10-13. Test: 351 xanh, 1 skip (Windows).

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
| F.4b | Gắn store/engine vào luồng review MR (M5) | ⬜ | | | |
| F.4c | Chạy thử review 1 MR thật (shadow) | ⛔ | | | **cần project + số MR** |
| F.6a | Luật tự đăng comment GitLab (`review_rules.py`) | ✅ | `20cb944` | — | mặc định `shadow`; cần `REVIEW_AUTO_POST_PROJECTS` |
| F.6b | Executor tách biệt + duyệt Telegram gắn hash | ⛔ | | | **cần user Windows chuẩn + token GitLab chỉ đọc** |
| F.1 | Làm rõ → AC, gom câu hỏi 1 lần, triage 4 làn + tự nâng làn | ⬜ | | | có sẵn `clarify`/`triage` |
| F.2 | Thang kiểm local (build → test liên quan → suite → Codex tới GO) | ⬜ | | | **cần repo thử** |
| F.3 | AC → test thật trong repo + bảng AC → bằng chứng | ⬜ | | | |
| F.5 | Cổng vào: `mr_watch` / `atask_watch` / `group_watch` chỉ xếp hàng vào store | ⬜ | | | cần allowlist TChat (user id) |
| F.7 | Điều phối nhiều việc + bảng tiến độ (Orca + aTask/`status.py`) | ⬜ | | | quyết định ghi tự động project "Agent Hub" |
| F.8 | Thang kiểm mở rộng: deploy dev, health pod, smoke UI, e2e, bench | ⛔ | | | **cần `environments.dev` trong `projects.json`** |
| F.9 | Làn Đề xuất (ADR + debate) + quét chủ động hằng tuần | ⬜ | | | |
| F.10 | Làn Lớn (refactor: khảo sát, lưới an toàn, bánh cóc, lát cắt) | ⬜ | | | |
| F.11 | Điều tiết theo mức dùng (80% / 95%), model theo bước, ngân sách cứng | ⬜ | | | |
| F.12 | `code_context.py` (CodeGraph mặc định, grep dự phòng) cho grounding / fix_loop | 🟡 | `166bcf7` | | đã có `cg.py` cho review MR |

## Thông tin đang chờ người dùng

| Cần | Dùng cho |
|---|---|
| Project + số MR để chạy thử | F.4c |
| Repo thử cho thang kiểm | F.2 |
| Repo được tự đăng (`REVIEW_AUTO_POST_PROJECTS`), giữ `shadow` bao lâu | F.6a |
| Allowlist TChat (user id) | F.5 |
| Agent mặc định (đề xuất: code = Claude, review chéo = Codex) | F.1, F.2 |
| Câu thông báo limit thật của Claude CLI / Codex | F.4a | Store bền + engine checkpoint + quota pause/resume + handoff | ✅ | `8520548` → `980412a` → `4a847a0` → `199a6f5` | GO (vòng 3) | lease token, heartbeat, input key, kill cây tiến trình; mẫu limit vẫn tự viết — cần câu thông báo thật |
| `environments.dev` (job Jenkins, context/namespace k8s, URL dev) | F.8 |
| User Windows chuẩn + token GitLab chỉ đọc | F.6b |

## Nhật ký review Codex

| Ngày | Phần | Vòng | Kết luận |
|---|---|---|---|
| 2026-10-11 | Kế hoạch hệ sinh thái | 2 | đồng thuận (Phụ lục B) |
| 2026-10-12 | Kiến trúc repo | 2 | đồng thuận (Phụ lục C) |
| 2026-10-13 | F.4a store/engine/quota (`8520548`) + 0.7 winexec (`317a381`) | 1 | **NO-GO**: 1 HIGH (thiếu claim token) + 6 MED (heartbeat, quota runner→engine, LeaseLost lọt, checkpoint không gắn SHA, quota bắt nhầm, reset bị cắt 6 h) — đồng ý cả 7, sửa ở `980412a` |
| 2026-10-13 | F.4a + 0.7 (`980412a`) | 2 | **NO-GO**: xác nhận sửa 4/7; HIGH mới (claim đọc sau commit nhận nhầm token), MED key_fn, migration race, winexec quoting, quota còn bắt nhầm — sửa ở `4a847a0` |
| 2026-10-13 | F.4a + 0.7 (`4a847a0`) | 3 | **GO**: chấp nhận 2 phản biện (finish khi lease hết hạn an toàn nhờ token; dừng hợp tác cho Python thuần). MED còn: kill cây tiến trình, quota JSON nhúng, tham số rỗng — sửa ở `199a6f5` (sẽ được xác nhận trong vòng review F.4b) |
