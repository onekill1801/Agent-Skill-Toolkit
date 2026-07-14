---
name: Team Registry
description: >
  Kho hồ sơ đội nhóm: vai trò, kỹ năng, tính cách, cách tương tác của từng người
  trong dự án/đội nhỏ — để giao việc đúng người và giao tiếp hiệu quả. Dữ liệu ở
  work/team.json (gitignored). Trigger phrases: "team", "đội nhóm", "ai làm được",
  "giao cho ai", "hồ sơ thành viên", "match người", "kỹ năng của", "who can do".
---

# Team Registry

Kho dùng chung lưu **vai trò · kỹ năng · tính cách · cách tương tác** của từng
người, để `atask_watch` (đề xuất ASSIGN đúng người) và các watcher TChat (điều
chỉnh tông giao tiếp) cùng tra cứu.

> Dữ liệu ở `work/team.json` — **gitignored** (thông tin cá nhân/đánh giá tính cách
> nhạy cảm, KHÔNG commit). Thuần stdlib, chạy mọi OS.

## Tool (`tools/team.py`)

```
python team.py list [--format summary|json]
python team.py get <key>
python team.py set <key> [--name][--role][--seniority][--skills a,b][--personality]
       [--interaction][--load low|normal|high][--email][--atask-id][--gitlab]
       [--fchat-id][--fchat-username][--projects a,b][--notes]   # upsert, chỉ đổi field truyền
python team.py remove <key>
python team.py match --task "mô tả" [--skills a,b] [--top N] [--exclude k1,k2]
python team.py bootstrap --stdin   # nạp skeleton từ JSON [{id,username,displayName,department}]
```

Mỗi bản ghi: trường CẤU TRÚC (role/seniority/skills/load/handles/projects) để
match + ghi chú TỰ DO (`personality`, `interaction`, `notes`). `key` = username.

## Match người cho task (heuristic + agent)

`team.py match` chấm điểm heuristic (skill trùng +2, role nhắc trong task +1, tải
thấp +1/cao −1.5, lead/senior nhỉnh nhẹ) → trả **shortlist**. Đây là BƯỚC LỌC;
agent đọc shortlist + `team.py get <key>` (hồ sơ đầy đủ) để **chọn cuối + nêu lý do**.

## Tích hợp

- **atask_watch** (nhánh ASSIGN): chạy `team.py match --task "..." --exclude <ATASK_MY_LOGIN>`
  → chọn người hợp nhất → đưa tên vào đề xuất.
- **TChat watcher**: `team.py get <fchat_username>` để lấy `personality`/`interaction`
  → điều chỉnh giọng khi nhắn/giao việc.

## Slash command

- `/team` — xem/cập nhật hồ sơ, hoặc match người cho một task (xem `.claude/commands/team.md`).

## Guardrail

- `set`/`remove`/`bootstrap` ghi vào `work/team.json` → [WRITE], xác nhận khi sửa
  hàng loạt. Không commit file này. Không bịa skill/tính cách — chỉ ghi điều người
  dùng cung cấp hoặc quan sát được.
