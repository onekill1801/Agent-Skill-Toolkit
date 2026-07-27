# Changelog

Mọi thay đổi đáng chú ý của toolkit này được ghi lại ở đây.

Định dạng theo [Keep a Changelog](https://keepachangelog.com/vi/1.1.0/),
đánh version theo [Semantic Versioning](https://semver.org/lang/vi/).

## Cách ghi (đọc trước khi thêm mục)

- Mỗi bản phát hành một mục `## [x.y.z] - YYYY-MM-DD`. Thay đổi chưa phát hành gom ở `## [Unreleased]`.
- Nhóm theo loại: **Added** (thêm mới) · **Changed** (đổi hành vi) · **Deprecated** (sắp bỏ) ·
  **Removed** (đã bỏ) · **Fixed** (sửa lỗi) · **Security** (bảo mật).
- Viết theo góc nhìn người dùng ("làm được gì mới"), không phải chi tiết code. Một dòng một thay đổi.
- Tăng version: **MAJOR** khi phá vỡ tương thích (đổi lệnh/env đang dùng) · **MINOR** khi thêm skill/tool/lệnh ·
  **PATCH** khi chỉ sửa lỗi. Khi phát hành: đổi `[Unreleased]` thành version + ngày, tạo `[Unreleased]` rỗng mới.

## [Unreleased]

### Added
-

### Changed
-

### Fixed
-

## [0.1.0] - 2026-07-27

Bản gộp đầu tiên — chốt lại toàn bộ tính năng đã tích luỹ trước khi bắt đầu đánh version.

### Added

- **dev-automation** — luồng Azure DevOps + GitLab: đọc task → tạo branch → code → tạo MR → review → báo tester.
  - `azure_devops.py`: đọc/liệt kê work item, đổi state, comment, gán việc theo email/tên hiển thị.
  - `gitlab_api.py`: liệt kê/đọc/diff/discussion MR, tạo branch & MR, comment, merge, `my-review-mrs`.
  - `mr_watch.py`: poll MR gắn tên tôi rồi tự mở Claude review (hỏi trước khi post).
  - Auto MR review + các lệnh GitLab mở rộng.
- **etask-automation** — quản lý task/sprint/checklist/analytics trên eTask qua REST AI-agent (auth PAT).
  - `tasks.py`, `projects.py`, `search.py`, `analytics.py`, `checklists.py`.
  - `get-detail` gọi thẳng `/api/tasks/{id}` (đủ field hơn kênh `ai/execute`).
  - Dashboard tổng hợp (personal/org/user/project) + `governed_search` (DSL whitelist an toàn).
  - Output tinh gọn, client hỗ trợ phân trang & lọc theo status-type; luồng tạo/duyệt/feedback task.
- **auto-dev** — pipeline đầu-cuối Plan → Implement → Test → Deliver với checkpoint người thật; chốt MR trên bài test xanh.
  - Vòng lặp plan tranh luận (agent debate) + làm rõ yêu cầu ở khâu intake; chế độ tự chủ lai (hybrid autonomy).
- **remote-control** — điều khiển agent qua Telegram (headless `claude -p`, duyệt bằng nút bấm) + fan-out lệnh ra máy LAN qua SSH.
  - Vòng đời session theo từng chat + dự phòng nhiều tài khoản; luồng duyệt & tự-duyệt có cấu hình.
- **tchat-automation** — đọc TChat (messenger nội bộ) qua REST: hội thoại, lịch sử tin nhắn, media, danh bạ, todo.
- **team-registry** — kho hồ sơ đội nhóm (vai trò/kỹ năng/tính cách) ở `work/team.json` để giao việc & match người.
- **fork-terminal** — spawn agent khác (Claude/Codex/Gemini) ra cửa sổ terminal mới; hỗ trợ Windows/macOS/Linux.
- **skill-scaffold** — meta-skill: trích tool từ app ngoài → sinh skill mới (SKILL.md, tools/, cookbook/, prompts/, slash command).
- **Workspace đa project** — registry `work/projects.json`, clone theo project, switcher `work/proj.sh`; chuyển project bằng env inline.
- **Tool kiểm chứng stack (thuần stdlib)** — `probe_db` (MySQL + Postgres qua socket, không cần CLI, có `--max-rows`),
  `probe_api/redis/kafka`, `jenkins`, `kafka_ui`, `flow_check`, `local_app`, `run_log`, `postman_gen`.
- **status.py / daemon_common.py** — bảng trạng thái tổng hợp: daemon, pipeline run đang mở, queue, approval chờ, feedback.
- **feedback.py** — sổ học từ can thiệp người (recall bơm bài học cũ vào prompt run sau).
- **doctor.py** — báo mức sẵn sàng của máy (OS, Python, CLI có/thiếu, feature nào chạy được).

### Changed

- Hỗ trợ đa nền tảng: mọi tool tự ép stdout UTF-8 (không crash console Windows cp1252); đường dẫn dùng `os.path`.
- `.env.sample` + README + CLAUDE.md mở rộng cho workspace đa project, tuỳ chọn `SSL_VERIFY`, và cấu hình các tích hợp mới.

### Security

- Scrub định danh nội bộ trước khi công khai; không hardcode token/URL — luôn qua `config.py`.

[Unreleased]: https://github.com/onekill1801/Agent-Skill-Toolkit/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/onekill1801/Agent-Skill-Toolkit/releases/tag/v0.1.0
