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
    mr-review/              # Hàng đợi review MR: worktree đúng diff_refs → review.json → DUYỆT → đăng
  hooks/guard_write.py      # PreToolUse: lệnh ghi/notify/đọc secret → hỏi (headless → chặn)
.claude-plugin/ .codex-plugin/   # manifest plugin (Orca/Claude/Codex nhận skill ở .claude/skills)
.env                        # Token + cấu hình (KHÔNG commit). Mẫu: .env.sample
temp/                       # Output tạm (đã gitignore)
```

Mỗi skill có: `SKILL.md` (trigger + workflow + routing) · `cookbook/` (chi tiết) · `prompts/` · `tools/` (Python).

Code mới đặt trong package `.claude/skills/_runtime/agent_skill_toolkit/` (`core` · `integrations` · `workflows`),
skill chỉ là vỏ: tool gọi `_runtime/bootstrap.py` rồi import đủ namespace — xem `docs/design/agent-ecosystem.md §B`.

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

- **Cách gọn nhất — một entry point:** `python agentkit.py <tool> [args]` (chạy từ gốc repo, không cần
  `cd`). `--list [chuỗi]` xem tool có sẵn · `--where <tool>` xem file nào sẽ chạy. Tên trùng giữa các
  skill (`config`) phải ghi rõ `<skill>:<tool>` — agentkit từ chối đoán. Nó cũng đặt thư mục tools của
  skill sở hữu **lên đầu** `sys.path` nên tool luôn import đúng `config` của mình.
- Tool **chỉ dùng stdlib** (`urllib`), không cần `pip install`.
- Cách cũ vẫn chạy y nguyên: `cd` vào `tools/` của skill rồi gọi. Windows: `python` · macOS/Linux: `python3`.
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
python mr_watch.py [--who ...] [--interval 300] [--once] [--no-spawn] [--mode store|terminal]   # poll MR gắn tên tôi → xếp task review_mr vào store (mặc định; `review_flow.py work` chạy) · terminal = mở Claude như cũ
python daemon_common.py status   # tình trạng các daemon (last event: started/transient/fatal/recovered) từ temp/daemon_health.jsonl
python ../auto-dev/tools/verify.py plan|start --repo DIR [--base B] [--spec F] | work [--once] | status   # THANG KIỂM: build→test liên quan→suite→review chéo, tự sửa tới xanh (spec đọc từ base ref; không chạy trên nhánh mặc định)
python ../auto-dev/tools/refactor.py survey --repo DIR [--depth 2] | ratchet check|init|tighten --repo DIR | program new|approve|status | snapshot|metrics record --rung R   # LÀN LỚN: khảo sát module/vòng phụ thuộc/điểm nóng + bánh cóc kiến trúc (ngoại lệ chỉ giảm; bậc `kind: ratchet` trong thang kiểm)
python ../auto-dev/tools/proposals.py scan --repo DIR [--enqueue] | new --repo DIR --question Q | go <id> | decide <id> accept|reject --reason R   # LÀN ĐỀ XUẤT: quét tuần (không token) → [Làm] → debate → ADR → bạn quyết
python ../auto-dev/tools/accounts.py [--json]   # TÀI KHOẢN agent (work/agents.json, gồm tài khoản Orca): đang làm gì + dung lượng còn lại (số từ Orca)
python ../auto-dev/tools/coordinator.py work [--parallel 2] [--once] | board | accept <id> [--lane L] | capacity [N] | usage [PCT] | budget [--daily-agent-runs N]   # ĐIỀU PHỐI mọi loại việc trong store (giới hạn song song toàn cục, phụ thuộc, khoá repo) + bảng tiến độ
python taskstore.py summary | list [--status ...] | show <id> | events [<id>] | add <kind> <ref> | unblock|cancel <id>   # store bền (temp/toolkit.db): task, lease, checkpoint, dừng khi hết quota → chạy tiếp
python dashboard.py serve [--port 8765]   # GIAO DIỆN WEB chỉ đọc (127.0.0.1): việc, tài khoản, review MR, duyệt chờ, daemon… — mở http://127.0.0.1:8765 trong trình duyệt Orca
python status.py [--json] [--section daemons,runs,queue,approvals,feedback]   # BẢNG TRẠNG THÁI tổng hợp: daemon + pipeline run đang mở + queue + approval chờ + feedback stats
python k8s.py doctor|contexts                       # kubectl (PATH/Lens) + kubeconfig có trên máy — không gọi mạng
python k8s.py access|pods|deploys|events|describe|logs|get --project P --env E   # READ-ONLY; hoặc --context C --kubeconfig F [-n NS]
python k8s.py forwards [--listening]               # port-forward đã lưu trong Lens: cluster/context, svc, cổng local, đang mở?
python dbeaver.py list [--q t] [--folder PROD] | show "Folder/Tên" | forwards   # metadata DBeaver (KHÔNG đọc credential)
python dbeaver.py probe "Folder/Tên" --sql "select …"   # qua probe_db; user/pass từ env DB_USER/DB_PASSWORD; PROD = chỉ đọc
python podfwd.py start <pod/x|svc/x|deploy/x|"-l app=db"> <local>:<remote> --project P --env E [--name N]   # forward nền, tự nối lại
python podfwd.py start-saved --project P --env E | list | stop <name|all> | logs <name>   # PROD cần --allow-prod (hook hỏi)
python podfwd.py fetch <pod|"-l sel"> <path-trong-pod> [--to DIR]   # oc rsync pod -> work/pods/ (đọc code/config đã deploy)
python code_context.py callers|impact|locate <symbol> | affected <file>... | trace < stack.txt | status --repo DIR   # NGỮ CẢNH CODE: CodeGraph khi index mới (cũ → sync), không thì grep; luôn ghi source
python cg.py prepare <worktree> --project-id <id> | analyze <worktree> --base B --head H --out F   # codegraph cho worktree: copy seed + sync (giây), method bị sửa, nơi gọi ngoài diff, test bị ảnh hưởng
python vpn.py status | up | ensure | down | watch    # FortiVPN (macOS/Linux: openfortivpn; Windows: FortiClient). status exit 0 = mạng nội bộ OK
python k8s.py logs ... deploy/NAME [--since 30m] [--errors -C 3] [--grep RE] [--previous]   # gộp log mọi pod của deploy; --errors bắt text/JSON/glog
python notifier.py started|mr-created|review-done|deploy-done|custom <id> ...   # [WRITE → người thật thấy]
python ../auto-dev/tools/agent_init.py scan|draft|apply|check --project <P>   # chuẩn .agent/ cho repo sản phẩm (apply là [WRITE])
python feedback.py add|recall|list|search|stats --project <P> ...   # sổ học từ can thiệp người (recall bơm bài học cũ vào prompt run sau)
```

### mr-review (`.claude/skills/mr-review/tools/`) — xem `mr-review/SKILL.md`
```
python review_queue.py add --project etask --iid 123 | add-text "<tin có link MR>" | list --brief | next | pending
python review_ws.py prepare <job> [--with-merge] | diff <job> [--path P] | check-stale <job> | cleanup <job> | gc
python review_post.py validate|render|request-approval <job>    # agent dừng ở đây
python review_post.py approve|reject <job>                      # CHỈ người (cần TTY), duyệt gắn với đúng nội dung
python review_rules.py check <job>                              # luật tự đăng (REVIEW_AUTO_POST=off|shadow|auto)
python review_flow.py enqueue --project P --iid N | enqueue-text "<tin>" --source fchat | work [--once] | reconcile | status   # review BỀN: hết quota → dừng/chạy tiếp, MR đổi commit → review lại
python review_post.py sweep                                     # [WRITE] đăng review đã duyệt + báo FChat
```
Orca: automation "Auto review MR" = công tắc (precheck `review_queue.py pending`); listener FChat
(`tchat-automation/tools/group_watch.py`) chỉ xếp hàng + kích hoạt. `orca_adapter.py` (dev-automation)
là nơi DUY NHẤT gọi `orca` CLI. Router model/tài khoản: `agent_runner.py --role <r>` theo `work/agents.json`.
Task nhiều repo: `auto-dev/cookbook/multi-repo.md`.

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
   Ngoại lệ duy nhất: comment review MR do `review_post.py sweep` tự đăng khi `REVIEW_AUTO_POST=auto` và
   review qua mọi luật của `mr-review/tools/review_rules.py` (agent vẫn không tự gọi `gitlab_api.py mr-comment`).
1b. **Mốc duyệt là gate thật, không phải trạng thái.** `run_log.py checkpoint ... --source agent` (mặc định)
   **không** đủ để `advance deliver` ở `mode=checkpoint` — cần `--source human|telegram`. Đường chuẩn:
   `tg_gate.py parse` tự ghi checkpoint kèm proof. Đừng tự khai `--source human` khi không có người duyệt thật.
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
