# Lộ trình sản phẩm: công cụ cá nhân → nhóm → sản phẩm riêng

> Trạng thái: **v4.1 — Codex GO (vòng 4)**. Ngày: 2026-10-17. Đã duyệt ở mức lộ trình/thiết kế, chưa triển khai.
> v1 → Codex vòng 1: GO-WITH-CHANGES (7 HIGH, 2 MED, 1 LOW). v2 → vòng 2: GO-WITH-CHANGES (3 HIGH, 2 MED).
> v3 → vòng 3: GO-WITH-CHANGES (1 HIGH, 2 MED).
> Thay đổi ghi ở §9.
> Đầu vào: audit 3 mảng ngày 2026-10-16. Điểm số/ước lượng là [Inference] từ đọc code, không phải đo đạc.

## 0. Tầm nhìn

1. **Giai đoạn 1** — công cụ làm việc ở công ty cho *một* người, tin được tới mức giao việc không cần canh.
2. **Giai đoạn 2** — nhóm nhỏ (2–5 người).
3. **Giai đoạn 3** — sản phẩm riêng.

Hai nguyên tắc xuyên suốt:
- **Ranh giới tin cậy thật, không phải regex.** Agent không giữ credential ghi và không ghi được vào thành
  phần tin cậy. Mọi thao tác ghi ra ngoài đi qua một **broker** (executor giữ token) tự kiểm authorization.
  Hook `guard_write.py` chỉ còn là lớp UX/phòng thủ phụ.
- **Phần riêng của công ty nằm sau adapter** — làm dần khi cần, không adapter hoá toàn bộ trước khi có người dùng thứ hai.

## 1. Hiện trạng (tóm tắt audit, đã sửa theo Codex)

| Mảng | Điểm | Ghi chú |
|---|---|---|
| mr-review — đúng diff | 8–9 | diff_refs chuẩn |
| mr-review — an toàn duyệt | 6 | stale chỉ so head/state (`review_ws.py:302–308`), base/target đổi vẫn lọt; duyệt dựa TTY + tên env (`review_post.py:620–632`) — TTY không phải xác thực người |
| Store/engine | 7 | `run_log` không atomic/không khoá read-modify-write (`run_log.py:145–147`) |
| Test | [Unverified] | 723 test xanh khi chạy local; số lượng không chứng minh chất lượng. Lệnh chạy có ở `tests/test_layering.py:13` nhưng không có trong README |
| Mốc duyệt auto-dev | 3 | §2.2 |
| Truy vết task↔branch↔MR | 4 | `create-mr` description rỗng |
| CI | 1 | Không thấy cấu hình CI trong repo (có thể có ngoài repo — chưa kiểm) |
| Tài liệu | 4 | `.agents/skills` bản copy lệch; README cũ |
| Migrate `_runtime` | 4 | `core/env.py` đã được `integrations/gitlab.py`, `workflows/settings.py` dùng, nhưng 4 `config.py` của skill vẫn tự load `.env` |

## 2. Giai đoạn 1 — "tin được cho một người"

### 2.0 Bước 0 — dừng ghi tự động chưa kiểm soát (ngay)

Cho tới khi **broker + tách quyền (§2.1) chạy và qua bộ test thử lách (§2.6 tiêu chí)**: không chạy `queue_worker`/`autopilot`/bridge Telegram/`group_watch` ở chế độ spawn
agent `--dangerously-skip-permissions`; `REVIEW_AUTO_POST` không ở `auto`. Polling/status chỉ đọc vẫn chạy.
Lý do: từ 2026-10-16 máy dev đặt `TOOLKIT_GUARD_ASK=off` nên agent con không bị chặn ghi; lọc env (G4) chưa
chặn agent đọc credential từ đĩa. Bắt đầu đo số lần can thiệp (§2.6) ngay.

### 2.1 Ranh giới tin cậy: broker + tách quyền

```
 agent (user OS "agent" / sandbox)          broker (user OS người dùng)           ngoài
 ┌──────────────────────────┐   IPC     ┌──────────────────────────────┐   token  ┌────────┐
 │ đọc code, chạy test,     │──yêu cầu─▶│ kiểm authorization + gate     │─────────▶│ GitLab │
 │ soạn review/MR/notify    │           │ giữ token ghi, khoá ký proof  │          │ Azure  │
 │ KHÔNG có token ghi       │◀─kết quả──│ ghi audit, idempotency        │          │ TChat  │
 │ KHÔNG ghi được vùng tin  │           │ nhận sự kiện duyệt thật       │◀─duyệt───│ Tele.  │
 └──────────────────────────┘           └──────────────────────────────┘          └────────┘
```

- **Tách quyền bằng user OS riêng (hoặc sandbox tương đương) ngay giai đoạn 1.** Keyring cùng user OS
  không đủ trước agent có shell tuỳ ý (Codex §7.2).
  [Unverified] Cách làm cụ thể trên Windows (user riêng + ACL thư mục; hoặc Windows Sandbox/WSL) cần spike.
- **Vùng tin cậy** (agent không ghi được): broker, hook, `.claude/settings*.json`, `.codex/` config,
  `remote-control/tools/{telegram_approve,approvals,telegram_bridge}.py`, executor, state duyệt/gate,
  khoá ký. Triển khai: broker cài ở thư mục riêng ngoài repo agent làm việc, chỉ user người ghi được.
- **Không đọc được, không chỉ không ghi được.** Token ghi, khoá ký, bot token Telegram nằm trong vùng
  user agent **không đọc** được (ACL). Agent không có credential push GitLab: mọi đường ghi GitLab, gồm
  `git push`, đi qua broker. Ghi Azure/aTask/TChat chưa qua broker ⇒ **tắt**, chỉ còn đọc.
- **Broker không nạp gì do agent kiểm soát:** cấu hình cố định ngoài workspace (không đọc `.env` theo cwd
  như `dev-automation/tools/config.py:42–63`), code broker import từ thư mục cài đặt riêng, không thêm
  workspace vào `sys.path`, không gọi executable/script trong repo agent.
- **Broker từ chối mọi write thiếu ngữ cảnh/authorization** — không dựa vào biến tuỳ chọn như `RUN_ID`.
  API wrapper phía agent chỉ để tiện, không phải chốt chặn.
- Broker **không** có API "ký payload agent đưa" và **không nhận JSON "update" Telegram từ agent**. Nó tự lấy
  sự kiện qua kênh bot Telegram đã xác thực (long-poll bằng token chỉ broker đọc được), hoặc TTY của phiên
  người dùng được bảo vệ (không phải cờ agent tự khai), rồi tự tạo proof.
- **IPC** (named pipe trên Windows) có ACL và nhận diện tiến trình gọi.

**Authorization theo từng thao tác.** Agent gửi *đề xuất*; broker lưu đề xuất, hiển thị cho người duyệt,
và khi duyệt tạo authorization ràng buộc **nội dung chuẩn hoá**:

```json
{"op": "gitlab.create_mr", "host": "<gitlab_url>", "project": 123,
 "source": "feature/123-x", "target": "develop", "commit": "<sha>",
 "payload_sha256": "<hash title+description>", "actor": "<approver_id>",
 "run": "<run_id>", "nonce": "<random>", "expires_at": "<iso>"}
```

- Mỗi op (`create_mr`, `push`, `notify`, `comment`…) một authorization riêng; `notify` ràng buộc người nhận + nội dung.
- Broker **tự dựng request** từ dữ liệu đã duyệt, không nhận request agent dựng sẵn.
- Tiêu nonce gắn với bản ghi thao tác bền trong SQLite: `authorized → executing → sent | uncertain | failed`,
  trong một transaction. Crash giữa chừng ⇒ `executing/uncertain` ⇒ đối soát trước khi thử lại; không mất
  quyền, không gửi trùng.

**Runner test tin cậy (cho G6).**
- Bộ điều phối runner thuộc broker (vùng tin cậy). Test chạy dưới **user quyền thấp, không credential,
  không mạng tới hệ thống ghi**.
- **Snapshot bất biến:** runner checkout đúng SHA từ **repo broker** (sau bước nhập object bên dưới) vào thư
  mục do runner sở hữu; source **chỉ đọc** với cả agent lẫn user test. Build/temp ghi vào vùng riêng
  (`out/`, `TMP`) tách khỏi source. Mỗi lần chạy lại tạo snapshot sạch; không tái dùng thư mục cũ.
  Bảo đảm chính là snapshot đúng SHA + quyền chỉ đọc; `git status --porcelain --ignored` rỗng chỉ là kiểm tra
  phụ, không phải bằng chứng duy nhất.
- Lệnh test lấy từ cấu hình project được quản lý trong vùng tin cậy (bản sao `projects.json` của broker),
  **không** từ `--cmd` agent đưa (`test_runner.py:7–22` hiện nhận `--cmd`).
- [Inference] Code test vẫn do agent viết, nên runner chứng minh "lệnh đã cấu hình chạy xanh trên SHA X",
  không chứng minh test đủ tốt — phần đó thuộc review.
- Chứng cứ: `{job, commit, cmd_id, exit_code, log_sha256, started_at}` runner ghi thẳng vào SQLite của broker
  (ngoài quyền ghi của agent/test) — không cần chữ ký vì không đi qua ranh giới khác; broker kiểm khi xét `create_mr`.

**Nhập commit & push (agent không có credential push).**

```
 repo agent (agent ghi)        tiến trình nhập (user quyền thấp)          repo broker (bare, broker sở hữu)
 commit trong worktree  ──▶  git bundle create <sha>  ──bundle file──▶  git fetch <bundle> <sha>:refs/incoming/<run>
                                                                         git (exe/config/env cố định) push <sha>:<ref đã duyệt>
```

- Broker **không chạy Git trong repo agent** và không đọc file của nó trực tiếp. Một tiến trình quyền thấp
  (không credential) tạo `git bundle` chứa đúng SHA đề xuất; broker chỉ nhận file bundle.
- Broker `git fetch` bundle vào **repo bare riêng** của nó, rồi kiểm SHA trong bundle khớp SHA được duyệt.
- Bundle là đầu vào không tin cậy. Tiêu chí triển khai: broker nhận **bản sao** bundle mà agent không sửa
  được; `git bundle verify` + `fetch.fsckObjects=true` / `fsck --strict` trong **repo tạm** trước khi nhập repo
  chính; giới hạn dung lượng/thời gian/tài nguyên; Git luôn cập nhật. Các kiểm tra này bổ trợ cho cách ly,
  không chứng minh parser Git miễn lỗi.
- Git của broker: executable đường dẫn tuyệt đối cố định; `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL` trỏ file
  tin cậy; `core.hooksPath` trỏ thư mục rỗng; không dùng helper/credential của repo agent; remote URL lấy từ
  cấu hình tin cậy, không từ bundle.
- Push **chính xác** `<sha>:refs/heads/<ref đã duyệt>`, không force (trừ khi authorization ghi rõ), rồi đối soát
  SHA trên remote trước khi đánh dấu `sent`.

### 2.2 Toàn vẹn mốc duyệt

| ID | Lỗi (bằng chứng) | Sửa |
|---|---|---|
| G1 | `task_queue.py:511–512` coi mục thiếu/rỗng = đồng ý; `tg_gate.py:161–167` → `approved_all` | Mặc định mỗi mục **pending**. Chỉ duyệt khi **toàn bộ** câu trả lời khớp whitelist (`ok`, `duyệt`, `approve`… so khớp nguyên câu sau chuẩn hoá, không tìm chuỗi con). Mục chưa trả lời không kế thừa đồng ý; duyệt một phần ⇒ chỉ mục được nêu |
| G2 | `tg_gate.py:176–179` tự ghi `source=telegram`; `run_log.py:198–206` chỉ kiểm tên source | Proof do broker tạo, ràng buộc `actor, project, run, gate, content_hash, commit, nonce, expires_at`; **dùng một lần** (broker ghi nonce đã tiêu). Bỏ `--text`, bỏ `--source human\|telegram` phía agent |
| G3 | `telegram_bridge.py:467–476` không kiểm người bấm; `approvals.py:44–52` ghi đè quyết định đã chốt; `_is_allowed` (`:83–84`) kiểm **chat** | Allowlist **approver user ID** theo project, tách khỏi allowlist chat. Kiểm user/project/chat/message/request. `pending → decided` một lần, trong transaction SQLite, kiểm expiry |
| G4 | `queue_worker.py:57–60`, `autopilot.py:71–73` không lọc env; `telegram_bridge.py:309–311`, `group_watch.py:223–229` copy toàn bộ env | Dùng lại `agent_runner.py:160–172` (đã lọc secret + đặt headless) làm hàm spawn duy nhất; test layering cấm gọi `claude -p` nơi khác; xoá `TOOLKIT_GUARD_ASK` khỏi env con. Bridge: thao tác đã được người duyệt **thực thi qua broker**, không qua shell agent ⇒ không còn hai hook quyết định mâu thuẫn |
| G5 | `ssh_exec.py:45–61` nhận read theo đầu lệnh; `telegram_approve.py:30–32,108–110` tự cho Read/WebFetch | Tự cho phép chỉ tập lệnh đọc với **argv kiểm soát** (không qua shell). Mọi lệnh shell có substitution/redirect/pipe/interpreter ⇒ hỏi. Read secret + WebFetch bỏ khỏi auto-allow. Chặn secret và đích mạng ở broker/sandbox, không ở regex |
| G6 | `run_log.py:355–363` checkpoint mode qua khi gate thiếu; `pipeline.md:327,341` tạo MR trước `advance deliver`; `run_log.py:384–388` chỉ cảnh báo khi verdict trái JSON | Gate ở broker: `create-mr`/`notify` cần proof `before_mr` + **chứng cứ test do runner tin cậy tạo**, gắn commit. Verdict trái JSON ⇒ từ chối. Commit đổi ⇒ vô hiệu duyệt + test cũ. Test đỏ chặn ở mọi mode |
| G7 | `telegram_approve.py:45,81–83,108–110` tự duyệt sửa file; `guard_write.py:87–92` không bảo vệ hook/settings | Giải bằng §2.1 (vùng tin cậy ngoài quyền ghi của agent), không bằng danh sách deny |

### 2.3 Toàn vẹn state & idempotency (trước nền móng)

- `run_log`: khoá bao trùm **read–modify–write** + ghi atomic; note có timestamp/actor. Dài hạn: vào SQLite store.
- Thao tác ngoài (create-branch/MR, notify, comment): trạng thái `pending → sent | uncertain`; timeout ⇒
  `uncertain` ⇒ **đối soát** với hệ thống ngoài trước khi gửi lại. Theo mẫu `review_post.py:645–670,748–751`.
- `create-branch`/`create-mr` đã tồn tại ⇒ trả về cái cũ.
- Trùng việc trong một máy: khoá theo `(project, iid/task)` ở broker. Trùng giữa nhiều người: **chưa cam kết**,
  để giai đoạn 2 (§3).

### 2.4 mr-review — vá trước khi làm lõi sản phẩm

- Stale = so **toàn bộ ngữ cảnh diff** (base/start/head SHA + target branch + state), đổi ⇒ vô hiệu duyệt.
- Duyệt qua broker (§2.1), bỏ TTY làm xác thực.
- Rủi ro "chỉ comment" vẫn có: lộ dữ liệu trong comment, spam ⇒ giữ cap/ngày, shadow mặc định.

### 2.5 Nền móng kỹ thuật (tối thiểu)

- CI tối thiểu: unittest trên Windows/macOS/Ubuntu. Lệnh chạy test vào README.
- Hook/bootstrap/broker entrypoint **và toàn bộ import bắc cầu** thuần stdlib (test AST).
- `pyproject.toml` + `uv.lock` khi có dependency đầu tiên có nhu cầu cụ thể — không thêm hàng loạt.
  Ứng viên theo nhu cầu: pymysql/pg8000 (thay client socket), filelock. [Unverified] Plugin Orca/Codex có
  dùng đúng Python của venv không — cần kiểm thực tế.
- Gộp `.agents/` về một nguồn; 4 `config.py` dùng `core/env.py`; docs cập nhật.

### 2.6 Đo giá trị (từ ngày đầu)

`feedback.py`/`status.py`: số lần người can thiệp mỗi run, % MR tự review, tỷ lệ comment review bị bỏ
qua/resolve, giờ tiết kiệm ước tính/tuần.

**Tiêu chí xong giai đoạn 1:** broker + tách quyền chạy; G1–G7 đóng có test hồi quy (gồm test thử lách:
sửa JSON state, gọi Python trực tiếp, bỏ `RUN_ID`, replay proof); CI xanh 3 OS; 4 tuần dùng thật có số đo.

## 3. Giai đoạn 2 — nhóm nhỏ

- Mỗi người broker + credential riêng; mọi write có `actor`.
- Approver = user ID trong allowlist project (đã có từ G3).
- Cài một lệnh: cài broker → `doctor.py` → wizard (`toolkit_settings.py`).
- Store local mỗi người; `projects.json` chia sẻ qua repo riêng của nhóm.
- Chống trùng giữa nhiều người cần **claim chung** — câu hỏi mở. Ứng viên: claim trên chính hệ thống ngoài
  (gán reviewer/label GitLab, assignee task) làm lease, hoặc một store chung nhỏ (tiền thân service §4).
- Adapter hoá các integration mà người dùng thứ hai thực sự cần khác (không làm toàn bộ).

## 4. Giai đoạn 3 — sản phẩm

| | A. Plugin local mỗi dev | B. Service trung tâm |
|---|---|---|
| Ưu | Phát hành dễ, không server | Quản lý tập trung, audit, RBAC |
| Nhược | Nâng cấp đồng loạt khó | Server, phân quyền, chi phí token tập trung |

**Lõi sản phẩm đầu tiên = mr-review**, dạng plugin local, shadow/duyệt-trước-đăng, credential giới hạn.
Chỉ chuyển sang service khi nhu cầu điều phối tập trung đã chứng minh. Broker ở §2.1 là tiền thân của service.

Rủi ro ngoài kỹ thuật: IP khi viết trong thời gian/tài nguyên công ty; chính sách gửi code/task công ty
tới nhà cung cấp LLM; chi phí token theo người. [Unverified] chưa xem hợp đồng/chính sách.

## 5. Kiến trúc port/adapter (làm dần)

```python
# core/ports.py
from typing import Protocol

class TaskSource(Protocol):
    def get(self, task_id: str) -> dict: ...
    def comment(self, task_id: str, text: str) -> None: ...

class CodeHost(Protocol):
    def mr_detail(self, iid: int) -> dict: ...
    def post_review(self, iid: int, note: str, inline: list[dict]) -> None: ...

class Notifier(Protocol):
    def send(self, to: str, text: str) -> None: ...
```

Phương thức **ghi** của port chỉ có implementation ở phía broker. Luật layering: `core/`, `workflows/`
không import `integrations.<cụ thể>`.

## 6. Thứ tự thực hiện

0. Bước 0 (§2.0) + bắt đầu đo (§2.6).
1. G4 cùng G1–G3 (spawn an toàn, duyệt đúng người/đúng mục).
2. Spike user Windows riêng + ACL + named pipe (kiểm Claude Code/Orca chạy được dưới user agent) →
   broker tối thiểu (GitLab write gồm push qua bundle + notify, authorization theo op, runner snapshot bất biến,
   **nhật ký thao tác SQLite + phục hồi crash + đối soát `uncertain`**) → G2/G5/G6/G7 qua broker → test thử lách
   (gồm test crash giữa `executing` và timeout API) → mới bật lại automation ghi (§2.0).
3. Phần state còn lại phía agent: `run_log` atomic/khoá, idempotency `create-branch`/`create-mr` (§2.3).
4. CI tối thiểu + test stdlib cho hook/broker.
5. mr-review vá (§2.4).
6. Gộp `.agents/`, docs, `config.py` → `core/env.py`.
7. Dependency theo nhu cầu (driver DB) → 4 tuần dùng thật → giai đoạn 2.

**Quy trình kiểm theo rủi ro:** thay đổi chạm broker/duyệt/gate/credential ⇒ debate Codex tới GO.
Thay đổi khác ⇒ CI + test tự động, debate khi thấy cần.

## 7. Câu hỏi còn mở

- Chống trùng việc giữa nhiều người (§3) — giải ở giai đoạn 2.
- Kiến trúc A/B giai đoạn 3 (§4).
- [Unverified] Claude Code/Orca chạy được dưới user Windows riêng — spike ở §6 bước 2.

## 8. Nhật ký debate Codex

| Vòng | Bản | Verdict | Phát hiện |
|---|---|---|---|
| 1 | v1 | GO-WITH-CHANGES | 7 HIGH (G1–G7 cách sửa chưa đủ: regex/keyring cùng user, `RUN_ID` lách được, gate test giả được), 2 MED, 1 LOW (audit sai về `core/env.py`) |
| 2 | v2 | GO-WITH-CHANGES | 3 HIGH (runner tin cậy, proof chưa gắn op/đích, chưa chặn đọc secret), 2 MED |
| 3 | v3 | GO-WITH-CHANGES | 1 HIGH (đường nhận commit/push), 2 MED (checkout bất biến, thứ tự bật lại) |
| 4 | v4 | **GO** | 2 LOW (bundle là đầu vào không tin cậy; `git status` không phải bằng chứng duy nhất) — đã đưa vào v4.1 |

(Câu hỏi vòng 2 đã được Codex trả lời: user Windows riêng + ACL; broker v1 = GitLab write + notify đủ nếu gồm push và tắt ghi khác; bước 0 hợp lý; broker tự lấy sự kiện Telegram.)

## 9. Thay đổi so với v1 (theo Codex vòng 1)

- Thêm bước 0 dừng ghi tự động; đo giá trị từ đầu.
- Thay "keyring + HMAC cùng user" bằng **broker + tách quyền user OS/sandbox** ngay giai đoạn 1.
- G1: pending mặc định, whitelist nguyên câu, duyệt một phần. G2: proof ràng buộc + nonce + một lần.
  G3: allowlist approver user, transaction + expiry. G4: dùng lại `agent_runner`, bridge thực thi qua broker.
  G5: argv allowlist, shell phức tạp ⇒ hỏi. G6: gate ở broker, chứng cứ test tin cậy gắn commit, bỏ phụ thuộc `RUN_ID`.
  G7: vùng tin cậy thay vì danh sách deny.
- State atomic bao read-modify-write; trạng thái `uncertain` + đối soát.
- Hạ điểm an toàn mr-review, thêm §2.4.
- Sửa audit: `core/env.py` có được dùng; lệnh test có trong `test_layering.py`; CI "không thấy trong repo".
- Bớt quá tay: không adapter hoá toàn bộ sớm, không thêm dependency hàng loạt, debate theo rủi ro.

**v3 (theo Codex vòng 2):**
- Runner test tin cậy: điều phối trong vùng tin cậy, test chạy dưới user quyền thấp không credential, checkout bất biến đúng SHA, lệnh từ cấu hình được quản lý (bỏ `--cmd` agent đưa).
- Authorization theo từng op, nội dung chuẩn hoá (op/host/project/source/target/commit/payload/người nhận), broker tự dựng request; tiêu nonce trong transaction gắn bản ghi thao tác bền.
- Tách quyền: token/khoá **không đọc được**; broker không nạp `.env`/module/executable từ workspace; mọi ghi GitLab gồm push qua broker; ghi Azure/aTask/TChat chưa qua broker thì tắt.
- Bước 0 kéo dài tới khi broker + tách quyền qua test thử lách.
- Chống trùng nhiều người: rút cam kết khỏi broker local, chuyển thành câu hỏi mở giai đoạn 2.
- Broker tự lấy sự kiện Telegram, không nhận update JSON từ agent; IPC named pipe có ACL.

**v4 (theo Codex vòng 3):**
- Nhập commit qua `git bundle` do tiến trình quyền thấp tạo → repo bare của broker; Git broker với exe/config/hooks/remote cố định; push đúng SHA→ref đã duyệt, đối soát remote.
- Runner: snapshot từ repo broker, source chỉ đọc, build/temp vùng riêng, snapshot sạch mỗi lần, kiểm cây khớp SHA.
- Chứng cứ runner ghi thẳng SQLite broker, không ký.
- Điều kiện bật lại automation gồm nhật ký thao tác + phục hồi crash + đối soát `uncertain`; chỉ `run_log` phía agent để sau.
