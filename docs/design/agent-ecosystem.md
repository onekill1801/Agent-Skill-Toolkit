# Kiến trúc tổng quan — Agent-Skill-Toolkit

> Trạng thái: **bản thiết kế v1.1 (đề xuất)** · Cập nhật: 2026-10-12 · Chủ: hangntt41
>
> **Phạm vi:** kiến trúc của **chính repo này** (toolkit dùng chung). Các repo sản phẩm (`work/<name>/`,
> ví dụ `work/etask`) là **đối tượng** toolkit làm việc trên đó — mọi nhắc tới etask trong tài liệu chỉ là
> **ví dụ minh hoạ**, không phải phạm vi thiết kế. Cấu hình riêng từng repo sản phẩm nằm ở
> `<repo>/.agent/` + `work/projects.json`, không nằm trong toolkit.
>
> Đọc theo thứ tự: **§A hiện trạng repo → §B kiến trúc repo đích** (bố cục, luật import, hợp đồng tương
> thích, 2 track) → §0–§17 hệ thống chạy thế nào → §18 bổ sung sau review → Phụ lục C debate kiến trúc repo.
> Nguồn: các phiên thảo luận + 2 vòng debate Claude ↔ Codex + review nhánh `feat/mr-review-orca-tooling`
> + đọc code Orca (`E:\Agent-Tool\orca`, v1.4.197) + quy trình thực tế ở `work/etask`.
>
> Quy ước nhãn: **[Inference]** = suy luận chưa kiểm chứng trên hệ thống thật · **[Unverified]** = chưa xác minh được.

---

## A. Hiện trạng repo (đo trên các file đã commit, nhánh `feat/mr-review-orca-tooling`, 2026-10-12)

> Đã sửa theo debate Claude ↔ Codex (Phụ lục C). Số đo trên working tree trước đó lệch vì gồm file chưa commit.

| Skill | Vai trò hiện nay |
|---|---|
| `dev-automation` (31 tool, ~8.500 dòng) | GitLab, Azure, Jenkins, k8s/Lens, probe API/DB/Redis/Kafka, test_runner, run_log, notifier, daemon_common, status, doctor, orca_adapter… |
| `auto-dev` | pipeline Plan→Implement→Test→Deliver, clarify, triage, debate, fix_loop, verify_gen, task_queue (`agent_init` ở nhánh `feat/agent-init`) |
| `tchat-automation` | client TChat, listener, group_watch, digest, reply |
| `atask-automation` | aTask CRUD/search/analytics, atask_watch, task_resolver |
| `remote-control` | Telegram bridge, approvals, SSH |
| `mr-review` | hàng đợi review MR + worktree + đăng |
| `new-product`, `fork-terminal`, `team-registry`, `skill-scaffold` | tiện ích tương đối độc lập |

Tổng ≈ 24.750 dòng Python stdlib · 25 slash command · 173 test method (đếm được ≠ chứng minh suite xanh).

**Vấn đề kiến trúc của repo:**

1. **Import mong manh:** 49 lệnh `sys.path.insert(...)`; tên module trùng giữa các skill (`config`, `client`,
   `auth`) → thứ tự `sys.path` và cache `sys.modules` quyết định import đúng/sai. `agentkit.py` đặt thư mục
   tools của skill sở hữu lên đầu nhưng **không** khử được (tool tự chèn thêm thư mục khác sau đó).
2. **Phụ thuộc chéo — đã đo (AST, 2026-10-12, §19):** chỉ **1 vòng import thật**: `dev-automation ↔ remote-control`
   (`bg_notify → tg_api`, `telegram_bridge → daemon_common`). "4 vòng" trước đây là sai (đếm theo tên skill).
   Phụ thuộc thật nhiều nhất vào: `dev-automation` (`daemon_common`, `run_log`, `gitlab_api`, `orca_adapter`,
   `project_config`), `remote-control` (`tg_api`, `approvals`, `rc_config`), `fork-terminal` (`agent_parser`,
   `fork_terminal`). **4 tool import tên mơ hồ `config`** của skill khác (`auto-dev/agent_runner`,
   `auto-dev/debate_engine`, `mr-review/review_common`, `new-product/repo_init`) — nạp `config` nào tuỳ `sys.path`.
3. **Cấu hình lặp:** 3 `config.py` + `remote-control/rc_config.py` tự parse `.env`; nơi tìm `.env` khác nhau
   (cwd vs gốc repo); mỗi skill có helper riêng (`pat_token()`, `dotenv_path()`, GitLab/Azure helper) — gom
   phần parse/UTF-8/tìm gốc, **giữ nguyên** API riêng.
4. **`dev-automation` là "túi đồ":** adapter + hạ tầng chung + daemon trong một skill.
5. **Điều phối có nhưng rời rạc:** `group_watch` đã xếp review vào `review_queue`; watcher dùng chung
   `daemon_common`; `task_queue` có claim độc quyền theo luồng; mr-review có giới hạn song song — nhưng
   **chưa có quota/điều phối chung**, và yêu cầu build trong group vẫn gọi agent trực tiếp.
6. **Trạng thái bằng file JSON rải rác** (`temp/runs/`, `temp/review_queue/`, `temp/tg_approvals/`) — xem Phụ lục A.
7. **Tài liệu kiến trúc nhiều nơi:** `README §Kiến trúc`, `CLAUDE.md`, `IMPROVEMENT_PLAN.md`, `AGENT_AUDIT.md`,
   các `SKILL.md`; ngoài ra có bản sao `.agents/` + `AGENTS.md` chưa commit trên máy.

---

## B. Kiến trúc repo đích (đã chốt sau debate)

### B.1 Bố cục — một package có namespace, skill chỉ là vỏ

```text
agentkit.py                              # lối vào chung (không quét _runtime)
.claude-plugin/plugin.json               # giữ nguyên: "skills": "./.claude/skills"
.codex-plugin/plugin.json
.claude/
  commands/                              # giữ nguyên
  hooks/guard_write.py                   # lớp phụ
  skills/
    _runtime/                            # KHÔNG có SKILL.md — không phải skill
      bootstrap.py
      agent_skill_toolkit/
        core/                            # parse env/config, UTF-8, đường dẫn/gốc, lock, lỗi chuẩn
        integrations/                    # client có namespace: gitlab, azure, jenkins, k8s, probes,
                                         #   atask, tchat, telegram — không gọi agent, không import lẫn nhau
        workflows/
          execution/                     # giao diện + runner Claude/Codex/Orca (agent_runner, orca_adapter)
          mr_review/                     # policy, checkpoint, store, executor (lát dọc đầu tiên)
          auto_dev/
          intake/                        # watcher: chỉ xếp hàng
    <skill-hiện-có>/                     # dev-automation, auto-dev, mr-review, atask-automation, …
      SKILL.md                           # giữ tên + frontmatter
      tools/<tool>.py                    # facade: CLI + re-export helper cũ
      cookbook/ prompts/ …               # giữ nguyên
tests/
docs/design/agent-ecosystem.md           # tài liệu kiến trúc chuẩn
```

Facade dùng chung một shim (chỉ bootstrap được sửa `sys.path`):

```python
from pathlib import Path
import runpy
runpy.run_path(str(Path(__file__).resolve().parents[2] / "_runtime" / "bootstrap.py"))["activate"]()
from agent_skill_toolkit.integrations.gitlab import cli   # import đầy đủ namespace
```

`activate()` chỉ chèn `_runtime`, kiểm `agent_skill_toolkit` đã nạp (nếu có) đúng vị trí, xung đột → lỗi rõ.
Định vị theo vị trí facade → chạy được từ mọi cwd, worktree, qua `agentkit`, và layout plugin đã cài
**nếu** trình cài giữ nguyên cây con (phải kiểm ở mốc M2).

### B.2 Luật import

| Vùng | Được import | Cấm |
|---|---|---|
| `core` | stdlib (+ phụ thuộc chung đã duyệt) | mọi thứ khác |
| `integrations` | `core` | gọi agent; import integration khác |
| `workflows` | `core`, `integrations`, `workflows/execution` | phụ thuộc workflow khác khi chưa có hợp đồng rõ |
| mọi runtime | — | import facade; import module xoay tài khoản; import tên trần (sibling) |

- Trạng thái nghiệp vụ + chuyển trạng thái thuộc **workflow**; `core` chỉ có primitive chung (không có
  "trạng thái task chuẩn" hay policy nghiệp vụ).
- Bánh cóc: test quét **AST import + đường dẫn script trong subprocess** + **danh mục cạnh runtime** khai báo
  tay (cho import động), ngoại lệ phải có lý do; danh sách vi phạm chỉ được giảm.

### B.3 Hợp đồng tương thích (checklist mỗi lần chuyển)

- [ ] Tên skill, frontmatter `SKILL.md`, slash command, 2 plugin manifest, cookbook, tài nguyên tương đối
- [ ] Tham số CLI, định dạng output JSON, exit code, helper đang được import
- [ ] Thứ tự ưu tiên cấu hình, helper riêng từng skill, biến môi trường đè, hành vi `work/`/`WORK_DIR`
- [ ] Tách rõ: đường dẫn tài nguyên toolkit · gốc repo sản phẩm · thư mục trạng thái ghi được (`temp/`)
- [ ] Chạy trực tiếp và qua `agentkit`, cwd bất kỳ, worktree, đường dẫn Windows có dấu cách, cả 2 kiểu cài plugin
- [ ] Tự đăng comment GitLab là **thay đổi hành vi có chủ đích**: cập nhật mô tả "cần người duyệt" trong
      `CLAUDE.md`/`SKILL.md` và test tương ứng

### B.4 Hai track song song

**Track R — tái cấu trúc repo**

| Mốc | Việc | Nghiệm thu |
|---|---|---|
| **M1** Hợp đồng | sửa kiểm kê; ghi đồ thị import + cạnh runtime, API công khai, baseline test Windows | kiểm kê tái lập được; lỗi sẵn có được gọi tên |
| **M2** Phân phối + bootstrap | cài thử cả 2 plugin, chạy một facade tối thiểu | `_runtime` được ship, không bị coi là skill; import tất định ở mọi layout |
| **M3** Primitive chung | tách parse env, UTF-8, đường dẫn, lock vào `core`; giữ wrapper | cấu hình + đường dẫn tương thích |
| **M4** Lát dọc MR review | chuyển `mr-review` sang `workflows/mr_review` + `integrations/gitlab` | trực tiếp = qua dispatcher; integration mock xanh; tài nguyên còn nguyên |
| **M6** Mở rộng dần | từng workflow / integration còn lại | số vi phạm giảm; hợp đồng giữ nguyên |

**Track F — tính năng (bắt đầu bằng review MR)**

| Mốc | Việc | Nghiệm thu |
|---|---|---|
| **M5** Thực thi review bền | store SQLite + checkpoint thiết kế cùng M4, phát hành độc lập: định danh task, revision MR, bước, artifact, tham chiếu provider/session, giờ retry, lease, ý định/kết quả đăng; executor + luật tự đăng (§10.2.1) | dừng khi hết quota rồi chạy lại; lease hết hạn được thu hồi; MR đổi revision; claim đồng thời; crash trước/sau khi đăng được đối soát (marker review ổn định) |

"Chạy tiếp đúng bước" = **checkpoint bền gần nhất**; lượt gọi model bị cắt giữa chừng thì chạy lại bước
đó từ input đã lưu (trừ khi provider hỗ trợ tiếp tục phiên). Transaction DB **không** bảo đảm GitLab nhận
đúng một lần → luôn kiểm lại revision và đối soát bằng marker trước khi thử lại.

Các bước §14 (làm rõ, thang kiểm, cổng vào, …) đi theo Track F sau M5, mỗi workflow mới viết thẳng
trong `workflows/` theo luật B.2.

### B.5 Cắt bỏ (đã thống nhất)

5 lớp vật lý · bung nhiều thư mục skill mới · chuyển SQLite cho mọi thứ · viết lại mọi daemon một lượt ·
mục tiêu "0 vòng" ngay · đặt runtime ở gốc repo (plugin có thể không ship) · tích hợp module xoay tài khoản ·
thay `.agents/` bằng con trỏ khi chưa kiểm cách Codex đọc.

Module xoay tài khoản của người dùng: để **ngoài toolkit phân phối** (tốt nhất repo riêng tư); không import,
không discovery, không đường bootstrap nào chạm tới; workflow chỉ dùng giao diện provider đã cấu hình.

---

## 0. Mục tiêu

Một người (về sau có thể là cả đội) **giao việc bằng lời** → hệ thống **làm rõ, chia nhỏ, giao cho đội
agent**, tự **build – kiểm – sửa** tới khi đạt, người dùng **xem tiến độ trực quan** và chỉ chạm vào ở
vài điểm quyết định.

Bốn yêu cầu cốt lõi (theo thứ tự ưu tiên kiến trúc):

| # | Yêu cầu | Đo bằng |
|---|---|---|
| R1 | **Ổn định** khi hết limit / đổi tài khoản / đổi agent / tắt máy — dừng rồi chạy tiếp không mất việc | % task chạy tiếp đúng chỗ sau gián đoạn |
| R2 | **Chất lượng cao** — vòng thử–sai có số đo khách quan (như `work/etask`) | tỉ lệ MR merge không phải sửa; lỗi lọt lên dev/prod |
| R3 | **Tiết kiệm token** | token / task, theo bước |
| R4 | **Giao việc + xem tiến độ** cho nhiều loại việc chạy song song | số phút người dùng phải tham gia / task |

Loại việc cần hỗ trợ: tính năng, bug, task nhỏ, **refactor lớn**, **cải tiến kiến trúc / đề xuất nâng
cấp**, **review MR** (kể cả khi được nhờ qua **TChat**), **điều tra lỗi**, **tạo/cập nhật task aTask**,
**trả lời TChat**, và **đề xuất chủ động** (hệ thống tự tìm việc).

---

## 1. Nguyên tắc thiết kế

1. **Trạng thái nằm ngoài phiên agent.** Tiến độ = git branch (commit WIP) + `handoff.md` + sổ ghi. Agent
   nào cũng tiếp tục được, không cần lịch sử chat.
2. **Bằng chứng do tool đo, không do agent tự khai.** Coordinator tự chạy build/test/đo trên đúng SHA.
3. **Agent không bao giờ tự ghi ra ngoài.** Mọi thao tác người khác thấy (comment MR, gửi TChat, cập nhật
   aTask, tạo MR, chạm prod) là **đề xuất**, chỉ **Người gác** thực thi sau khi người duyệt.
4. **Quyền hạn ở phía server là lớp chắc nhất.** Token chỉ đọc cho worker; regex/hook chỉ là lớp phụ.
5. **Tool làm thay LLM bất cứ khi nào được** (test, lint, grep, refactor cơ học, đo) → rẻ và chính xác.
6. **Co giãn theo repo và theo cỡ việc** — không ép mọi việc qua cùng một quy trình nặng.
7. **Không tự xây cái đã có:** Orca lo chạy agent + giao diện; toolkit lo nghiệp vụ + kiểm chứng + quyền.
8. **Mỗi bước triển khai phải dùng được ngay** — không xây framework rồi mới có giá trị.

---

## 2. Kiến trúc tổng thể

```
 ┌──────────────────────────── CỔNG VÀO ────────────────────────────┐
 │ Bạn (Orca/Telegram) · TChat group · GitLab MR gán bạn · aTask    │
 │ task mới · Lịch quét chủ động                                    │
 └──────────────────────────────┬───────────────────────────────────┘
                                ▼
                 HÀNG ĐỢI CHUNG  (sổ ghi — SQLite)
                                ▼
          LÀM RÕ → TRIAGE (loại việc × làn) → KẾ HOẠCH
                                ▼                        🧑 duyệt (khi cần)
 ┌──────────────────────── ĐIỀU PHỐI (coordinator tất định) ────────┐
 │ chọn việc sẵn sàng · giới hạn song song · sở hữu tài nguyên ·    │
 │ dừng khi hết quota / chạy tiếp · retry · đối soát                │
 └──────────────┬──────────────────────────────┬────────────────────┘
                ▼                              ▼
        WORKER (Claude/Codex trong Orca, mỗi việc 1 worktree)
                ▼
        THANG KIỂM (build → test → review chéo → deploy dev → e2e → đo)
                │ đỏ → tóm lỗi → worker sửa → kiểm lại từ đúng bậc đỏ
                ▼ xanh
        KẾT QUẢ: MR / báo cáo / bản nháp + bảng AC → bằng chứng
                ▼
        NGƯỜI GÁC (executor tách biệt) ── 🧑 duyệt qua Telegram ──► GitLab/TChat/aTask
                ▼
        BẢNG TIẾN ĐỘ (Orca / aTask) · Telegram (tin khẩn + tổng hợp)
```

| Khối | Trách nhiệm | Nền tảng |
|---|---|---|
| Cổng vào | nhận việc từ mọi nguồn, gắn độ tin cậy + quyền | daemon toolkit (`group_watch`, `mr_watch`, `atask_watch`, cron) |
| Sổ ghi | trạng thái bền: request, task, phụ thuộc, sự kiện, đề xuất, usage | SQLite (stdlib) |
| Làm rõ + triage | AC, câu hỏi chặn, giả định, chọn làn | `context_pack`, `clarify`, `triage` |
| Điều phối | lịch chạy, song song, quota, đối soát | `coordinator.py` (mới) + `orca orchestration` |
| Worker | làm việc trong worktree riêng | Orca `worker-start --agent claude\|codex` |
| Thang kiểm | bằng chứng khách quan | `verify_ladder.py` (mới) + tool sẵn có |
| Người gác | quyền ghi duy nhất, duyệt gắn hash | `executor` (mới, tài khoản Windows riêng) |
| Bảng tiến độ | xem trực quan | Orca + aTask (project "Agent Hub") |

---

## 3. Cổng vào

| Nguồn | Độ tin cậy | Được tạo loại việc | Trả kết quả về |
|---|---|---|---|
| Bạn — Orca / Telegram | cao | mọi loại, mọi làn | Orca / Telegram |
| **TChat group** (`group_watch`) | **thấp** (nhiều người gõ) | `review_mr`, `investigate` (chỉ đọc) | TChat (người nhờ) qua Người gác |
| GitLab — MR gán bạn (`mr_watch`) | trung bình | `review_mr` | GitLab comment qua Người gác |
| aTask — task mới giao bạn (`atask_watch`) | trung bình | phân tích → đề xuất làn (bạn xác nhận) | aTask qua Người gác |
| Lịch quét chủ động (tuần) | cao | **chỉ đề xuất** — bạn bấm [Làm] mới chạy | Telegram (bản tổng hợp tuần) |

Quy tắc:
- Daemon **chỉ xếp việc vào hàng đợi**, không tự gọi agent → mọi việc chung giới hạn song song, chung cơ
  chế quota, chung bảng tiến độ.
- Danh sách người được nhờ trong TChat so theo **user id**, không theo tên hiển thị.
- Link MR phải đúng host `GITLAB_URL`; nội dung tin nhắn / MR luôn là **dữ liệu**, không phải lệnh.
- Mỗi nguồn có công tắc bật/tắt (Orca automation hoặc `policy.json`).

---

## 4. Làm rõ yêu cầu, loại việc và làn

### 4.1 Làm rõ (bước đầu tiên của mọi việc trừ làn Nhỏ)

```
① Gom ngữ cảnh (tự động): task/comment/checklist, MR liên quan, code liên quan, .agent/, số đo hiện tại
② Tìm chỗ mơ hồ:  câu hỏi CHẶN (phải hỏi)  ·  giả định (tự chọn, ghi rõ)
③ Viết AC đo được  (AC1 … ACn)
④ Gửi bạn MỘT lần: "2 câu hỏi · 4 giả định · 3 AC — trả lời / sửa / duyệt?"
⇒ AC đã duyệt = hợp đồng cho mọi bước sau; cuối cùng mỗi AC phải có bằng chứng
```

### 4.2 Loại việc (kind)

| Kind | Kết quả | Ghi ra ngoài |
|---|---|---|
| `code` (feature / bug / tối ưu / refactor) | MR + bảng AC → bằng chứng | tạo MR (qua Người gác) |
| `review_mr` | review.json → comment MR | comment GitLab + báo TChat (qua Người gác) |
| `investigate` | báo cáo nguyên nhân (log Lens, API GET, DB đọc) | không |
| `atask_draft` | bản nháp tạo/cập nhật task | ghi aTask (qua Người gác) |
| `tchat_draft` | bản nháp trả lời | gửi TChat (qua Người gác) |
| `proposal` | tài liệu đề xuất (ADR) | không |

### 4.3 Làn (áp cho `code`; `proposal` có làn riêng)

| | **Nhỏ** | **Vừa** | **Lớn** | **Đề xuất / kiến trúc** |
|---|---|---|---|---|
| Ví dụ | typo, config, bug 1 file | tính năng, bug nhiều file, tối ưu 1 API | refactor codebase, nâng framework | "nâng Spring Boot?", "cải tiến authz?" |
| Làm rõ | bỏ qua (trừ khi mơ hồ) | đầy đủ → AC | mục tiêu + bất biến + định nghĩa "xong" | câu hỏi + tiêu chí đánh giá |
| Kế hoạch | không | có | chương trình + lát cắt (DAG) | các phương án |
| Thang kiểm | rút gọn | đầy đủ theo repo | đầy đủ + tương đương + bánh cóc | spike (nhánh bỏ đi) |
| Review | model rẻ | Codex tới GO | Codex tới GO mỗi lát | debate Claude ↔ Codex |
| Model | Haiku/Sonnet | Sonnet + Opus review | như Vừa / lát | Opus |
| Bạn chạm | merge | duyệt AC + merge | duyệt kế hoạch + merge lát + báo cáo đợt | quyết định |

**Tự nâng làn** (không tự hạ) khi: diff > 150 dòng, > 5 file, một bậc đỏ 2 lần, hoặc phát sinh câu hỏi
chặn mới. Bạn đổi làn được trước khi chạy; mỗi lần đổi ghi vào `feedback.py` để triage học.

**Gom việc nhỏ:** nhiều việc nhỏ cùng repo chạy tuần tự trong một worker (đỡ nạp ngữ cảnh lặp lại).

---

## 5. Điều phối và thực thi

### 5.1 Sổ ghi (SQLite, WAL, transaction — thay các file JSON rời)

```sql
requests (id, source, kind, lane, goal, status, created_at)
tasks    (id, request_id, title, kind, repo, branch, status, backend, attempts,
          owner_paths, resume_at, ac_json)
deps     (task_id, depends_on)
orca_map (task_id, run_id, orca_task_id, dispatch_id, worktree)
gates    (task_id, rung, passed, sha, summary, metrics_json, ts)      -- kết quả thang kiểm
proposals(id, task_id, target, action, payload_hash, sha, expires_at, nonce, status)
approvals(proposal_id, decision, actor, channel, proof, ts)           -- chỉ Người gác ghi
usage    (task_id, step, backend, model, tokens_in, tokens_out, ts)
events   (ts, task_id, kind, data)
```

Trạng thái task: `queued → ready → running → verifying → awaiting_approval → done`
cùng `paused_quota`, `blocked`, `failed`, `cancelled`.

### 5.2 Vòng điều phối (tất định, không phải LLM)

```python
def tick():
    reconcile_unknown_outcomes()                 # timeout/mất kết nối ≠ đã chết → đối soát trước
    for t in store.due("paused_quota"):          # tới giờ reset quota
        start_worker(t, pick_backend(t), resume=True)
    for t in store.ready():                      # mọi deps done
        if running() >= MAX_PARALLEL:             break
        if owner_conflict(t):                     continue   # vùng file/tài nguyên đang bị giữ
        if not budget_ok(t):                      continue
        start_worker(t, default_backend(t))
```

Ràng buộc từ Orca (đọc từ `orca skills get orchestration` và code):
- **Orca không tự lên lịch** — coordinator phải tự khởi chạy, xử lý `worker_done`, khôi phục.
- `worker_done` tự kết thúc task trong Orca → **không** gọi `task-update completed` sau đó. Orca giữ
  vòng đời thực thi; toolkit quyết định **được giao hay không**.
- Phải **xác nhận tin trong inbox** (Orca phát lại tới khi xác nhận) và **release worker** đã xong.
- Worker review/điều tra chạy với `--setup skip` (code trong repo là đầu vào không tin cậy).

### 5.3 Worker và bàn giao

Mỗi task = 1 worktree + 1 nhánh. Playbook bắt buộc worker:
- **commit WIP** sau mỗi bước nhỏ;
- cập nhật `handoff.md` trước khi kết thúc lượt — file nằm **ngoài repo** (`temp/handoff/<task>.md`, sổ
  ghi giữ bản sao), không bao giờ commit vào nhánh (tránh lọt vào MR / lộ log, đường dẫn nội bộ);
- commit WIP được **squash** khi đề xuất tạo MR (lịch sử MR sạch, theo quy ước commit của repo);
- mỗi bước **idempotent** (kiểm "đã có MR chưa" trước khi đề xuất tạo).

```markdown
## Mục tiêu       … (AC1..ACn)
## Đã xong        … (commit, bậc kiểm đã xanh @SHA)
## Đang dở        …
## Bước tiếp      …
## Quyết định     …
## File liên quan …
```

### 5.4 Song song

- Mỗi task khai báo `owner_paths` (module/file) + tài nguyên dùng chung (DB schema, port, Jenkins job,
  môi trường dev). Coordinator chỉ chạy song song khi **không giao nhau**.
- Worktree chỉ cô lập file — port/DB/browser/dev env vẫn chung → phải qua cơ chế sở hữu.
- Bắt đầu `MAX_PARALLEL = 1`, tăng khi đã ổn định.

---

## 6. Thang kiểm (vòng thử–sai)

Engine thang kiểm là **mã chung của toolkit** (`_runtime/agent_skill_toolkit/workflows/verify/ladder.py`); **nội dung** các bậc do
**từng repo sản phẩm** khai báo trong `<repo>/.agent/project.yml`. Mô hình rút ra từ một quy trình thực tế
(ví dụ `work/etask`, đọc từ git log + roadmap [Inference]): code từng bước → test đo chỉ số → full suite
(chạy lại test flaky) → review chéo tới GO → deploy dev → e2e so baseline → bench → kiểm pod → ghi số đo + bài học.

### 6.1 Khai báo theo repo — `<repo>/.agent/project.yml` (ví dụ: repo Java/Maven như etask)

```yaml
verify:
  ladder:
    - {id: compile,    run: "./mvnw -q -DskipTests package"}
    - {id: targeted,   run: "./mvnw -q test -Dtest={changed_tests}"}
    - {id: sql_budget, run: "./mvnw -q test -Dtest='*SqlTest'",
       metric: {regex: "queries=(\\d+)", compare: baseline, must: "<="}}
    - {id: suite,      run: "./mvnw -q test", flaky: {known: [RestoreListOpenJpaTest], rerun: 3}}
    - {id: review,     kind: cross_review, reviewer: codex, until: GO, max_rounds: 6, block_on: [P1]}
    - {id: deploy_dev, kind: jenkins, job: etask-dev, wait_sha: true}            # cần policy cho phép
    - {id: health,     kind: k8s, env: dev, checks: [rollout, no_restart_10m, no_new_errors_10m]}
    - {id: e2e,        run: "npm run e2e:dev", baseline: .agent/e2e-known-failures.json}
    - {id: bench,      kind: apibench, runs: 20, warmup: 1, target: {p95_ms: 500}, same_node: true}
  budgets: {max_fix_rounds_per_rung: 3, max_tokens: 2000000}
  lanes:
    nho: [compile, targeted, lint]
```

### 6.2 Engine

```python
def run_ladder(task, ladder, start_at=0):
    for i, rung in enumerate(ladder[start_at:], start_at):
        res = RUNNERS[rung.kind](task, rung)                 # tool chạy, đo trên đúng SHA
        store.record_gate(task, rung.id, res, sha=git_head(task))
        if res.passed:
            continue
        if task.fix_rounds[rung.id] >= budgets.max_fix_rounds_per_rung:
            return escalate(task, rung, res)                 # dừng, lên bảng, hỏi bạn
        handoff.write(task, failed=rung.id, evidence=res.summary)   # tóm tắt, không log thô
        fix_agent(task, rung, res.summary)
        return run_ladder(task, ladder, start_at=i)          # chạy lại từ đúng bậc đỏ
    return deliverable(task)
```

- Kết quả từng bậc **cache theo SHA** → không chạy lại cái đã xanh trên cùng commit.
- Bậc "đắt" (deploy, e2e, bench) chỉ chạy khi các bậc rẻ đã xanh.
- **Một luật nghiệm thu duy nhất:** bậc bắt buộc đỏ ⇒ không được giao (hết mâu thuẫn "inform vs block"
  trong `auto-dev/SKILL.md`).

### 6.2.1 Đã hiện thực (F.2, Codex GO vòng 8)

`workflows/verify/ladder.py` + `auto-dev/tools/verify.py`. Luật an toàn chốt qua 8 vòng review:
spec đọc từ **base ref**; worktree **sạch + đúng nhánh** trước mỗi bậc; `pending_fix` lưu bền trước khi agent sửa
chạy (crash/hết quota vẫn được kiểm khi chạy tiếp); ref nhánh/tag khác bị đổi → dừng kèm lệnh khôi phục;
**mọi bậc chạy lệnh phải khai `protects: [...]`** (file mà lệnh thực thi; `[]` là khai báo tường minh) — không đoán
từ argv; launcher `argv[0]` luôn được bảo vệ; thay đổi test / cấu hình build-CI / `.agent` / file được khai báo →
người duyệt; so khớp không phân biệt hoa thường; `covered_by` chỉ trỏ tới bậc `full_suite: true`.

### 6.3 Co giãn theo những gì repo có

`agent_init scan` dò repo → đề xuất `verify.ladder`:

| Repo có | Bậc dùng được |
|---|---|
| chỉ build | compile + lint + review |
| unit test | + test liên quan + suite |
| API | + smoke API (`local_app` chạy local hoặc gọi dev) |
| UI | + smoke UI qua trình duyệt Orca (`tab create`, `snapshot`, `click`, `screenshot`) |
| e2e | + e2e so baseline |
| deploy dev | + deploy, health pod, bench |

### 6.4 Repo không có e2e — không dựng e2e trước

1. **Mỗi task tạo test cho chính AC của nó, giữ lại trong repo** (`verify_gen` → test thật). Bộ test lớn
   dần đúng chỗ thay đổi.
2. **Refactor:** tạo test chụp hành vi (characterization) cho vùng sẽ đụng **trước** khi sửa.
3. **Luôn báo rõ đã kiểm tới đâu:**

```
MR !512 — Export Excel báo cáo
  AC1 ✅ ReportExportIT (mới) @a1b2c3   AC2 ✅ 10k dòng 3,1 s   AC3 ✅ test 403 (mới)
  Đã kiểm: build · unit · API local · Codex GO
  CHƯA kiểm: e2e UI (repo chưa có) · chạy trên dev
  → Đề xuất: smoke UI màn Báo cáo (task riêng)  [Tạo]
```

Dựng khung e2e riêng chỉ khi nhiều task chạm UI hoặc lỗi lọt lặp lại ở cùng luồng [Inference] — khi
đó nó là một task bình thường qua đúng quy trình này.

---

## 7. Ổn định khi hết limit / đổi agent (R1)

```python
QUOTA_RX = re.compile(r"usage limit|rate.?limit|429|quota|resets? at (\S+)", re.I)

def on_worker_exit(task, output):
    if m := QUOTA_RX.search(output):
        store.update(task.id, status="paused_quota",
                     resume_at=parse_reset(m) or now() + timedelta(minutes=30))
        return                                    # không tính là lỗi, không tăng attempts
    ...
```

- Chạy tiếp = worker mới đọc `handoff.md` + `git log` + `gates` → làm từ đúng bậc. **Không phát lại chat.**
- `pick_backend(t)`: cùng backend nếu đã reset; nếu không, backend khác **bạn được phép dùng**
  (Claude ↔ Codex, API trả theo dùng). Một hợp đồng task/kết quả chung cho mọi backend.
- **Không** xoay vòng nhiều tài khoản subscription **cùng một nhà cung cấp** để né giới hạn (rủi ro điều
  khoản / bị khoá tài khoản) — kiến trúc vẫn hỗ trợ chờ reset hoặc đổi nhà cung cấp.
- Bài nghiệm thu: giết worker giữa chừng + giả lập lỗi quota → worker mới làm tiếp đúng bậc, không trùng.

### 7.1 Điều tiết theo mức dùng (toolkit làm)

| Mức dùng | Hành động tự động |
|---|---|
| ≥ 80% | tạm dừng làn Đề xuất + quét chủ động; bước code dùng Sonnet; tóm tắt/phân loại dùng Haiku |
| ≥ 95% | chỉ chạy làn Nhỏ + `review_mr`; còn lại `paused_quota` tới giờ reset |
| 100% / lỗi limit | `paused_quota` → tự chạy tiếp khi reset, hoặc chuyển nguồn dự phòng |

Nguồn dự phòng hợp lệ (cấu hình `backends.order`): Claude subscription → Codex → Claude API trả theo dùng
(có `daily_usd_cap`). Nếu có nhiều tài khoản cho **mục đích khác nhau** (công ty / cá nhân) thì định tuyến
**theo project** (`accounts_by_project`), không theo phần trăm còn lại.

### 7.2 Chuyển tài khoản — NGOÀI phạm vi toolkit (người dùng tự quản)

- Việc chuyển giữa các tài khoản cùng nhà cung cấp (bấm tay trên Orca hoặc cơ chế riêng của người dùng)
  **do người dùng tự quản, nằm ngoài toolkit**; toolkit không triển khai, không gọi, không phụ thuộc vào nó.
- Toolkit **không phân biệt** tài khoản bên dưới: mọi task chạy tiếp được nhờ trạng thái nằm ngoài phiên
  (handoff + WIP commit + sổ ghi), bất kể worker kế tiếp chạy dưới tài khoản / backend nào.
- Nên giữ cơ chế đó **tách rời** toolkit để sự cố của nó không kéo theo hệ thống.
- Hiện trạng (2026-10-12): working tree có `dev-automation/tools/claude_account_rotation.py` + cookbook +
  test (chưa commit, tạo ngoài thiết kế này). Theo luật §B.2: runtime **không import** module này;
  nếu giữ trong repo, nên đặt ngoài toolkit phân phối (repo riêng tư) — xem §B.5.
- Rủi ro (xem §16) — **đã đọc Consumer Terms của Anthropic (2026-10-12, §19):** không có điều khoản nêu
  đích danh "nhiều tài khoản" hay "lách giới hạn sử dụng". Điều khoản gần nhất: cấm "bypassing any of our
  systems or protective measures" (§3), cấm chia sẻ/cho người khác dùng tài khoản (§2), và Anthropic có
  quyền "suspend or terminate … at any time without notice" (§12). Việc xoay tài khoản có thuộc "bypassing
  … protective measures" hay không là **diễn giải, chưa có kết luận**; Acceptable Use Policy chưa đọc.
  Rủi ro thực tế còn lại: tài khoản bị tạm khoá/chấm dứt theo §12.

---

## 8. Tiết kiệm token (R3)

| Kỹ thuật | Cách làm |
|---|---|
| Tool làm thay LLM | test/lint/build/grep/đo do tool; agent chỉ đọc `res.summary` |
| Refactor cơ học bằng tool | OpenRewrite / refactor IDE / script cho đổi tên, chuyển package, đổi API theo mẫu |
| Worker ngắn | mỗi task một worker mới, chỉ nạp `handoff.md` + file liên quan |
| Ngữ cảnh chọn lọc | `.agent/` + `grounding` (chỉ file liên quan) |
| Model theo bước | Opus: plan/review cuối/đề xuất · Sonnet: code · Haiku: tóm log, phân loại, viết handoff |
| Phần cố định đầu prompt | playbook + `.agent/` đứng đầu, không đổi → tận dụng prompt caching nếu backend hỗ trợ [Unverified với Orca] |
| Cache theo SHA | không review/test lại cùng commit |
| Bậc rẻ trước | H2 đếm SQL bắt lỗi trước khi phải deploy |
| Ngân sách cứng | trần token + số vòng sửa / task; vượt → dừng hỏi |
| Đo | `usage` theo task/bước → tối ưu đúng chỗ tốn |
| **Codegraph (mặc định)** | mọi tính năng lấy ngữ cảnh code qua `code_context.py` (xem §8.1) |

### 8.1 Codegraph — lựa chọn mặc định, grep dự phòng

`dev-automation/tools/code_context.py` (mới): `callers` · `impact` · `locate` · `deps`.
Thứ tự: **codegraph** (đã index + SHA của index khớp) → **ctags/grep** (luôn chạy được).

| Dùng ở | Mục đích |
|---|---|
| làm rõ / `grounding` | module + file liên quan, nạp ít file hơn |
| review MR | người gọi của hàm/DTO/endpoint bị đổi |
| `fix_loop` | stack trace → symbol → file |
| refactor survey / đề xuất | bản đồ phụ thuộc, điểm nóng, độ ghép nối |

Ràng buộc: index tăng dần sau mỗi commit WIP, cũ ⇒ dùng grep; lời gọi HTTP/Kafka giữa repo không có
trong graph (hợp đồng là nguồn sự thật); worker Claude **và** Codex đều dùng được (ưu tiên công cụ có CLI,
không chỉ MCP); chạy **local** (code công ty không rời máy); hỗ trợ Java + Windows.
**Đã chọn: CodeGraph (`colbymchenry/codegraph`, npm `@colbymchenry/codegraph`, MIT).** Theo README
[Unverified — chưa chạy thử]: hỗ trợ Java, Windows/macOS/Linux, SQLite cục bộ `.codegraph/codegraph.db`,
file watcher + sync tăng dần, CLI đầy đủ (`init`, `sync`, `status`, `query`, `explore`, `callers`,
`callees`, `impact`, `affected`), MCP (`serve --mcp`), tự cấu hình cho Claude Code và Codex.

Cách dùng trong toolkit:
- `code_context.py` gọi **CLI** (`codegraph callers|impact|affected|explore ... `) → tool stdlib dùng được,
  không phụ thuộc MCP; worker Claude/Codex dùng thêm MCP `codegraph_explore`.
- Trước mỗi truy vấn: `codegraph status` — index cũ / đang khoá ⇒ `sync` hoặc rơi về grep.
- Worktree — **đã thử (§19):** worktree **không** tự thấy index của clone chính ("Not initialized"). Cách chạy
  được: **copy `.codegraph/` từ clone chính sang worktree rồi `codegraph sync`** (repo toolkit: sync ~4 s).
  Repo lớn (index ~500 MB) thì chi phí copy cần đo; phương án khác: truy vấn index clone chính bằng `-p <clone>`
  (cũ với file đã sửa → bù bằng grep).
- **Đã hiện thực (commit `166bcf7`, `dev-automation/tools/cg.py`)**: seed index theo project
  (`work/cache/codegraph-seeds/<id>/`) → copy vào worktree → `sync`; tác giả đo trên etask: cùng commit 0,6 s,
  lệch 395 file ≈ 70 s. `review_ws.prepare` sinh `<worktree>.codegraph.md` (method bị sửa, nơi gọi ngoài diff,
  test bị ảnh hưởng). Vá 2026-10-13: trên Windows gọi thẳng `node` + script (shim `.cmd` làm crash và có nguy cơ
  chèn lệnh qua tiêu đề MR), tắt telemetry trong mọi lời gọi, bỏ option `-c` không tồn tại, tự exclude `.codegraph/`.
  Còn mở: `save_seed` copy cả index sau **mỗi** lần prepare (repo lớn ~500 MB/lần).
- Chọn test: **`codegraph affected <file>`** có sẵn — đã thử: sửa `review_common.py` → trả `tests/test_mr_review.py`.
- Loại thư mục bản sao (vd `.agents/`) khỏi index để không trùng kết quả.
- **Tắt telemetry mặc định** (`CODEGRAPH_TELEMETRY=0` / `DO_NOT_TRACK=1`) — README nói không gửi code, nhưng code công ty.
- Thêm `.codegraph/` vào `.gitignore` của repo sản phẩm (`agent_init apply` làm).
- `codegraph install` sửa cấu hình agent **toàn máy** (`~/.claude.json`, Codex) → xem trước, cân nhắc
  cấu hình theo project.
- Benchmark của tác giả (7 repo, Opus 4.8, 4 lượt/nhánh): −62% token, −88% tool call — **tự báo**; README
  cũng nêu đánh đổi: ngữ cảnh truy xuất tồn lại nhiều hơn trong phiên dài → hợp với thiết kế worker ngắn.
- Nghiệm thu: A/B trên **một repo sản phẩm mẫu** cho mỗi stack chính (vd Java/Maven: etask) và trên **chính
  repo toolkit** (Python) — cùng task, có / không CodeGraph; so token, số file đọc, kết quả thang kiểm.
- Toolkit chỉ cung cấp `code_context`; việc `init`/`sync` index là theo từng repo sản phẩm (`agent_init`
  thêm `.codegraph/` vào ignore), index nằm ở repo đó, không nằm trong toolkit.

---

## 9. Chất lượng (R2)

- Bằng chứng do coordinator đo trên SHA (không tin agent báo "xong").
- **Review chéo khác model** (Claude viết → Codex review và ngược lại), lặp tới GO, chặn P1.
- AC rõ trước khi code; bảng AC → bằng chứng cuối cùng.
- Task nhỏ, diff review được; refactor có baseline + kiểm tương đương.
- Học từ can thiệp người (`feedback.py recall` vào prompt lần sau).
- Kiến thức repo trong `.agent/` (architecture, conventions, operations, learned) cập nhật sau mỗi việc.

---

## 10. An toàn và quyền hạn

### 10.1 Bảng quyền theo môi trường — `work/policy.json`

```json
{
  "defaults": {
    "dev":  {"gitlab": ["read","branch"], "jenkins": ["read","build"], "k8s": ["read","logs"],
             "api": ["get","write"], "db": ["read"]},
    "prod": {"gitlab": ["read"], "jenkins": ["read"], "k8s": ["logs"], "api": ["get"], "db": []}
  },
  "toggles": {"prod.k8s.logs": {"on": false}, "dev.deploy": {"on": true}}
}
```

- Prod = bảng **và** công tắc đang bật, có TTL; mặc định tắt hết. Bật công tắc chỉ qua Người gác + Telegram.
- Dev **không** phải toàn quyền: Jenkins/DB/thông báo dùng chung → chỉ tự động sửa local + test cô lập;
  hành động môi trường dùng chung phải khai báo rõ. Sửa hạ tầng dev (affinity, resources, …) = hành động riêng cần duyệt.
- `policy.require()` gọi trong **từng tool** (để Codex/Cursor cũng tuân) — nhưng chỉ là lớp phụ: agent có
  shell sửa được file → ranh giới thật là 10.2.

### 10.2 Người gác (executor) — ranh giới thật

- Chạy dưới **tài khoản dịch vụ Windows riêng**; ACL chặn worker đọc token/cấu hình/trạng thái duyệt.
  Điều kiện: Orca + worker chạy user thường, **không admin** (nếu không → executor ở máy khác).
- Worker chỉ có **token chỉ đọc** (`TOOLKIT_ENV_FILE=.env.worker`); token ghi + bot Telegram chỉ executor giữ.
- Duyệt qua **Telegram trên điện thoại**, chỉ chấp nhận tài khoản của bạn. TTY/gate Orca **không** phải
  bằng chứng có người (Orca tạo được terminal, `orca computer` bấm được UI).
- Phê duyệt gắn **đích + hành động + hash nội dung + SHA + hạn + dùng 1 lần**; sửa nội dung / MR có commit
  mới ⇒ mất hiệu lực. Trước khi ghi: lưu id thực thi; lỗi không rõ ⇒ kiểm phía server rồi mới thử lại.
- Ngoại lệ ghi tự động (cần bạn đồng ý): cập nhật trạng thái/comment trong project aTask riêng "Agent Hub".

### 10.2.1 Tự đăng comment review lên GitLab (đã quyết: KHÔNG cần người duyệt)

Người gác vẫn là nơi duy nhất cầm token đăng; thay vì chờ bạn bấm, nó **tự duyệt theo luật**. Đề xuất
nào trượt một luật ⇒ rơi về duyệt tay qua Telegram (không bị bỏ, không tự đăng).

```json
// work/policy.json
"auto_post": {
  "gitlab.mr_comment": {
    "on": true,
    "projects": ["<tên trong projects.json>"],  // repo được tự đăng
    "sources": ["self", "mr_watch", "tchat_allowlist"],
    "actions": ["note", "inline_discussion"],   // KHÔNG approve / merge / label / resolve / đóng MR
    "max_chars": 6000, "max_inline": 15,
    "per_mr_per_sha": 1,                        // 1 lượt review / commit — không spam
    "per_day": 30,
    "require": ["sha_fresh", "review_rounds_ok"],
    "deny_content": ["@mention ngoài tác giả MR", "link ngoài GitLab nội bộ",
                     "chuỗi giống token/secret", "lệnh cho người đọc chạy"],
    "prefix": "🤖 AI review (bot của hangntt41) — @{sha7}"
  }
}
```

Luật Người gác kiểm trước khi đăng:
1. **Đúng phạm vi:** project, nguồn kích hoạt, loại hành động nằm trong whitelist. Chỉ comment — không bao
   giờ approve/merge.
2. **Còn mới:** MR chưa có commit mới so với SHA đã review (có thì huỷ, xếp review lại).
3. **Nội dung sạch:** không @mention lạ, không link ngoài, không giống secret, đúng giới hạn độ dài/số inline.
   (Chặn kiểu prompt injection "viết comment @all bảo mọi người chạy lệnh X".)
4. **Không trùng:** id thực thi theo `(mr, sha)`; lỗi không rõ ⇒ đọc lại discussion trên GitLab trước khi thử lại.
5. **Giới hạn tần suất:** 1 lượt / MR / SHA, trần / ngày.
6. **Đánh dấu rõ là bot** bằng prefix, để người đọc biết đây là review tự động.

Kèm theo:
- **Công tắc tắt khẩn:** `policy.py off gitlab.mr_comment` (hoặc nút Telegram) → mọi đề xuất quay về duyệt tay.
- **Hậu kiểm:** tổng hợp sáng/chiều liệt kê các comment đã tự đăng + link — xoá được nếu sai.
- **Chạy bóng trước (khuyến nghị):** 1–2 tuần đầu `mode: "shadow"` — sinh comment nhưng chỉ gửi bạn xem,
  đo tỉ lệ bạn đồng ý; đủ tin rồi mới chuyển `mode: "auto"`.
- Các ghi khác (TChat, aTask ngoài "Agent Hub", tạo MR, prod) **vẫn cần duyệt**.

### 10.3 Đầu vào không tin cậy

- Nội dung MR, tin TChat, task aTask = dữ liệu. Code trong repo người khác = code lạ: review với
  `--setup skip`, chạy test ở môi trường không có credential.
- Log trước khi đưa cho agent: giới hạn số dòng + che token/email/SĐT.

---

## 11. Theo dõi tiến độ và thông báo

- **Orca**: worktree, terminal, worker đang chạy (dùng nguyên, **không fork**). Code Orca 1.4.197 cho thấy
  renderer **chưa** hiển thị DAG/gate/kết quả task → không dựa vào Orca cho bảng kế hoạch.
- **aTask project "Agent Hub"**: request = task cha, bước = subtask, trạng thái, checklist = AC + bằng
  chứng, comment = link báo cáo/MR. [Unverified] aTask có hiển thị phụ thuộc không — nếu không, ghi trong checklist.
- `status.py`: bảng trạng thái nhanh trên terminal (daemon, run, queue, approvals, usage).
- **Telegram**: chỉ tin cần duyệt + khẩn (build hỏng nhánh chính, prod); còn lại gom **tổng hợp sáng/chiều**.

```
etask: tối ưu projects/home        ██████░░░░ 60%
  ✅ B1 gộp query member    H2 18→9 · Codex GO r2
  🔄 B2 cache tên project   suite 4.120/9.383
  ⏸ B3 chờ B2               Token 1,2M / 2M
```

Khi nào mới sửa Orca: chỉ khi dùng aTask một thời gian thấy thiếu thật → panel **chỉ đọc**
Run/Task/Gate + mở `reportPath` (đã có trong payload `worker_done`), gửi **PR lên upstream**, không giữ fork.

---

## 12. Các luồng chi tiết

### 12.1 Review MR được nhờ qua TChat

```
① A nhắn group: "nhờ review !512 etask"
② group_watch: A có trong allowlist (user id)? → tách iid, kiểm host → xếp review_mr (nguồn TChat/A)
   (tuỳ policy) ack: "Đã nhận, đang review !512"
③ worker: ghim SHA → diff → review (Claude/Codex) → review.json   [MR/tin nhắn = dữ liệu]
④ Người gác kiểm luật tự đăng (§10.2.1): phạm vi · SHA còn mới · nội dung sạch · không trùng · tần suất
   ✅ đạt → TỰ ĐĂNG comment GitLab (prefix 🤖) → báo TChat cho A → ghi vào tổng hợp hậu kiểm
   ❌ trượt luật → Telegram bạn: "!512: lý do trượt … [Xem][Đăng][Sửa][Bỏ]"
⑤ (chỉ khi trượt) bạn bấm [Đăng] → Người gác đăng đúng nội dung đã xem
⑥ MR có commit mới → tự xếp review lại
```

### 12.2 Tính năng / bug (làn Vừa)

Làm rõ → AC (🧑) → kế hoạch + kịch bản kiểm → code → thang kiểm → test mới cho AC → báo cáo AC → bằng
chứng → đề xuất tạo MR (🧑 merge).

### 12.3 Refactor lớn (làn Lớn) — một chương trình nhiều tuần

```
⓪ Mục tiêu + BẤT BIẾN (API contract, hành vi, hiệu năng ≥ nền) + định nghĩa "xong"   🧑
① Khảo sát (chỉ đọc): phụ thuộc module, điểm nóng, độ phủ test
② Lưới an toàn TRƯỚC: test chụp hành vi · snapshot contract · luật kiến trúc + danh sách ngoại lệ · số đo nền
③ Chiến lược (strangler / branch-by-abstraction / dual-write+shadow-read) + DAG lát cắt   🧑
④ Theo đợt: mỗi lát = quy trình thường + bậc "tương đương" + bậc "bánh cóc" → MR → merge vào develop
⑤ Sau mỗi đợt: đo tiến độ, lập lại kế hoạch                                          🧑 xem
⑥ Chuyển đổi + dọn dẹp                                                              🧑
```

- **Không** dùng nhánh tích hợp sống lâu — merge từng lát nhỏ vào `develop`, phần chưa xong sau flag.
- **Bánh cóc:** danh sách ngoại lệ luật kiến trúc chỉ được giảm; "142 → 0" là thanh tiến độ.
- Thay đổi cơ học bằng OpenRewrite/tool; LLM chỉ phần ngữ nghĩa.
- Song song chỉ khi lát không chung vùng sở hữu.

### 12.4 Đề xuất / cải tiến kiến trúc / nâng cấp

```
① Câu hỏi + tiêu chí (hiệu năng, công bảo trì, rủi ro, chi phí)
② Bằng chứng bằng tool: số đo, phụ thuộc, CVE/EOL, độ phủ test, code liên quan
③ Spike trên nhánh bỏ đi (vd nâng version → đếm lỗi compile/test đỏ)
④ Debate Claude ↔ Codex 2–3 vòng
⑤ ADR: bối cảnh · phương án · đánh đổi · bằng chứng · rủi ro · kế hoạch chuyển · ước lượng
⑥ 🧑 quyết định → đồng ý: ADR vào .agent/decisions/, sinh chương trình (Lớn) hoặc task (Vừa)
                → không: lưu lý do, không đề xuất lại
```

Ví dụ ứng viên: etask đang ở Spring Boot 2.1.10 / JHipster 3.0.7 / Java 11 (theo `.agent/project.yml`)
— [Inference] đều đã cũ; một đề xuất nâng cấp sẽ có spike đo khoảng cách thật.

### 12.5 Đề xuất chủ động (quét hằng tuần)

| Quét (tool, gần như không tốn token) | Nguồn |
|---|---|
| thư viện cũ, CVE, EOL | `pom.xml` / `package.json` |
| API chậm, 5xx tăng | Prometheus `http_server_requests_seconds` |
| test flaky | lịch sử `gates` |
| thiếu chuẩn quan sát | `agent_init check` |
| điểm nóng (sửa nhiều, hay lỗi) | `git log` |
| vi phạm kiến trúc tăng | bánh cóc |

LLM chỉ tóm tắt + xếp hạng → Telegram "Đề xuất tuần N" với [Làm]/[Bỏ]; [Làm] vào đúng làn.

### 12.6 Điều tra lỗi · nháp aTask · nháp TChat

- `investigate` (dev trước; prod chỉ khi công tắc bật): log Lens giới hạn + che → API GET → DB đọc →
  báo cáo nguyên nhân + đề xuất việc sửa (bấm tạo thành task `code`).
- `atask_draft` / `tchat_draft`: chỉ tạo đề xuất ghi rõ đích/người nhận; gửi thật qua Người gác.

---

## 13. Thành phần: có sẵn / sửa / mới

| Thành phần (đường dẫn theo bố cục §B.1) | Trạng thái | Việc |
|---|---|---|
| `_runtime/agent_skill_toolkit/workflows/mr_review/auto_rules.py` | **mới** | luật tự đăng comment GitLab (§10.2.1), chế độ shadow/auto, công tắc tắt |
| `context_pack`, `clarify`, `triage` | có | gom câu hỏi 1 lần; triage 4 làn + loại việc; quy tắc nâng làn |
| `debate_engine` | có | dùng cho làn Đề xuất; xuất DAG cho làn Lớn |
| `verify_gen` | có | AC → test thật trong repo |
| `test_runner` | có | trích metric, chạy lại flaky, so baseline |
| `fix_loop` | có | gộp vào `verify_ladder` |
| `review_gate` | có | lặp tới GO, gom P1/P2, trần lượt |
| `jenkins`, `k8s`, `probe_*`, `flow_check`, `local_app` | có | runner cho thang kiểm; `k8s health`, `logs --redact` |
| `run_log`, `feedback`, `status` | có | chuyển sang SQLite store; usage |
| `agent_init` | có | `scan` đề xuất `verify.ladder` |
| `group_watch`, `mr_watch`, `atask_watch`, `task_digest` | có | chỉ xếp hàng; allowlist theo user id |
| `mr-review` (`review_*`) | có (nhánh) | bỏ approval qua file/board; lock; dùng store |
| `orca_adapter` | có (nhánh) | `orca.exe`, đường dẫn thật, `worker-start/read/release`, ack inbox, retry id |
| `agent_runner` | có | bỏ xoay tài khoản qua `HOME`; agent mặc định theo loại việc |
| `config.py` (các skill) | có | `TOOLKIT_ENV_FILE` tách `.env.worker` / executor |
| `_runtime/agent_skill_toolkit/workflows/mr_review/store.py` → sau tách lên dùng chung | **mới** | sổ ghi SQLite (bắt đầu cho review MR, §B.4 M5) |
| `_runtime/agent_skill_toolkit/workflows/coordinator.py` | **mới** | vòng điều phối, quota, sở hữu, đối soát |
| `auto-dev/verify_ladder.py` | **mới** | engine thang kiểm + runners |
| `_runtime/agent_skill_toolkit/integrations/probes/apibench.py` | **mới** | bench API chung (p50/p95, bỏ lượt đầu, cùng node); tham khảo script sẵn có ở một repo sản phẩm (etask) |
| `_runtime/agent_skill_toolkit/workflows/policy.py` + `work/policy.json` | **mới** | bảng quyền + công tắc |
| `_runtime/agent_skill_toolkit/workflows/executor/` | **mới** | Người gác + Telegram + cài tài khoản dịch vụ |
| `_runtime/agent_skill_toolkit/workflows/board/atask_board.py` | **mới** | bảng tiến độ trên aTask |
| `refactor/{survey,safety_net,ratchet,slicer}.py` | **mới** | làn Lớn |
| `_runtime/agent_skill_toolkit/workflows/intake/scan_weekly.py` | **mới** | đề xuất chủ động |
| `.claude/skills/<skill>/prompts/` (playbook là tài nguyên skill) | **mới** | code, review-mr, investigate, atask-draft, tchat-draft, refactor, proposal |

---

## 14. Kế hoạch triển khai

Mỗi bước xong là dùng được. Thời gian là **ước lượng thô [Inference]**, chưa gồm chạy thử trên hệ thống thật.

| Bước | Nội dung | Nghiệm thu | Ước lượng |
|---|---|---|---|
| **1** | Làm rõ → AC + triage loại việc × làn | một yêu cầu thật ra được AC + câu hỏi chặn gom 1 lần | 3–4 ngày |
| **2** | Thang kiểm co giãn (engine chung trong toolkit), chạy local: compile → test → suite → Codex tới GO | trên ≥ 2 repo khác stack (vd 1 repo Java như etask + chính repo toolkit Python): một nhánh tự sửa tới xanh + GO, không cần điều khiển tay | 3–5 ngày |
| **3** | AC → test thật trong repo + bảng bằng chứng | MR có bảng AC → bằng chứng, ghi rõ chưa kiểm gì | 2–3 ngày |
| **4** | Sổ ghi + handoff + WIP + dừng/chạy tiếp khi hết quota + hàng đợi chung | giết worker / giả lập quota → chạy tiếp đúng bậc, kể cả đổi Claude↔Codex | 3–4 ngày |
| **5** | Cổng vào: `group_watch`/`mr_watch`/`atask_watch` chỉ xếp hàng | nhờ review trong TChat → việc vào hàng đợi đúng quyền | 2–3 ngày |
| **6** | Khoá an toàn + Người gác + duyệt Telegram (gắn hash) | worker không đăng được; đề xuất sửa/cũ/hết hạn/phát lại bị từ chối; không đăng trùng | 4–6 ngày |
| **7** | Điều phối nhiều việc + bảng tiến độ (Orca + aTask) | ≥2 việc song song, xem được trên board | 4–5 ngày |
| **8** | Thang kiểm mở rộng: deploy dev, health pod, smoke UI, e2e baseline, bench | vòng đầy đủ (như quy trình mẫu ở etask), tự động, bật theo khai báo của từng repo | 4–5 ngày |
| **9** | Làn Đề xuất (ADR + debate) + quét chủ động hằng tuần | bản "Đề xuất tuần" đầu tiên; 1 ADR thật | 3–4 ngày |
| **10** | Làn Lớn (khảo sát, lưới an toàn, bánh cóc, lát cắt) | một chương trình refactor nhỏ chạy hết 1 đợt | 1–2 tuần |

Đường tắt: nếu cần dùng sớm, làm **5 + 6 chỉ cho review MR qua TChat** trước (toolkit gần đủ, chủ yếu vá
an toàn), song song với 1–4.

**Bước 0 — bạn làm ngay, không cần code:** thu hồi / cấp lại token trong `.env` (GitLab, Azure, aTask,
TChat, Jenkins) vì hook cũ không chặn được `Grep`/`cp .env`/PowerShell [Inference: chưa kiểm được có agent
nào đã đọc]. Xem lại các launcher `claude-work.cmd` / `claude-personal.cmd` (ở gốc toolkit và trong repo
sản phẩm, vd `work/etask/claude-work.cmd`): `--dangerously-skip-permissions` = không guardrail nào.

Tái cấu trúc repo chạy theo **Track R (M1–M4, M6)** song song **Track F**; lát đầu tiên của cả hai là review MR (§B.4).
Trước đó: merge `feat/agent-init` (có `agentkit.py`) + vá Phụ lục A.

---

## 15. Không làm (v1)

- Tự làm dashboard riêng · fork Orca · xoay vòng tài khoản cùng nhà cung cấp để né limit.
- Truy cập prod mặc định · tự gửi tin/tự cập nhật task ngoài "Agent Hub" · tự merge (trừ làn Nhỏ khi policy repo cho phép).
  (Ngoại lệ đã quyết: **comment review lên GitLab được tự đăng** theo luật §10.2.1 — không bao giờ approve/merge.)
- Tự nâng quyền theo "số lần chạy sạch" (đo lịch sử duyệt, không đo đúng/sai).
- Worker chạy máy từ xa · định tuyến model tự động phức tạp.

## 16. Rủi ro chính

| Rủi ro | Giảm thiểu |
|---|---|
| Lộ token | tài khoản dịch vụ riêng, worker user thường, token đọc, đổi secret, quyền phía server |
| Ghi trùng / worker trùng | id bền, đối soát, retry có giới hạn, ack inbox, release worker |
| "Xong" giả | tách "Orca chạy xong" khỏi "được giao"; bằng chứng đo trên SHA |
| Phình phạm vi / công bảo trì > thời gian tiết kiệm | xong bước mới mở bước; đo phút người dùng tham gia + token/task |
| Đầu vào độc hại / tranh tài nguyên | `--setup skip`, chạy code cô lập, che log, sở hữu tài nguyên |
| Tài khoản bị khoá (cơ chế chuyển tài khoản do người dùng tự quản, §7.2) | toolkit không phụ thuộc vào nó; có nguồn dự phòng Codex / Claude API; trạng thái nằm ngoài phiên |
| Orca đổi CLI nhanh | mọi lời gọi qua `orca_adapter.py`; Orca là tuỳ chọn, có đường thay (aTask/GitLab) |

## 17. Câu hỏi mở (cần bạn quyết)

1. Đồng ý ngoại lệ **ghi tự động trong project aTask "Agent Hub"**?
2. Executor chạy **tài khoản Windows thứ 2** trên laptop, hay **máy khác**? Orca/worker có chạy được không cần admin?
3. Agent mặc định theo loại việc (đề xuất: code = Claude, review chéo = Codex; đảo lại khi review code Codex viết)?
4. Trong TChat: ai được nhờ review (allowlist user id), có tự trả lời "đã nhận" không?
5. Làn Nhỏ có được **tự merge khi xanh** ở repo nào không?
6. Repo sản phẩm mẫu nào (ngoài chính toolkit) để chạy thử bước 1–3? (etask là ứng viên)
7. ~~Công cụ codegraph nào?~~ → đã chọn CodeGraph (colbymchenry), đã cài 1.6.2; còn: chạy A/B.
8. Đồng ý kiến trúc repo đã chốt (§B: `_runtime/agent_skill_toolkit` + skill làm vỏ, 2 track M1–M6)?

---

## 18. Bổ sung sau review kiến trúc lần 2 (2026-10-11)

Số đo tham khảo (CodeGraph 1.6.2 trên **một repo sản phẩm mẫu cỡ lớn** — etask, Java): 4.497 file ·
155.928 node · 379.168 cạnh · DB **520 MB** · `init` ~71 s. Dùng để định cỡ, không phải phạm vi thiết kế.

| # | Vấn đề | Bổ sung vào thiết kế |
|---|---|---|
| 1 | **Thời gian thang kiểm**: repo lớn có hàng nghìn test (vd etask ~9.400) — chạy lại mỗi vòng sửa thì chậm | bậc `targeted` chọn test bằng `codegraph affected` / `callers` (gồm test gọi tới symbol đổi); `suite` chỉ chạy khi các bậc rẻ xanh + 1 lần trước khi giao |
| 2 | **Tài nguyên máy** (đã thấy tiến trình bị kill vì thiếu RAM trong phiên này): nhiều Maven build + agent song song trên 1 laptop | `MAX_PARALLEL` theo **CPU/RAM** chứ không chỉ token: semaphore riêng cho build nặng (mặc định 1), kiểm RAM trống trước khi `start_worker` |
| 3 | **CodeGraph cho worktree**: 520 MB + ~70 s mỗi lần `init` | index **một lần ở clone chính** tại SHA nền; worktree dùng index đó cho truy vấn cấu trúc + grep cho file đã đổi trong worktree; `sync` ở clone chính sau mỗi merge [Inference — cần thử cách tối ưu hơn] |
| 4 | **Handoff lọt vào MR** | đã sửa §5.3: handoff ngoài repo, squash WIP |
| 5 | **Coordinator là điểm hỏng đơn** | chạy dưới Task Scheduler / service tự khởi động lại; ghi nhịp sống vào `daemon_health.jsonl` (đã có `daemon_common`); `status.py` báo khi nhịp sống quá hạn |
| 6 | **Kiểm thử chính toolkit** mà không đốt token | backend giả (`fake_agent`) trả kết quả kịch bản: xanh / đỏ / hết quota / treo / mất response → test coordinator, quota, đối soát, người gác trong CI |
| 7 | **Review tự động MR của người khác** chạy test = chạy code lạ trên laptop có token đọc | mặc định review **tĩnh** (diff + codegraph + đọc code); chạy test chỉ khi tác giả trong allowlist **hoặc** trong môi trường không có credential (WSL/container không mount `.env`) |
| 8 | **Kiến thức `.agent/` do agent tự ghi** có thể sai / bị đầu độc qua nội dung MR | thay đổi `.agent/` (learned, operations, decisions) đi qua MR như code, không ghi thẳng |
| 9 | **Phiên bản công cụ ngoài** (Orca, Claude CLI, Codex CLI, CodeGraph) đổi nhanh | ghi phiên bản đã kiểm vào `doctor.py`; nâng cấp = chạy bộ smoke (backend giả + 1 task thật nhỏ) trước khi dùng |
| 10 | **Dọn dẹp đĩa**: worktree, index, log, review artifact | `gc` định kỳ: worktree xong > 7 ngày, log > 30 ngày; cảnh báo khi đĩa < ngưỡng |
| 11 | **Đo thời gian bạn tham gia** (R4) chưa có bước cụ thể | bảng `events` ghi mỗi lần bạn duyệt / sửa / từ chối kèm thời điểm; báo cáo tuần — thêm vào bước 4 |
| 12 | **Việc nhiều repo** (BE + FE + service khác) chưa có trong thiết kế | theo `auto-dev/cookbook/multi-repo.md`: chốt hợp đồng trước, mỗi worker một repo, verify tích hợp; CodeGraph index từng repo riêng (lời gọi HTTP/Kafka giữa repo không có trong graph) |
| 13 | **Cấu hình MCP codegraph cũ** trên máy trỏ tới lệnh chưa từng cài (log lỗi 22/09) | sau khi cài: rà cấu hình MCP các profile (`~/.claude.json`, `~/.claude-work`, Codex) cho thống nhất, tắt telemetry ở mọi profile |

Thứ tự kế hoạch §14 không đổi; các mục trên gắn vào bước tương ứng (1→bước 2, 2/5/6→bước 4, 3→bước 1–2,
7→bước 5–6, 8→bước 3, 9/10→bước 7, 11→bước 4, 12→sau bước 8).

---

## 19. Kết quả verify các điểm chưa giải quyết (2026-10-12)

| # | Điểm | Cách kiểm | Kết quả |
|---|---|---|---|
| 1 | `_runtime/` không SKILL.md có bị coi là skill? Manifest `skills` hợp lệ? | plugin thử + `claude plugin validate` (CC 2.1.296) + nạp tại chỗ `--plugin-dir` | ✅ manifest hợp lệ (path field, cộng thêm vào `skills/` mặc định); `_runtime` **không** bị đăng ký thành skill, không lỗi; chỉ `plugtest:hello` xuất hiện. Theo tài liệu, cài từ marketplace **copy cả thư mục plugin** vào cache → `_runtime` có mặt. Hành vi "thư mục không SKILL.md bị bỏ qua" **không được ghi trong tài liệu** (chỉ kiểm thực nghiệm). Cài qua marketplace thật + Orca/Codex: **chưa thử** |
| 2 | Plugin thật của repo | `claude plugin validate` trên bản chụp repo | ✅ hợp lệ; cảnh báo: `CLAUDE.md` ở gốc plugin **không** được nạp làm ngữ cảnh khi cài dạng plugin |
| 3 | Vòng phụ thuộc thật | quét AST import | 1 vòng thật (`dev-automation ↔ remote-control`), 4 import `config` mơ hồ — đã sửa §A.2 |
| 4 | Baseline test Windows | `python -m unittest discover -s tests` | 191 test, **1 lỗi** (`test_vpn`: `os.geteuid`); con số 191 gồm cả test chưa commit trên máy |
| 5 | Chèn lệnh qua `orca.cmd` | `.cmd` giả + `subprocess.run([...cmd, '--display-name', 'x" & echo MARKER & "'])` | ❌ **xác nhận lỗ hổng**: lệnh thứ hai chạy được. Bắt buộc gọi `orca.exe` |
| 6 | Token trong `.env` đã bị agent đọc? | quét transcript Claude Code trên máy | ❌ **Có**: nhiều phiên chạy `grep '^…TOKEN=' .env`, `cat .env`, `cp .env` (toolkit + etask). Giá trị token đã nằm trong lịch sử phiên → **phải thu hồi / cấp lại** (Bước 0, §14) |
| 7 | Orca/worker chạy không cần admin? | kiểm token Windows + UAC | User `ThisPC` **thuộc nhóm Administrators**, tiến trình hiện chạy **không elevated**, UAC bật (`EnableLUA=1`, `ConsentPromptBehaviorAdmin=5`). [Inference] ACL chặn được tiến trình không elevated, nhưng tài khoản admin có thể elevated (qua hộp UAC) để chiếm quyền file → executor nên dùng **tài khoản riêng + worker dưới user chuẩn**, hoặc đặt executor ở máy khác |
| 8 | CodeGraph với worktree | init toolkit, tạo worktree, copy index + sync | xem §8.1: không kế thừa; copy + `sync` chạy được (~4 s ở toolkit); `affected` chọn test đúng |
| 9 | aTask có phụ thuộc giữa task? | đọc `atask-automation/tools/tasks.py` | Toolkit hỗ trợ **task cha/con** (`--parent`, `subtasks`); **không** có trường phụ thuộc (dependency) → ghi phụ thuộc trong checklist/mô tả. Server có hỗ trợ hay không: chưa kiểm |
| 10 | Điều khoản nhiều tài khoản | đọc Consumer Terms | không có điều khoản đích danh; xem §7.2 (diễn giải, chưa kết luận) |
| 11 | Prompt caching qua Orca | — | **chưa kiểm được** (cần đo usage thực tế qua Orca) |

Còn mở sau verify: cài plugin qua marketplace + nạp trong Orca/Codex (M2), chi phí copy index repo lớn,
dependency phía server aTask, prompt caching, Acceptable Use Policy.

---

> **Trạng thái 2026-10-12 (chưa commit):** đã vá tất cả HIGH + các MED `save_job`/đăng trùng/inline đăng lại/
> validate crash/old_path/`auth reconcile`; thêm luật tự đăng (`review_rules.py`, mặc định `shadow`). Suite Windows:
> 308 test xanh, 1 skip (POSIX-only). **Còn lại (MED):** clone race `review_ws`, lock bị phá giữa chừng, `podfwd`
> (`--insecure`, process mồ côi, PID tái dùng), mật khẩu trong argv `vpn.py` (Windows), allowlist theo tên
> `group_watch`, chuyển backend theo stderr `agent_runner`. **Giới hạn tạm thời:** approval của người vẫn ghi
> trong file cùng user OS → chỉ executor tách biệt (Track F M5) mới chặn được giả mạo hoàn toàn.

## Phụ lục A — Lỗi đã tìm thấy ở nhánh `feat/mr-review-orca-tooling` (cần vá ở bước 6)

| Mức | Vị trí | Vấn đề |
|---|---|---|
| HIGH | `review_post.py:192` | approval `via=terminal` đọc từ file job agent ghi được → giả duyệt |
| HIGH | `guard_write.py:36` + `review_post.py:194` | duyệt = trạng thái thẻ Orca, agent đặt được qua CLI/path Windows |
| HIGH | `guard_write.py` | lách qua `agentkit`, tool PowerShell, `Grep .env`, `cp .env`; agent không phải Claude không qua hook |
| HIGH | `orca_adapter.py:39` | ưu tiên `orca.cmd` → **ĐÃ XÁC NHẬN (§19)** chèn lệnh qua tham số khi gọi `.cmd` bằng `subprocess` (tiêu đề MR có `" & … & "` chạy được lệnh thứ hai); đường dẫn shim sai với bản cài thật |
| HIGH | `k8s.py:477` | redact Secret lách được (`secret/x`, `secrets,cm`, annotation last-applied) |
| HIGH | `dbeaver.py:136,166` | guard PROD lách bằng nhiều câu lệnh; `--host` đè đích |
| MED | `review_post.py:244`, `review_common.py:130` | đăng trùng; `save_job` không lock, `.tmp` chung |
| MED | `review_ws.py:209`, `review_common.py:93` | clone race xoá clone job khác; lock bị phá giữa chừng |
| MED | `podfwd.py` | `--insecure` hỏng; process mồ côi; PID tái dùng |
| MED | `vpn.py:220` | mật khẩu trong argv (Windows) |
| MED | `group_watch.py:270` | allowlist theo tên hiển thị |
| MED | `agent_runner.py:316` | chuyển backend theo chuỗi stderr; agy/Cursor chạy bỏ qua quyền |
| LOW | `test_vpn.py` | `os.geteuid` không có trên Windows → cần `skipIf` |

## Phụ lục B — Kết luận debate Claude ↔ Codex (2 vòng)

- Orca không lên lịch; `worker_done` tự kết thúc task; gate Orca là điều phối, không phải ủy quyền.
- Kiểm quyền trong tool không đủ với agent có shell → executor + credential tách biệt là ranh giới thật.
- TTY không chứng minh có người; phê duyệt phải gắn đề xuất bất biến.
- Ghi ra ngoài có thể "không rõ kết quả" → id thực thi + đối soát; không hứa exactly-once.
- Refactor lớn không phải ví dụ đầu tiên; bắt đầu từ luồng đã gần đủ (review MR) và vòng thử–sai local.
- Hai agent (Claude + Codex) được, với một hợp đồng task/kết quả chung.
- Kiểm UI Orca bằng một lần chạy thật bị gián đoạn trước khi phụ thuộc vào nó.

## Phụ lục C — Debate kiến trúc repo Claude ↔ Codex (2026-10-12, 2 vòng)

- Vòng 1 (Codex): kiểm kê §A lệch; "vòng phụ thuộc" trộn import với subprocess/comment; điều phối đã có một
  phần; lặp cấu hình sâu hơn tên file; **5 lớp vật lý quá nặng cho 1 người** → một package namespace, skill
  làm vỏ; `agentkit` không khử import nhầm; tương thích phải gồm manifest/frontmatter/`__file__`/plugin đã
  cài; không gộp dọn kiến trúc với hệ thực thi mới; chuyển theo lát dọc MR review.
- Vòng 2 (chốt): runtime tại `.claude/skills/_runtime/agent_skill_toolkit/` + shim bootstrap (phải kiểm
  bằng cài plugin thật); **cần store + executor ngay cho review MR** (Track F) tách khỏi Track R; 3 vùng
  `core`/`integrations`/`workflows` với `workflows/execution` cho runner agent; trạng thái nghiệp vụ thuộc
  workflow; module xoay tài khoản ngoài toolkit phân phối.
