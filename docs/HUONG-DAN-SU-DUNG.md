# Hướng dẫn sử dụng Agent-Skill-Toolkit

## 1. Hiểu nhanh trong 30 giây

Toolkit = **một hàng đợi việc (store) + các "làn" xử lý + một người điều phối**.

```
  Nguồn việc                    Hàng đợi (temp/toolkit.db)          Điều phối               Bạn
  ───────────                   ──────────────────────────          ─────────               ───
  mr_watch (MR gắn tên bạn) ─┐
  TChat group (group_watch) ─┤    task: review_mr / verify /     coordinator.py work     duyệt ở các
  aTask watch               ─┼──▶ proposal / refactor / atask ──▶ (chạy song song,  ──▶  "cổng"
  bạn gõ lệnh (manual)      ─┤    trạng thái: queued/running/     giới hạn quota)        (Telegram / terminal)
  quét tuần (schedule)      ─┘    blocked/paused_quota/done
```

- **Nguồn việc chỉ xếp hàng, không tự chạy.** Phải có `coordinator.py work` đang chạy thì việc `queued` mới được làm (§3.3).
- Việc **không mất** khi hết quota hay tắt máy: task chuyển `paused_quota`, chạy lại `work` là tiếp từ checkpoint.
- Task `blocked` = **đang chờ bạn** (duyệt, hoặc agent không tự xử được). Xem lý do ở `board`.
- Mọi thao tác ra ngoài (comment GitLab, báo tester, đổi state, ghi lên k8s) đều qua cổng duyệt của bạn —
  trừ review MR khi bạn bật tự đăng (§4.2).

## 2. Ba cách dùng (chọn cách hợp nhất)

| Cách | Khi nào | Ví dụ |
|---|---|---|
| **A. Nói chuyện với Claude Code** trong repo này | Hằng ngày, dễ nhất | "review MR 123 của etask", "xem log deploy etask-api ở uat", "restart deploy X" |
| **B. Slash command** | Việc lặp lại có sẵn | `/review-mr`, `/fix-bug`, `/implement-feature`, `/list-tasks`, `/watch-mrs`, `/atask-triage` |
| **C. Chạy tool trực tiếp / daemon** | Muốn tự động chạy nền, hoặc kiểm soát chi tiết | `coordinator.py work`, `group_watch.py` |

Cách A/B: Claude tự chọn tool phía dưới. Phần còn lại của tài liệu là cách C — để bạn hiểu chuyện gì xảy ra bên dưới.

> Tất cả lệnh chạy **từ gốc repo này** (`D:\Claude-cli\Agent-Skill-Toolkit`). Viết tắt: `T=.claude/skills`
> (PowerShell: `$T=".claude/skills"`). **Đừng chạy daemon từ repo cũ** (`fork-repository-skill`): nó đọc/ghi
> thư mục trạng thái riêng, bấm duyệt sẽ không ăn (§5).

## 3. Chuẩn bị một lần

### 3.1 Kiểm máy và cấu hình

```bash
python $T/dev-automation/tools/doctor.py     # máy thiếu gì (git, mvn, kubectl, codex...)
python $T/dev-automation/tools/config.py     # token GitLab/Azure đã đủ chưa
```

### 3.2 Khoá trong `.env` (gốc repo, KHÔNG commit, mẫu: `.env.sample`)

| Nhóm | Khoá | Ghi chú |
|---|---|---|
| GitLab / Azure DevOps | `GITLAB_URL`, `GITLAB_PRIVATE_TOKEN`, `GITLAB_PROJECT_ID`, `AZURE_DEVOPS_ORG/PROJECT/PAT` | bắt buộc cho review MR, task |
| aTask | `ATASK_BASE_URL`, `ATASK_PAT_TOKEN`, `ATASK_PAT_HEADER`, `ATASK_VERIFY_SSL`, `ATASK_MY_LOGIN` | code chỉ đọc tiền tố **`ATASK_`** — khoá `ETASK_*` không có tác dụng |
| TChat | `FCHAT_BASE_API_URL`, `FCHAT_BEARER_TOKEN`, `FCHAT_REFRESH_TOKEN`, `FCHAT_WS_URL`, `FCHAT_WATCH_GROUP` | token tự gia hạn; hết hạn hẳn → dán token mới (§4.9) |
| Telegram (duyệt từ xa) | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHATS`, `TELEGRAM_OPS_BOT=ops`, `TELEGRAM_BOT_TOKEN_OPS`, `TELEGRAM_ALLOWED_CHATS_OPS` | bot OPS hiện nút Duyệt |
| Review tự đăng | `REVIEW_AUTO_POST=off\|shadow\|auto`, `REVIEW_AUTO_POST_PROJECTS=etask` | §4.2 |
| Ai được gửi yêu cầu từ TChat | `FCHAT_REVIEW_ALLOWED=*` (mọi người) hoặc danh sách user id | rỗng = chỉ chính bạn |
| DB | `DB_USER`, `DB_PASSWORD` | cho `probe_db` / `dbeaver.py probe` (§4.11) |
| Khác (khi cần) | `JENKINS_*`, `FORTIVPN_*`, `REDIS_*`, `KAFKA_*` | |

- `SSL_VERIFY=false` / `*_VERIFY_SSL=false` = bỏ kiểm chứng chỉ (cert nội bộ) — tool sẽ in cảnh báo.
- **Không dán nội dung `.env` vào chat / ticket.** Nếu lỡ dán: đổi các token đó (GitLab, Azure, Telegram, DB…).

### 3.3 Các file trong `work/` (gitignore)

- `work/projects.json` — project → GitLab id, thư mục clone, môi trường (`environments.<env>`: `api_base_url`, `k8s`, `forwards`…).
- `work/agents.json` — tài khoản agent (5 tài khoản Claude của Orca + Codex) và thứ tự dùng theo vai (§4.4).
- `work/<tên>/` — code clone của từng project.

### 3.4 Các daemon chạy nền — mở mỗi cái **một tab terminal trong Orca**

Để bạn nhìn thấy log và tự tắt (Ctrl+C / đóng tab). Chạy từ gốc repo:

| Tab | Lệnh | Làm gì |
|---|---|---|
| Coordinator | `python $T/dev-automation/tools/supervise.py --name Coordinator -- python -u $T/auto-dev/tools/coordinator.py work` | **worker**: lấy việc `queued` ra chạy (song song tối đa 10) |
| TChat group_watch | `python $T/dev-automation/tools/supervise.py --name group_watch -- python -u $T/tchat-automation/tools/group_watch.py` | nghe group TChat → xếp review MR; đăng review đã duyệt + báo lại group |
| TG bot OPS | `python $T/dev-automation/tools/supervise.py --name tg_ops -- python -u $T/remote-control/tools/telegram_bridge.py --bot ops --mode approvals-only` | nhận nút **Duyệt/Từ chối** trên Telegram |
| TG bot CODE | `python $T/dev-automation/tools/supervise.py --name tg_code -- python -u $T/remote-control/tools/telegram_bridge.py` | nhắn Telegram → Claude chạy lệnh (tuỳ chọn) |
| MR watch (tuỳ chọn) | `python $T/dev-automation/tools/supervise.py --name mr_watch -- python -u $T/dev-automation/tools/mr_watch.py --interval 300` | 5 phút/lần tìm MR gắn tên bạn → xếp review |
| Dashboard | `python $T/dev-automation/tools/supervise.py --name dashboard --no-reload -- python $T/dev-automation/tools/dashboard.py serve` | trang theo dõi + form cài đặt (§4.0) |

**`supervise.py` bọc ngoài mỗi daemon:** daemon thoát hoặc lỗi → tự chạy lại (chờ 2 s, 4 s … tối đa 60 s;
chạy ổn 5 phút thì về lại 2 s); có **commit mới**, `.env` hoặc `work/agents.json` đổi → tự dừng êm và chạy lại ngay,
nên tab không bao giờ chạy code/token cũ (`--no-reload` để tắt). Ctrl+C trong tab = dừng hẳn. Mọi lần chạy lại được
ghi vào `temp/daemon_health.jsonl` (`daemon_common.py status`).
Kiểm đúng chưa: dòng khởi động bot Telegram phải in `Hub repo: D:\Claude-cli\Agent-Skill-Toolkit`.
Mỗi daemon chỉ chạy **một bản** (hai bản group_watch = trả lời/đăng trùng). Sửa `.env` (tay hoặc qua thẻ Cài đặt) → restart tab liên quan.
Tình trạng: `python $T/dev-automation/tools/daemon_common.py status`.

## 4. Các tình huống dùng hằng ngày

### 4.0 Giao diện web theo dõi (mở trong trình duyệt Orca)

```bash
python $T/dev-automation/tools/dashboard.py serve        # mở một tab Orca; lệnh in ra link http://127.0.0.1:8765/#key=…
```
Mở **đúng link có `#key=…`** (key đổi mỗi lần chạy, chỉ in ở tab này). Mở không có key = chỉ xem.
Một trang tự làm mới (5/10/30 giây): ô số tổng quan, **"Cần bạn xử lý"** (việc bị chặn, yêu cầu duyệt — kèm lệnh, bấm để copy),
toàn bộ việc trong hàng đợi (lọc theo loại), tài khoản agent + dung lượng, review MR, daemon, pipeline run, port-forward, feedback.
- Mỗi thẻ có mục **"Lệnh & cách dùng"** (bấm để mở): các lệnh của chức năng đó + giải thích; bấm vào lệnh để copy.
- Thẻ **Khởi động**: lệnh của từng daemon (mỗi lệnh một tab Orca).
- Thẻ **Cài đặt**: đổi các công tắc hay dùng mà không phải nhớ tên khoá — chế độ đăng review, project được tự đăng,
  ai được gửi yêu cầu từ TChat, từ khoá, tự duyệt, timeout, số việc song song, trần lượt agent/ngày… Đổi rồi bấm
  **Lưu thay đổi**; mục "nhạy cảm" hỏi xác nhận. Sau khi lưu, trang ghi **tab nào cần restart** (cài đặt .env) —
  cài đặt "Điều phối" áp dụng ngay. Token/mật khẩu **không** sửa ở đây (trang chỉ cho biết đã đặt hay chưa) — sửa tay trong `.env`.
  Cùng danh mục ở terminal: `python $T/dev-automation/tools/toolkit_settings.py list` / `set KEY VALUE`.
  Mỗi thay đổi được ghi vào `temp/settings_audit.jsonl`.
- Thao tác khác (duyệt, huỷ việc…) vẫn là lệnh ở terminal. Chỉ nghe `127.0.0.1` (máy khác không vào được).
  Đổi cổng: `--port 9000`. Chỉ xem, không có form: `--read-only`. Dừng: Ctrl+C.

### 4.1 Xem đang có việc gì — lệnh dùng nhiều nhất

```bash
python $T/auto-dev/tools/coordinator.py board          # bảng: việc nào chạy / chờ bạn / hết quota
python $T/dev-automation/tools/status.py               # tổng hợp: daemon, queue, approval đang chờ
python $T/dev-automation/tools/taskstore.py list --status queued   # việc đang chờ worker
```

### 4.2 Review một MR

**Cách nhận việc** (chọn một): người trong group TChat gửi link MR (§4.9) · `mr_watch` tự tìm · hoặc gõ tay:
```bash
python $T/mr-review/tools/review_flow.py enqueue --project etask --iid 123
```
**Chạy:** tab Coordinator (§3.4) tự làm. Chạy một lượt bằng tay: `python $T/mr-review/tools/review_flow.py work --once`.

**Xem và duyệt:**
```bash
python $T/mr-review/tools/review_flow.py status
python $T/mr-review/tools/review_post.py render <job>      # xem đúng nội dung sẽ đăng (job = <project_id>-<iid>, vd 5401-1202)
python $T/mr-review/tools/review_rules.py check <job>      # có đủ điều kiện tự đăng không, vì sao
```
Duyệt: bấm **Duyệt** trên thẻ Telegram (bot OPS), hoặc tự gõ `review_post.py approve <job>` ở terminal của bạn
(agent không gõ thay được). Sau khi duyệt, `group_watch` (hoặc `review_post.py sweep`) đăng lên GitLab.

**Tự đăng không cần hỏi** — trong `.env`:
```
REVIEW_AUTO_POST=auto
REVIEW_AUTO_POST_PROJECTS=etask        # project nào được tự đăng
```
Review chỉ tự đăng khi qua mọi luật (`review_rules.py`): tối đa 30 lần/ngày, độ dài/số comment inline giới hạn,
không chứa secret, không link ra ngoài GitLab, không có HTML/ký hiệu có thể tag người. Trượt luật → quay về hỏi duyệt.
`shadow` = chỉ đánh giá, vẫn hỏi bạn. `off` = luôn hỏi.

**Comment đăng lên GitLab** — mặc định là mẫu **"Code Review Summary"** của team (`REVIEW_NOTE_STYLE=full`):
MR / Reviewer / Date / Commit / Overall Verdict (+ ghi chú), dòng `> Phạm vi: …`, Overview, 🔴 Critical / 🟡 Major
(mỗi mục: tiêu đề, `Why:`, `Suggestion:`), 🟢 Minor / Nitpicks (một đoạn), Standards Compliance, Positive Observations,
Files Reviewed. `compact` = bản rút gọn không bảng (mỗi lỗi 1–2 dòng).
**Comment thẳng vào dòng code** (`REVIEW_INLINE_COMMENTS`) mặc định **tắt** — mọi lỗi gom trong một comment.
Bật (`1`) khi muốn tác giả thấy lỗi ngay tại dòng trong diff và resolve từng cái. Đổi cả hai ở thẻ Cài đặt của dashboard.

**Review không bao giờ tag ai:** mọi `@` ngoài code block được chèn một ký tự vô hình — `@Test`, `@Override`,
`@all` hiển thị như thường nhưng không gửi thông báo. Code trong khối ```` ```java ```` giữ nguyên để copy.
(Trong code một dòng `` `@Override` `` cũng có ký tự vô hình — copy từ khối code, đừng copy từ đó.)

MR có commit mới sau khi review → `review_flow.py reconcile` tự xếp review lại.

### 4.3 Kiểm code tới khi xanh (thang kiểm)

Dùng sau khi bạn (hoặc agent) code xong trên **nhánh feature**. Thang chạy: build → test liên quan → full suite → review chéo; bậc nào đỏ thì agent sửa (không được sửa test/config) rồi chạy lại.

Repo cần khai thang trong `.agent/project.yml` (tạo nhanh bằng `python $T/auto-dev/tools/agent_init.py draft --project etask`):
```yaml
verify:
  base: main
  ladder:
    compile:
      run: mvn -DskipTests package
      protects: []
    suite:
      run: mvn test
      protects: []
```
Chạy:
```bash
python $T/auto-dev/tools/verify.py plan  --repo work/etask                # xem sẽ chạy gì (không tốn token)
python $T/auto-dev/tools/verify.py start --repo work/etask --ac "AC1: tạo task trả 201"
python $T/auto-dev/tools/verify.py work --once
python $T/auto-dev/tools/verify.py report <task_id>                       # AC nào có test chứng minh, AC nào chưa
```

### 4.4 Tài khoản nào đang làm gì, còn bao nhiêu dung lượng

```bash
python $T/auto-dev/tools/accounts.py          # bảng: 5h còn / tuần còn / reset / đang làm / số lượt hôm nay
```
- Danh sách tài khoản: `work/agents.json` (tài khoản Orca qua `CLAUDE_CONFIG_DIR`, mỗi tài khoản tối đa 2 việc cùng lúc).
- **Chia đều tải:** mỗi lượt agent, trong cùng một model (vd Opus), tài khoản **còn nhiều dung lượng nhất** được dùng trước; tài khoản còn dưới 10% (5 giờ hoặc tuần) xếp cuối; tài khoản không đọc được số (token hết hạn) ở giữa. Bận/hết quota → tự chuyển tài khoản kế. Tắt: thẻ Cài đặt → "Chia đều tải giữa các tài khoản" = 0 (về thứ tự trong agents.json); ngưỡng: "Ngưỡng dung lượng tối thiểu".
- Dung lượng **đọc thẳng từ Anthropic** cho từng tài khoản (dùng access token có sẵn trong thư mục tài khoản, tối đa 5 phút/lần) — cột "Cập nhật" ghi `API …`.
- Tài khoản có access token **đã hết hạn** thì toolkit KHÔNG tự gia hạn (tránh đụng Orca) → hiện số Orca đọc lần cuối + dòng "token hết hạn". Mở tài khoản đó một lần trong Orca là token được gia hạn.
- `reset?` = đã qua giờ reset sau lần đọc, số mới chưa biết. `--orca-only` = chỉ dùng số của Orca.

### 4.5 Song song và quota

```bash
python $T/auto-dev/tools/coordinator.py capacity 10      # số việc chạy song song (mặc định đã đặt 10)
python $T/auto-dev/tools/coordinator.py work --parallel 4   # chỉ lần chạy này
python $T/auto-dev/tools/coordinator.py usage 85         # báo mức dùng hiện tại (%)
```
| Mức | Toolkit tự làm |
|---|---|
| ≥ 80% | dừng làn đề xuất; agent opus → sonnet |
| ≥ 95% | chỉ chạy review MR + việc nhỏ |
| 100% | dừng mọi lượt agent (việc → `paused_quota`, không mất) |

Khi quota hồi: `coordinator.py usage 0` — việc chạy tiếp. Ngân sách: `coordinator.py budget --daily-agent-runs 30`.
Trần thật = tổng slot tài khoản (5 tài khoản Claude × 2 + Codex); đặt cao hơn không chạy nhanh hơn.

### 4.6 Đề xuất cải tiến hằng tuần (không tốn token khi quét)

```bash
python $T/auto-dev/tools/proposals.py scan --repo work/etask              # chỉ đọc: thư viện cũ, file "nóng"
python $T/auto-dev/tools/proposals.py scan --repo work/etask --enqueue    # tạo ứng viên chờ bạn chọn
python $T/auto-dev/tools/proposals.py list
python $T/auto-dev/tools/proposals.py go <id>                             # [Làm]: Claude+Codex tranh luận → ADR (TỐN token)
python $T/auto-dev/tools/proposals.py decide <id> accept --reason "..."   # bạn quyết; reject thì toolkit nhớ, không đề xuất lại
```

### 4.7 Refactor lớn theo từng lát

```bash
python $T/auto-dev/tools/refactor.py survey --repo work/etask --depth 2   # bản đồ module, vòng phụ thuộc, điểm nóng (chỉ đọc)
python $T/auto-dev/tools/refactor.py ratchet init --repo work/etask       # chụp số vi phạm kiến trúc hiện tại (cần .agent/architecture.json)
python $T/auto-dev/tools/refactor.py program new --repo work/etask --goal "Tách module X" \
       --invariants "API không đổi" --slices slices.json                 # chia lát
python $T/auto-dev/tools/refactor.py program approve <id>                 # bạn duyệt kế hoạch
python $T/auto-dev/tools/coordinator.py work                              # mỗi lát: code → thang kiểm → nhánh riêng
python $T/auto-dev/tools/refactor.py program status <id>
```
`slices.json`: `[{"id":"s1","title":"..."},{"id":"s2","title":"...","depends_on":["s1"]}]`. Lát có `"run": [lệnh]` chạy bằng tool (vd OpenRewrite), không dùng agent. "Bánh cóc": số vi phạm chỉ được giảm, tăng là lát bị chặn.

### 4.8 Tra cứu code nhanh

```bash
python $T/dev-automation/tools/code_context.py callers <tênHàm> --repo work/etask   # ai gọi hàm này
python $T/dev-automation/tools/code_context.py affected <file> --repo work/etask    # sửa file này ảnh hưởng gì / test nào
python $T/dev-automation/tools/code_context.py trace --repo work/etask < stack.txt  # stacktrace → vị trí code
```

### 4.9 Nhận yêu cầu từ group TChat

Tab **TChat group_watch** (§3.4) nghe đúng một group (`FCHAT_WATCH_GROUP`, hoặc `--group <id>`).

| Ai gửi | Được xử lý khi |
|---|---|
| Người khác trong group | tin có từ khoá (`mr`, `review`, `merge`, `build`, `deploy`, `lên dev`, `!review`…; đổi bằng `FCHAT_WATCH_KEYWORDS`) **và** user id có trong `FCHAT_REVIEW_ALLOWED` (`*` = mọi người) |
| Chính bạn (chủ token) | tin bắt đầu bằng `@bot`, vd `@bot review MR 412` (đổi bằng `FCHAT_SELF_PREFIX`) |

- Link MR / `!iid` → xếp **review MR** (§4.2). Build/deploy → agent chạy, mọi thao tác ghi đều hỏi duyệt Telegram.
  Việc khác bị bỏ qua, trừ khi `FCHAT_WATCH_FREEFORM=1`.
- `FCHAT_WATCH_AUTO_APPROVE` = nhóm thao tác lượt group được tự duyệt (`read` an toàn nhất; `read,file,bash` = tự chạy cả lệnh ghi).
- Thử một câu không cần socket: `python $T/tchat-automation/tools/group_watch.py --once "review <link MR>"` (job vẫn vào hàng đợi thật).
- Tin nhắn 1-1: `python $T/tchat-automation/tools/listen.py --reply notify|claude|off`.

**Token TChat:**
```bash
python $T/tchat-automation/tools/auth.py token-status    # còn bao lâu (access_seconds_left âm = hết hạn)
python $T/tchat-automation/tools/auth.py refresh         # gia hạn bằng refresh token (tự ghi lại .env)
python $T/tchat-automation/tools/auth.py whoami          # phải ra user, không phải 401
```
Refresh cũng 401 → đăng nhập TChat web, DevTools → Network → copy `Authorization: Bearer …` vào `FCHAT_BEARER_TOKEN`
(và `FCHAT_REFRESH_TOKEN` nếu có), rồi restart tab group_watch. Bot gửi tin dưới tên chủ token.

### 4.10 Kubernetes: đọc log, xem pod

`k8s.py` **chỉ đọc** (restart/scale/patch/exec bị chặn trong tool). Dùng `kubectl` có sẵn trong Lens.
```bash
python $T/dev-automation/tools/k8s.py doctor | contexts           # kubectl + context trên máy
python $T/dev-automation/tools/k8s.py access --project etask --env dev   # token còn hạn không, được làm gì
python $T/dev-automation/tools/k8s.py pods|deploys|events --project etask --env dev
python $T/dev-automation/tools/k8s.py logs --project etask --env dev deploy/<app> --since 30m --errors -C 3
python $T/dev-automation/tools/k8s.py logs ... deploy/<app> --grep "Exception" --previous    # log trước lần crash
```
Không có `--project` thì ghi rõ: `--context "<ctx>" [--kubeconfig <file>] -n <namespace>`.
`access` báo `Unauthorized` = token trong kubeconfig hết hạn → lấy kubeconfig/token mới (Rancher "Download KubeConfig",
OpenShift `oc login`) rồi ghi vào `~/.kube/config`.

Khai cho project (`work/projects.json`) để chỉ cần `--project P --env E`:
```json
"environments": { "dev": {
  "k8s": { "kubeconfig": "~/.kube/config", "context": "<tên context>", "namespace": "<ns>" },
  "forwards": [ { "name": "etask-db", "target": "svc/<db-svc>", "ports": "13310:3306" } ]
}}
```

**Muốn sửa gì trên cluster** (restart, scale, apply…): nói với Claude Code trong một phiên bạn đang ngồi, vd
"restart deploy etask-api ở uat". Claude đọc trạng thái, cho bạn xem lệnh `kubectl … --context … -n …`, hook
**hỏi bạn duyệt** từng lệnh ghi, rồi kiểm lại. Agent chạy nền không bao giờ ghi được lên cluster. PROD: cẩn thận gấp đôi.

### 4.11 Truy cập DB qua port-forward

```bash
python $T/dev-automation/tools/k8s.py forwards [--listening]       # forward đã lưu trong Lens, cái nào đang mở
python $T/dev-automation/tools/podfwd.py start svc/<db-svc> 13310:3306 --project etask --env dev --name etask-db
#   hoặc ghi rõ: --context "<ctx>" --kubeconfig "<file>" -n <ns>      · đích: pod/x | svc/x | deploy/x | "-l app=db"
python $T/dev-automation/tools/podfwd.py start-saved --project etask --env dev   # mở mọi forward đã khai
python $T/dev-automation/tools/podfwd.py list | logs <name> | stop <name|all>
```
Forward chạy nền, tự nối lại, chỉ mở trên `127.0.0.1`. Chọn cổng local khác 3306 để không đụng MySQL trên máy.
PROD cần `--allow-prod` (hook hỏi). Cluster nội bộ: bật VPN trước (`python $T/dev-automation/tools/vpn.py status` exit 0 = OK).

Query (user/pass qua biến môi trường, không ghi trên dòng lệnh):
```powershell
$env:DB_USER="..."; $env:DB_PASSWORD="..."
python $T/dev-automation/tools/probe_db.py query --engine mysql --host 127.0.0.1 --port 13310 --name etask `
  --sql "select count(*) from task" --max-rows 5
python $T/dev-automation/tools/dbeaver.py probe "Folder/Tên kết nối" --sql "select 1"   # dùng kết nối DBeaver đã lưu
```
`probe_db`: MySQL (`mysql_native_password`) / PostgreSQL, không TLS — DB bắt buộc TLS thì dùng DBeaver. PROD chỉ đọc.

## 5. Gỡ rối

| Hiện tượng | Nguyên nhân thường gặp | Làm gì |
|---|---|---|
| Việc nằm `queued` mãi | không có worker | mở tab Coordinator (§3.4) |
| Review `blocked` ở bước approval | đang chờ bạn duyệt | bấm Duyệt trên Telegram / `review_post.py approve <job>` |
| Bấm Duyệt rồi mà vẫn chờ | bot Telegram đang chạy từ **repo khác** (thư mục duyệt khác) hoặc bot OPS không chạy | tắt bot cũ, mở lại tab TG bot OPS từ repo này; kiểm dòng `Hub repo` rồi bấm lại |
| Thẻ duyệt hết hạn | không bấm trong `TELEGRAM_APPROVAL_TIMEOUT` | `review_post.py request-approval <job>` gửi lại |
| Review không tự đăng dù đã `auto` | trượt luật | `review_rules.py check <job>` xem lý do |
| Review đã đăng nhưng group không được báo | gửi TChat lỗi / không xác nhận | dashboard "Cần bạn xử lý" ghi lý do; lỗi rõ ràng `sweep` tự thử lại; "uncertain/unconfirmed" kiểm group rồi `review_post.py notify <job>` |
| `WinError 10060` / timeout khi gọi GitLab | mất VPN | bật FortiClient, `vpn.py status`; xếp lại MR (nhắn lại hoặc `review_flow.py enqueue`) |
| group_watch không nhận tin | token TChat hết hạn / người gửi không trong allowlist / thiếu từ khoá | §4.9; xem log tab group_watch (`[DENY]`, `[skip]`) |
| `k8s.py access` → Unauthorized | token kubeconfig hết hạn | lấy kubeconfig mới (§4.10) |
| Tài khoản Claude "token hết hạn" | access token chưa được Orca gia hạn | mở tài khoản đó trong Orca |

Task `blocked` vì lý do khác:
1. `coordinator.py board` → đọc cột lý do.
2. Cổng duyệt → chạy lệnh duyệt tương ứng (`review_post.py approve`, `program approve`, `proposals.py decide`).
3. Agent không tự sửa được → sửa tay, rồi `python $T/dev-automation/tools/taskstore.py unblock <id>`.
4. Không muốn làm nữa → `taskstore.py cancel <id>`.
Chi tiết một task: `taskstore.py show <id>` · lịch sử: `taskstore.py events <id>`.

## 6. Thử an toàn trước khi dùng thật

```bash
python -m unittest discover -s tests          # toàn bộ test tự động, không đụng hệ thống thật
```
Muốn thử tay mà không lẫn vào hàng đợi thật, dùng store riêng:
```bash
export TOOLKIT_STORE=/tmp/try/toolkit.db TOOLKIT_STATE_DIR=/tmp/try    # PowerShell: $env:TOOLKIT_STORE="..."
```
Thứ tự gợi ý: (1) `board`, `proposals.py scan`, `refactor.py survey`, `k8s.py access` — chỉ đọc → (2) `verify.py plan/start` trên một nhánh nhỏ → (3) review 1 MR thật với `REVIEW_AUTO_POST=shadow` → (4) bật `auto` cho một project.

## 7. Lưu ý

- Lệnh **tốn token**: `work` (khi có việc cần agent), `proposals.py go`, `program` có lát dùng agent.
- Lệnh **người thật thấy**: `review_post.py sweep`, `notifier.py`, `gitlab_api.py create-mr/mr-comment`, tin bot gửi lên TChat — hook sẽ hỏi bạn (trừ review tự đăng qua luật).
- Lệnh duyệt (`approve`, `decide`) **phải gõ ở terminal thật** hoặc bấm trên Telegram — agent không làm thay được (cố ý).
- Secret chỉ nằm trong `.env` / thư mục tài khoản; đừng dán ra chat. Toolkit không gửi `.env` cho Codex/dịch vụ ngoài.
- Hạng mục còn treo trong `docs/design/CHECKLIST.md`: F.4c (nghiệm thu review MR thật — đã có một lần chạy thật trên
  etask!1202, checklist chưa ghi nhận), F.8 (deploy/env dev).
