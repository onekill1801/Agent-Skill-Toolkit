# CLAUDE.md

Agent-first monorepo: bộ **skill cho Claude Code** tự động hoá công việc dev/quản lý task.
Mục tiêu: giải phóng người dùng khỏi công việc lặp lại (đọc task → code → MR → review → báo tester).

> Đây là file ngữ cảnh nhẹ, nạp mỗi phiên. KHÔNG cần chạy `/prime` cho công việc thường ngày —
> chỉ dùng `/prime` khi muốn khám phá sâu toàn bộ codebase lần đầu (nó nạp ~40k token).

## Bố cục

```
.claude/
  commands/                 # Slash commands (/list-tasks, /review-mr, /fix-bug, ...)
  skills/
    dev-automation/         # Azure DevOps + GitLab: task → branch → code → MR → review → notify
    atask-automation/       # aTask: task/sprint/checklist/analytics (PAT auth)
    fork-terminal/          # Spawn agent khác (Claude/Codex/Gemini) ra terminal mới
    skill-scaffold/         # Meta-skill: trích tool từ app khác → sinh skill mới
.env                        # Token + cấu hình (KHÔNG commit). Mẫu: .env.sample
temp/                       # Output tạm (đã gitignore)
```

Mỗi skill có: `SKILL.md` (trigger + workflow + routing) · `cookbook/` (chi tiết) · `prompts/` · `tools/` (Python).

## Workspace đa project

Repo này là **toolset dùng chung**. Workspace project sống ở `./work/` (trong repo, đã **gitignore** → thấy được nhưng không commit):
- `./work/projects.json` — registry: project → `{gitlab_project_id, azure_project, clone_dir, stack, environments, ...}`.
- `./work/<name>/` — bản clone của từng repo (vd `./work/atask` = `<group>/atask`).
- `./work/proj.sh` — switcher cho terminal con người (`source` rồi `proj <name>`).

> Tool đọc registry tại `<repo>/work/projects.json` (đè bằng env `WORK_DIR` nếu cần).

Chuyển project = export `GITLAB_PROJECT_ID` / `AZURE_DEVOPS_PROJECT` (env đè `.env` — đã verify).
Agent: đọc `projects.json`, **prefix env inline** mỗi lệnh, vd:
`GITLAB_PROJECT_ID=123 AZURE_DEVOPS_PROJECT=My-Project python3 gitlab_api.py list-mrs`.
Chi tiết: `dev-automation/cookbook/multi-project.md`.

## Chạy tool Python

- Tool **chỉ dùng stdlib** (`urllib`), không cần `pip install`.
- `cd` vào thư mục `tools/` của skill rồi chạy. Windows: `python` · macOS/Linux: `python3`.
- Config đọc từ `.env` ở gốc repo (tự dò ngược tối đa 5 cấp thư mục).
- Tool trả JSON; lỗi trả `{"error": true, "status": ..., "message": ...}` — đọc và xử lý, đừng coi là crash.

### dev-automation (`.claude/skills/dev-automation/tools/`)
```
python config.py                                   # kiểm tra .env
python azure_devops.py get <id>                    # đọc work item
python azure_devops.py list [email]                # task được giao (@Me nếu trống)
python azure_devops.py state <id> <New|Active|Resolved|Closed>   # [WRITE]
python azure_devops.py comment <id> "text"         # [WRITE]
python gitlab_api.py list-mrs [opened|merged|all]
python gitlab_api.py mr-detail <iid>
python gitlab_api.py mr-changes <iid>              # diff (TỐN TOKEN với MR lớn)
python gitlab_api.py mr-discussions <iid>
python gitlab_api.py create-branch <name> [ref]    # [WRITE]
python gitlab_api.py create-mr <source> "title" [target]   # [WRITE]
python gitlab_api.py mr-comment <iid> "body"       # [WRITE]
python gitlab_api.py merge-mr <iid> [--keep-source] [--squash]   # [WRITE] accept/merge MR
python gitlab_api.py whoami                         # user GitLab hiện tại
python gitlab_api.py my-review-mrs [reviewer|assignee|both]   # MR gắn tên tôi (toàn project)
python mr_watch.py [--who ...] [--interval 300] [--once] [--no-spawn]   # poll MR gắn tên tôi → tự mở Claude review (hỏi trước khi post)
python daemon_common.py status   # tình trạng các daemon (last event: started/transient/fatal/recovered) từ temp/daemon_health.jsonl
python status.py [--json] [--section daemons,runs,queue,approvals,feedback]   # BẢNG TRẠNG THÁI tổng hợp: daemon + pipeline run đang mở + queue + approval chờ + feedback stats
python notifier.py started|mr-created|review-done|deploy-done|custom <id> ...   # [WRITE → người thật thấy]
python feedback.py add|recall|list|search|stats --project <P> ...   # sổ học từ can thiệp người (recall bơm bài học cũ vào prompt run sau)
```

### atask-automation (`.claude/skills/atask-automation/tools/`)
```
python tasks.py get|query|subtasks|by-sprint ...   # đọc
python tasks.py get-detail <id>                    # REST trực tiếp /api/tasks/{id} — đủ field hơn kênh ai/execute (cần ATASK_BEARER_TOKEN)
python tasks.py create|update|complete|move|assign-sprint|delete ...   # [WRITE]
python projects.py my-projects|my-lists|sprints|get-sprint|workspace ...
python search.py my-tasks|tasks|dashboard|candidates ...   # --status-type · --format summary|table|json · --fields ...
python analytics.py stats|by-status|by-priority|overdue|trends|finish-rates|unassigned|history ...
python analytics.py my-dashboard|org-dashboard|user-dashboard|project-dashboard|workload|by-metric|recent ...
python governed_search.py search --entity task --filter "field:op:value" ...   # DSL an toàn (whitelist)
python checklists.py list|create|delete|comments|add-comment|del-comment ...   # write có [WRITE]
```
Tất cả route qua `POST /api/ai/execute`, auth bằng header PAT. (52 tool server-side; map đầy đủ
trong `module/aiagent/tools/` của repo atask.)

## Cấu hình (.env)

| Khoá | Dùng cho |
|---|---|
| `AZURE_DEVOPS_ORG/PROJECT/PAT` | dev-automation |
| `GITLAB_URL/PRIVATE_TOKEN/PROJECT_ID` | dev-automation |
| `SSL_VERIFY` | dev-automation (true mặc định; false cho cert nội bộ) |
| `ATASK_BASE_URL/PAT_TOKEN/PAT_HEADER/VERIFY_SSL` | atask-automation |

Khi tắt SSL verify → cảnh báo người dùng.

## Guardrail (BẮT BUỘC)

1. **Hỏi xác nhận trước thao tác `[WRITE]`** lần đầu trong một mạch tự động: tạo branch/MR, đổi state, **mọi `notifier.py`** (người thật sẽ thấy).
2. **Luôn confirm** trước thao tác xoá (`delete`, `del-comment`, `delete-branch`) và trước khi chuyển task sang trạng thái cuối (Done/Closed/Cancelled).
3. **Không hardcode token/URL** — luôn qua `config.py`.
4. Đọc cookbook liên quan trước khi chạy một workflow nhiều bước.

## Quy ước

- Branch: `<type>/<task_id>-<short-kebab-desc>` (vd `bugfix/12345-fix-null-pointer`).
- MR title: `<Type>: <Task Title>` (vd `Fix: NullPointerException in UserService`).
- Code Java/Spring Boot: theo `dev-automation/cookbook/java-standards.md`.

## Nền tảng (cross-platform: Windows · macOS · Ubuntu)

Toolkit chạy trên cả 3 OS. Kiểm tra mức sẵn sàng của máy:
```
python dev-automation/tools/doctor.py        # (hoặc python3) — báo OS, Python, CLI có/thiếu, feature nào chạy được
```

- **Python**: `python` (Windows) · `python3` (macOS/Linux). Tool chỉ dùng **stdlib**, không pip.
- **UTF-8**: mọi tool tự ép stdout về UTF-8 (qua `config.py` / `probe_common.py`) → in tên/nội dung
  tiếng Việt không crash trên console Windows cp1252. Phòng hờ có thể đặt env `PYTHONUTF8=1`.
- **Đường dẫn**: dùng `os.path` (không hardcode `/` hay `\`); registry/`clone_dir` là dữ liệu máy-cụ-thể.
- **Tool thuần stdlib (urllib/socket) → chạy mọi OS, chỉ cần token**: azure/gitlab/notifier, atask,
  `probe_api/redis/kafka`, `probe_db` (mysql+postgres qua socket — `mysql_client.py`/`postgres_client.py`,
  KHÔNG cần psql/mysql CLI), `jenkins`, `kafka_ui`, `flow_check`, `local_app` (start/wait-health/stop
  app local cho e2e), `run_log`, `postman_gen`.
- **Tool wrap CLI ngoài (cần cài trên máy)**: `test_runner` (mvn/gradle/npm/go/pytest theo project),
  `local_app` chạy lệnh app (vd mvn spring-boot:run), bước commit/push (git). `doctor.py` cho biết cái nào thiếu.
- `fork_terminal.py`: Windows (`cmd /k`), macOS (`osascript`/AppleScript), Linux (gnome-terminal/
  x-terminal-emulator/xterm — tự dò).
- Môi trường phát triển chính: Windows + PowerShell; đã thiết kế để dev/CI trên macOS/Ubuntu chạy như nhau.
