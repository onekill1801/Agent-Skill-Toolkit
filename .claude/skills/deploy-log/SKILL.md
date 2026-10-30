---
name: deploy-log
description: >-
  Log a production deployment: create the matching aTask record (status ready_to_deploy) in the
  "Change request" list AND write the row into the "Kyta Deploy" Google Sheet (current month's tab),
  in one step. The two share the name "[<env> - <service> - <desc>]"; the sheet's task link (col P)
  is the created task's KCR link. Trigger phrases: "log deploy", "ghi log deploy", "deploy prod xong",
  "ghi sổ deploy", "tạo record deploy", "deploy-log", "log prod deployment", "đẩy prod xong ghi sổ".
---

# deploy-log

Tự động hoá việc ghi sổ mỗi lần deploy prod: thay vì mở Google Sheet điền tay + tạo task aTask riêng,
chạy **một lệnh** là xong cả hai, khớp nhau.

## Khi nào dùng
Người dùng vừa (hoặc sắp) đẩy một service lên **prod** và muốn ghi lại: 1 dòng trong sheet "Kyta Deploy"
+ 1 task trong list "Change request" với trạng thái `ready_to_deploy`.

## Thông tin cần
Chỉ **service**. Mô tả tự lấy từ **commit GitLab mới nhất** của repo đó (bỏ `--desc`). Còn lại tự suy:
- Description (cột F) = title commit mới nhất ở **nhánh prod trong registry** (fallback default branch).
- Tên task = cột Q (Message Group) = `[<env> - <service> - <desc>]` (env mặc định `Prod`).
- Link task (cột P) = `https://etask.sandbox.kytaplatform.com/task/<KCR>` (lấy sau khi tạo task).
- Ngày (cột N) = hôm nay, `DD/MM/YYYY`. Người confirm (cột H) = `chungtv8` (đổi bằng `--confirm`).
- Tab = tháng hiện tại (`Tháng MM-YYYY`), ghi vào dòng trống kế tiếp (sau dòng có Service cuối cùng).

Map `service` → repo GitLab: alias file → registry (`repo_path`). Service lạ (không map được) → tool
dừng, liệt kê ứng viên; chạy lại với `--gitlab-id <id> [--branch <b>] --save-alias` để **nhớ 1 lần**
(ghi `work/deploy-log-aliases.json`). Luôn có thể ghi đè bằng `--desc` tay.

## Cách chạy
```
# Xem trước (KHÔNG ghi gì) — tự lấy desc từ commit — luôn chạy trước:
python agentkit.py deploy_log --service workflow

# Ghi thật (tạo task + điền sheet):  [WRITE]
python agentkit.py deploy_log --service workflow --commit

# Service lạ: chỉ repo 1 lần rồi nhớ
python agentkit.py deploy_log --service eDlm-ext --gitlab-id 1234 --branch prod --save-alias
```
Cờ: `--desc "..."` (ghi đè) · `--env Prod|Demo` · `--confirm <user>` · `--date DD/MM/YYYY` ·
`--gitlab-id <id>` · `--branch <b>` · `--save-alias` · `--json`.

## Guardrail
- **Mặc định dry-run.** `--commit` là **[WRITE] + hướng ra ngoài** (tạo task người thật thấy + ghi sheet
  dùng chung của team). Hỏi xác nhận người dùng trước khi `--commit` lần đầu trong một mạch tự động.
- Luôn chạy dry-run và cho người dùng xem plan trước khi commit.

## Hạ tầng / phụ thuộc
- Google Sheets qua **service account** (ký JWT bằng openssl): `GOOGLE_SA_KEY_FILE` trong `.env`,
  key ở `work/secrets/`, sheet đã share cho email robot. Code: `integrations/google_sa.py` + `gsheets.py`.
- aTask qua client của skill `atask-automation` (`execute_tool` + `api_get_user`).
- Chi tiết + status-id của list: `work/memory/deploy-log-google-sheets.md`.

## Trạng thái
- `desc` tự lấy từ commit GitLab mới nhất (XONG — người dùng chỉ nhập `service`).
- Còn lại (tùy chọn sau): tự phát hiện service từ deploy Jenkins/GitLab để khỏi nhập cả `service`.
