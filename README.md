# Agent Skill Toolkit

Bộ **skill cho Claude Code** tự động hoá công việc dev/quản lý task: nhận yêu cầu (từ hệ
quản lý hoặc trực tiếp) → **lên kế hoạch → code → kiểm thử → giao sản phẩm** — giải phóng bạn
khỏi việc lặp lại (đọc task · code · MR · review · test · deploy · báo tester).

Đây là **toolset dùng chung**, không chứa code project. Code các project sống trong `./work/`
(xem [Workspace đa project](#workspace-đa-project--đa-môi-trường)).

> Chạy đa nền tảng: **Windows · macOS · Ubuntu**. Tool chỉ dùng **Python stdlib** (không cần `pip`).

---

## Mục lục
- [Bắt đầu nhanh](#bắt-đầu-nhanh)
- [Kiểm tra môi trường (doctor)](#kiểm-tra-môi-trường)
- [Các skill](#các-skill)
- [Pipeline auto-dev](#pipeline-auto-dev-plan--implement--test--deliver)
- [Workspace đa project & đa môi trường](#workspace-đa-project--đa-môi-trường)
- [Bộ công cụ kiểm thử (stack-verify)](#bộ-công-cụ-kiểm-thử-stack-verify)
- [Bàn giao API cho FE/tester](#bàn-giao-api-cho-fetester)
- [Slash commands](#slash-commands)
- [Cấu hình (.env)](#cấu-hình-env)
- [Guardrail an toàn](#guardrail-an-toàn)
- [fork-terminal & skill-scaffold](#fork-terminal--skill-scaffold)

---

## Bắt đầu nhanh

```bash
cd /path/to/fork-repository-skill
cp .env.sample .env        # điền token Azure/GitLab/Jenkins/... (xem mục Cấu hình)
claude                     # mở Claude Code trong thư mục này
```

Hai cách ra lệnh:

1. **Nói tự nhiên** — skill tự kích hoạt: `"deploy atask lên dev"`, `"review MR 524"`,
   `"fix bug task 6955"`, `"đọc 20 message mới nhất topic X"`, `"task của tôi"`.
2. **Slash command** — gõ `/<tên>`: `/auto-dev 6955`, `/review-mr 524`, `/list-tasks`, ...

Chạy tool Python trực tiếp: `cd .claude/skills/<skill>/tools` rồi `python <tool>.py ...`
(Windows dùng `python`, macOS/Linux dùng `python3`). Tool trả JSON; lỗi trả
`{"error": true, ...}` — đọc và xử lý, đừng coi là crash.

## Kiểm tra môi trường

Trước khi dùng trên máy mới, kiểm tra mức sẵn sàng:

```bash
python .claude/skills/dev-automation/tools/doctor.py
```
Báo: OS, phiên bản Python, CLI nào có/thiếu (git/mvn/npm/psql/mysql/...), `fork_terminal`
chạy được chưa, và trạng thái `.env`.

---

## Các skill

| Skill | Việc làm | Trigger ví dụ |
|---|---|---|
| **auto-dev** | Orchestrator: chạy cả chuỗi Plan→Implement→Test→Deliver (có checkpoint) | `/auto-dev <task>`, "làm task X tự động" |
| **dev-automation** | Azure DevOps + GitLab: đọc task, review MR, fix bug, feature, tạo MR, báo tester | "review MR", "fix bug", "list my tasks" |
| **atask-automation** | aTask: task/sprint/checklist/analytics (PAT auth) | "tạo task", "task của tôi", "thống kê" |
| **fork-terminal** | Spawn agent khác (Claude/Codex/Gemini) ra terminal mới | "fork terminal use claude code to..." |
| **skill-scaffold** | Trích tool từ app khác → sinh skill mới trong repo | "extract tools", "scaffold skill" |

Mỗi skill có `SKILL.md` (trigger + workflow) · `cookbook/` (chi tiết) · `prompts/` · `tools/` (Python).

---

## Pipeline auto-dev (Plan → Implement → Test → Deliver)

`/auto-dev <task_id>` (hoặc "làm task X tự động") chạy cả chuỗi, **dừng xin bạn duyệt ở 3 mốc**:

```
Intake ─→ Plan ─[✋ duyệt plan]─→ Implement ─→ Test ─(xanh?)─[✋ trước MR]─→ Deliver ─[✋ trước notify]
  task        kế hoạch              code+test      ▲  no                       tạo MR        báo tester
(Azure/aTask                                       └─ fix & chạy lại (tối đa 3 lần)
 /trực tiếp)
```

- **Cổng test bắt buộc**: không tạo MR khi test còn đỏ.
- **Resume**: pipeline lưu trạng thái ở `temp/runs/<run_id>.json` (`run_log.py`) → ngắt giữa chừng vẫn tiếp tục được.
- Chi tiết: `.claude/skills/auto-dev/cookbook/pipeline.md` · intake: `cookbook/intake.md`.

Tool nền: `test_runner.py` (chạy build/test, auto-detect mvn/npm/gradle/pytest/go), `run_log.py` (state machine).

---

## Workspace đa project & đa môi trường

Registry tại **`./work/projects.json`** (gitignored — thấy được, không commit; đè bằng env `WORK_DIR`).
Mẫu đầy đủ: `workspace/projects.sample.json`.

```
./work/
  projects.json     # registry: project → {gitlab/azure, clone_dir, stack, environments, ...}
  atask/            # git clone của từng project
```

Mỗi project khai báo:
- **clone_dir** — nơi code project (để code/commit/push).
- **environments.<env>** — mỗi môi trường (local/dev/uat/sandbox/prod) có riêng:
  - **branch** git (work thẳng vào nhánh env: `dev→dev`, `uat→uat`, `prod→prod`, `sandbox→pre-prod`),
  - **jenkins.job** riêng (mỗi env một job),
  - endpoint stack-verify (api/db/redis/kafka).
- **protected_envs** (mặc định `prod`) — thao tác **ghi/deploy** vào env này cần cờ `--allow-prod`.

Mọi tool nhận `--project <name> --env <env>`. Ví dụ deploy + lấy nhánh đích:
```bash
cd .claude/skills/dev-automation/tools
python jenkins.py build --project atask --env dev --wait          # deploy dev
python jenkins.py build --project atask --env prod --allow-prod --wait   # prod cần --allow-prod
python -c "import project_config as p; print(p.target_branch('atask','uat'))"   # → uat
```

> Ưu tiên config: cờ CLI > env shell > `environments.<env>` > `stack` base > `.env` chung.
> Chi tiết: `dev-automation/cookbook/multi-project.md`, `auto-dev/cookbook/stack-verify.md`.

---

## Bộ công cụ kiểm thử (stack-verify)

Kiểm thử full luồng backend thật — đọc/assert state từng thành phần, hoặc chạy kịch bản e2e.
Tất cả ở `.claude/skills/dev-automation/tools/`.

| Tool | Thành phần | Ví dụ |
|---|---|---|
| `probe_api.py` | HTTP API | `probe_api.py call --url /health --expect-status 200 --expect-json '$.x=y'` |
| `probe_db.py` | DB (Postgres/MySQL, bọc psql/mysql) | `probe_db.py query --engine postgres --sql "select ..." --expect-rows 1` |
| `probe_redis.py` | Redis (RESP qua socket) | `probe_redis.py get user:42 --expect-exists` |
| `probe_kafka.py` | Kafka qua **Confluent REST Proxy** | `probe_kafka.py consume --topic t --expect-contains '"uid":42'` |
| `kafka_ui.py` | Kafka qua **Provectus Kafka UI** (login form) | `kafka_ui.py messages --cluster c --topic t --from latest` |
| `jenkins.py` | CI/CD Jenkins (build/status/console/jobs) | `jenkins.py build --project atask --env dev --wait` |
| `flow_check.py` | **E2E**: kịch bản JSON nối API→DB→Kafka→Redis | `flow_check.py --file scenario.json --project atask --env dev` |

- `passed:false` = assertion fail; `{"error":true}` = **không chạy được** (service down/thiếu config) → coi là *chưa verify*, không phải đạt.
- **Guard prod**: probe/flow có thao tác **ghi** vào env protected bị từ chối trừ khi `--allow-prod`; thao tác **đọc** luôn cho phép.
- Mẫu kịch bản e2e: `auto-dev/scenarios/example-create-user.json`. Chi tiết: `auto-dev/cookbook/stack-verify.md`.

---

## Bàn giao API cho FE/tester

Sau khi viết feature/fix, sinh **Postman Collection** để FE/tester import là dùng — không cần
chỉnh sửa (backend không cần Swagger; tool parse controller Spring):

```bash
python .claude/skills/dev-automation/tools/postman_gen.py --project atask
# → temp/atask.postman_collection.json (folder theo controller, {{baseUrl}}/{{token}}, body skeleton + tên DTO)
```
FE: Postman → Import → set `baseUrl`/`token` 1 lần → chạy mọi endpoint.

---

## Slash commands

Gõ trong Claude Code (thư mục làm việc là repo này).

| Command | Việc |
|---|---|
| `/prime` | Onboard sâu toàn bộ codebase (nạp ~40k token — chỉ khi khám phá lần đầu) |
| `/all_skills` | Liệt kê skill + command |
| `/auto-dev <task\|mô tả>` | Chạy full pipeline (Plan→Implement→Test→Deliver, có checkpoint) |
| `/list-tasks` · `/read-task <id>` | Task Azure DevOps được giao / đọc 1 task |
| `/review-mr <iid>` | Review GitLab merge request |
| `/fix-bug <id>` · `/implement-feature <id>` | Workflow fix bug / làm feature |
| `/notify-tester <id> [url]` | Báo tester qua comment Azure DevOps |
| `/atask-create` · `/atask-search` · `/atask-projects` · `/atask-stats` | Tác vụ aTask |
| `/extract-tools` · `/design-skill` · `/scaffold-skill` | Sinh skill mới từ app khác |

Mọi command đều có bản nói-tự-nhiên tương đương.

---

## Cấu hình (.env)

Copy `.env.sample` → `.env` (đã gitignore). Các khoá chính:

| Nhóm | Khoá |
|---|---|
| Azure DevOps | `AZURE_DEVOPS_ORG/PROJECT/PAT` |
| GitLab | `GITLAB_URL/PRIVATE_TOKEN/PROJECT_ID` |
| aTask | `ATASK_BASE_URL/PAT_TOKEN/PAT_HEADER/VERIFY_SSL` |
| SSL | `SSL_VERIFY` (false cho cert nội bộ) |
| Stack-verify | `API_BASE_URL`, `DB_*`/`PG_URL`, `REDIS_*`, `KAFKA_REST_URL` |
| Jenkins | `JENKINS_URL/USER/TOKEN` (nên dùng **API token**, không phải mật khẩu) |
| Kafka UI | `KAFKA_UI_URL/USER/PASSWORD/LOGIN_PATH` |

> ⚠️ **Giá trị chứa ký tự `#` phải bọc nháy kép**, vd `KAFKA_UI_PASSWORD="abc#123"` —
> nếu không, phần sau `#` bị cắt như comment.

---

## Guardrail an toàn

1. **Hỏi xác nhận trước thao tác `[WRITE]`** lần đầu trong một mạch tự động: tạo branch/MR, đổi
   state task, **mọi notification** (người thật sẽ thấy), **deploy/build**.
2. **Luôn confirm** trước thao tác xoá và trước khi chuyển task sang trạng thái cuối (Done/Closed).
3. **Guard prod**: ghi/deploy vào `protected_envs` cần `--allow-prod` (agent không tự ý dùng cờ này).
4. **Không hardcode token/URL** — qua `config.py`/`.env`/registry.

---

## fork-terminal & skill-scaffold

**fork-terminal** — spawn agent/CLI ra cửa sổ terminal mới (Windows `cmd /k` · macOS AppleScript ·
Linux gnome-terminal/x-terminal-emulator/xterm — tự dò). Dùng để delegate/parallel:
```
"fork terminal use claude code to refactor the auth module"
"fork terminal use gemini fast to write tests for the API"
"new terminal: npm run dev"
```

**skill-scaffold** — trích tool từ app khác (Python/MCP/OpenAPI/TS) → sinh skill mới:
`/extract-tools` → `/design-skill` → `/scaffold-skill`.

---

## Kiến trúc

```
.claude/
  commands/                  # slash commands
  skills/
    auto-dev/                # orchestrator: pipeline + stack-verify + scenarios
    dev-automation/          # Azure/GitLab + stack-verify tools + multi-project + doctor
    atask-automation/        # aTask
    fork-terminal/           # spawn agent ra terminal
    skill-scaffold/          # sinh skill mới
.env                         # token + cấu hình (gitignored)
work/                        # registry + clone project (gitignored)
temp/                        # output tạm (gitignored)
workspace/projects.sample.json   # mẫu registry
CLAUDE.md                    # ngữ cảnh nạp mỗi phiên
```

Skill này phát triển từ [Fork Terminal Skill](https://youtu.be/X2ciJedw2vU) của IndyDevDan —
xem [Tactical Agentic Coding](https://agenticengineer.com/tactical-agentic-coding) và
[IndyDevDan](https://www.youtube.com/@indydevdan).
