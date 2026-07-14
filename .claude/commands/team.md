# Team Registry: xem · cập nhật · match người

Quản lý hồ sơ đội nhóm (vai trò/kỹ năng/tính cách/cách tương tác) và gợi ý người
làm cho một task. Dữ liệu: `work/team.json` (gitignored).

Tham số: `$ARGUMENTS` — vd `match <mô tả task>`, `get <key>`, `set <key> ...`,
hoặc rỗng = liệt kê đội nhóm.

## Quy trình

1. Đọc @.claude/skills/team-registry/SKILL.md.
2. `cd .claude/skills/team-registry/tools` (macOS/Linux: `python3`).
3. Theo ý định:
   - **Xem:** `python team.py list` hoặc `python team.py get <key>`.
   - **Cập nhật:** `python team.py set <key> --role ... --skills ... --personality "..."`
     (là `[WRITE]` — xác nhận trước khi sửa hàng loạt).
   - **Match người cho task:**
     a. `python team.py match --task "<mô tả>" --exclude <ATASK_MY_LOGIN trong .env>` → shortlist heuristic.
     b. Với top ứng viên: `python team.py get <key>` đọc hồ sơ đầy đủ.
     c. **Chọn cuối + nêu lý do** (skill khớp, tải, tính cách phù hợp việc), trình người dùng.
4. Nếu hồ sơ còn trống (mới bootstrap), gợi ý người dùng bổ sung skill/role/tính cách.

## Ghi chú

- `key` = username (vd `baond17`). Bootstrap từ TChat: xem `team.py bootstrap`.
- Không bịa kỹ năng/tính cách — chỉ ghi điều người dùng cung cấp hoặc quan sát được.
- File `work/team.json` KHÔNG commit (nhạy cảm).
