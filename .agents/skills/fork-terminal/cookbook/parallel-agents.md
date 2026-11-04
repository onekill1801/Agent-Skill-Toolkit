# Parallel Isolated Agents

Chạy nhiều agent (Claude/Codex/Gemini...) song song trên CÙNG một repo mà không giẫm lên nhau,
bằng cách cấp cho mỗi agent một **worktree riêng + cổng riêng + database riêng**.

## Vì sao cần

Nếu fork nhiều terminal trỏ vào cùng thư mục dự án, chúng dùng chung checkout (đụng nhánh/file),
chung `server.port` (đụng cổng), chung DB test (ghi đè dữ liệu của nhau). Cơ chế cách ly giải quyết
cả ba: mỗi agent một không gian ảo độc lập.

## 3 tool (stdlib-only)

| Tool | Vai trò |
|------|---------|
| `tools/worktree_manager.py` | `git worktree` — tạo/xoá thư mục `{project}-agent-{task_id}` trên nhánh `feature/task-{task_id}` |
| `tools/runtime_isolator.py` | Regex đổi `PORT`/`server.port` → [8000,9000] và tên DB → `{db}_task_{task_id}` trong `.env`/`application.properties`/`application.yml` |
| `tools/spawn_isolated_agent.py` | Orchestrator: create → isolate → fork terminal với **CWD = worktree** |

## Quy trình (1 lệnh / agent)

```bash
cd .claude/skills/fork-terminal/tools

# 1) Xem trước (KHÔNG mở terminal) — kiểm tra cổng/DB được cấp:
python spawn_isolated_agent.py spawn --project <repo> --task 5001 --cmd "claude" --dry-run

# 2) Spawn thật:
python spawn_isolated_agent.py spawn --project <repo> --task 5001 --cmd "claude"

# ...lặp cho 5002, 5003 -> 3 agent song song, mỗi agent worktree+cổng+DB riêng.

# 3) Dọn khi xong (force nếu worktree còn thay đổi chưa commit):
python spawn_isolated_agent.py cleanup --project <repo> --task 5001 --force
```

> `<repo>` lấy từ `work/projects.json` (`clone_dir`). Windows dùng `python`, macOS/Linux dùng `python3`.

## Output

`spawn` trả JSON gồm: `workspace` (đường dẫn + nhánh), `isolation` (cổng đã cấp + thay đổi từng file),
`cwd` (= worktree), `launched`. Lỗi trả `{"error": true, "step": ..., "detail": ...}`; nếu isolate
lỗi sau khi worktree đã tạo, orchestrator tự rollback (xoá worktree) để không bỏ lại rác.

## Lưu ý

- **Cổng tất định theo task_id**: gọi lại cùng task → cùng cổng (giảm trôi cấu hình). Tool **idempotent**
  — chạy isolate nhiều lần không nối chồng hậu tố DB hay đổi cổng lung tung.
- **Chỉ đổi cổng SERVER**: `REDIS_PORT`, `spring.redis.port`, `DB_PORT`... được giữ nguyên.
- **Cách ly DB chỉ đổi TÊN** trong cấu hình — việc tạo schema/DB thật (nếu cần) là bước riêng của
  quy trình test, ngoài phạm vi tool này.
- Tích hợp `/auto-dev`: ở bước Implement có thể gọi `spawn_isolated_agent.spawn(...)` để giao mỗi
  sub-task cho một agent phụ chạy trong worktree cách ly, rồi `cleanup` sau khi merge.
