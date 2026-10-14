# Hướng dẫn sử dụng Agent-Skill-Toolkit

## 1. Hiểu nhanh trong 30 giây

Toolkit = **một hàng đợi việc (store) + các "làn" xử lý + một người điều phối**.

```
  Nguồn việc                    Hàng đợi (temp/toolkit.db)          Điều phối               Bạn
  ───────────                   ──────────────────────────          ─────────               ───
  mr_watch (MR gắn tên bạn) ─┐
  FChat/TChat group         ─┤    task: review_mr / verify /     coordinator.py work     duyệt ở các
  aTask watch               ─┼──▶ proposal / refactor / atask ──▶ (chạy song song,  ──▶  "cổng"
  bạn gõ lệnh (manual)      ─┤    trạng thái: pending/running/    giới hạn quota)        (approve/decide)
  quét tuần (schedule)      ─┘    blocked/paused_quota/done
```

- Việc **không mất** khi hết quota hay tắt máy: task chuyển `paused_quota`, chạy lại `work` là tiếp từ checkpoint.
- Task `blocked` = **đang chờ bạn** (duyệt, hoặc agent không tự xử được). Xem lý do ở `board`.
- Mọi thao tác ra ngoài (comment GitLab, báo tester, đổi state) đều qua cổng duyệt của bạn.

## 2. Ba cách dùng (chọn cách hợp nhất)

| Cách | Khi nào | Ví dụ |
|---|---|---|
| **A. Nói chuyện với Claude Code** trong repo này | Hằng ngày, dễ nhất | "review MR 123 của etask", "đọc task 45678 rồi code giúp tôi" |
| **B. Slash command** | Việc lặp lại có sẵn | `/review-mr`, `/fix-bug`, `/implement-feature`, `/list-tasks`, `/watch-mrs`, `/atask-triage` |
| **C. Chạy tool trực tiếp / daemon** | Muốn tự động chạy nền, hoặc kiểm soát chi tiết | `coordinator.py work`, `mr_watch.py` |

Cách A/B: Claude tự chọn tool phía dưới. Phần còn lại của tài liệu là cách C — để bạn hiểu chuyện gì xảy ra bên dưới.

> Tất cả lệnh chạy **từ gốc repo**. Viết tắt: `T=.claude/skills` (PowerShell: `$T=".claude/skills"`).

## 3. Chuẩn bị một lần

```bash
python $T/dev-automation/tools/doctor.py     # máy thiếu gì (git, mvn, kubectl, codex...)
python $T/dev-automation/tools/config.py     # token GitLab/Azure/aTask đã đủ chưa
```
Project đăng ký trong `work/projects.json` (hiện có: etask, eintelligent, econdrive, …). Code clone ở `work/<tên>/`.

## 4. Các tình huống dùng hằng ngày

### 4.0 Giao diện web theo dõi (mở trong trình duyệt Orca)

```bash
python $T/dev-automation/tools/dashboard.py serve        # rồi mở http://127.0.0.1:8765
```
Một trang tự làm mới (5/10/30 giây): ô số tổng quan, **"Cần bạn xử lý"** (việc bị chặn, yêu cầu duyệt — kèm lệnh, bấm để copy),
toàn bộ việc trong hàng đợi (lọc theo loại), tài khoản agent + dung lượng, review MR, daemon, pipeline run, port-forward, feedback.
Chỉ đọc: không có nút hành động — mọi thao tác vẫn là lệnh ở terminal. Chỉ nghe `127.0.0.1` (máy khác không vào được).
Đổi cổng: `--port 9000`. Dừng: Ctrl+C.

### 4.1 Xem đang có việc gì — lệnh dùng nhiều nhất

```bash
python $T/auto-dev/tools/coordinator.py board          # bảng: việc nào chạy / chờ bạn / hết quota
python $T/dev-automation/tools/status.py               # tổng hợp: daemon, queue, approval đang chờ
```

### 4.2 Review một MR

```bash
# 1. xếp hàng
python $T/mr-review/tools/review_flow.py enqueue --project etask --iid 123
# 2. cho agent chạy (review trong worktree đúng commit của MR)
python $T/mr-review/tools/review_flow.py work --once
# 3. xem kết quả, duyệt rồi đăng
python $T/mr-review/tools/review_flow.py status
python $T/mr-review/tools/review_post.py render <job>      # xem nội dung sẽ đăng
python $T/mr-review/tools/review_post.py approve <job>     # CHỈ bạn gõ ở terminal thật
python $T/mr-review/tools/review_post.py sweep             # đăng lên GitLab
```
- `REVIEW_AUTO_POST=shadow` (mặc định): agent đánh giá "có được tự đăng không" nhưng **vẫn chờ bạn duyệt**. `auto` mới tự đăng (theo luật trong `review_rules.py`).
- MR có commit mới sau khi review → `review_flow.py reconcile` tự xếp review lại.

**Tự động hoá:** để chạy nền, mở 2 terminal:
```bash
python $T/dev-automation/tools/mr_watch.py --interval 300    # cứ 5 phút tìm MR gắn tên bạn → xếp hàng
python $T/auto-dev/tools/coordinator.py work                 # xử lý hàng đợi liên tục
```

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
- Danh sách tài khoản + thứ tự dùng: `work/agents.json` (gồm 5 tài khoản Orca qua `CLAUDE_CONFIG_DIR`). Tài khoản đầu hết quota → tự chuyển tài khoản kế.
- Dung lượng **đọc thẳng từ Anthropic** cho từng tài khoản (dùng access token có sẵn trong thư mục tài khoản, tối đa 5 phút/lần) — cột "Cập nhật" ghi `API …`.
- Tài khoản có access token **đã hết hạn** thì toolkit KHÔNG tự gia hạn (tránh đụng Orca) → hiện số Orca đọc lần cuối + dòng "token hết hạn". Token được gia hạn khi tài khoản đó được dùng trong Orca / Claude CLI.
- `reset?` = đã qua giờ reset sau lần đọc, số mới chưa biết. `--orca-only` = chỉ dùng số của Orca.

### 4.5 Khi sắp hết quota Claude

```bash
python $T/auto-dev/tools/coordinator.py usage 85     # báo mức dùng hiện tại (%)
```
| Mức | Toolkit tự làm |
|---|---|
| ≥ 80% | dừng làn đề xuất; agent opus → sonnet |
| ≥ 95% | chỉ chạy review MR + việc nhỏ |
| 100% | dừng mọi lượt agent (việc → `paused_quota`, không mất) |

Khi quota hồi: `coordinator.py usage 0` rồi `coordinator.py work` — việc chạy tiếp.
Giới hạn khác: `coordinator.py capacity 2` (số việc song song), `coordinator.py budget --daily-agent-runs 30`.

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

## 5. Khi task bị `blocked` — xử lý thế nào

1. `coordinator.py board` → đọc cột lý do.
2. Nếu là cổng duyệt → chạy lệnh duyệt tương ứng (`review_post.py approve`, `program approve`, `proposals.py decide`).
3. Nếu agent không tự sửa được → sửa tay, rồi `python $T/dev-automation/tools/taskstore.py unblock <id>`.
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
Thứ tự gợi ý: (1) `board`, `proposals.py scan`, `refactor.py survey` — chỉ đọc → (2) `verify.py plan/start` trên một nhánh nhỏ → (3) review 1 MR thật với `REVIEW_AUTO_POST=shadow`.

## 7. Lưu ý

- Lệnh **tốn token**: `work` (khi có việc cần agent), `proposals.py go`, `program` có lát dùng agent.
- Lệnh **người thật thấy**: `review_post.py sweep`, `notifier.py`, `gitlab_api.py create-mr/mr-comment` — hook sẽ hỏi bạn.
- Lệnh duyệt (`approve`, `decide`) **phải gõ ở terminal thật** — agent không gõ thay được (cố ý).
- Chưa chạy nghiệm thu trên hệ thống thật: review MR thật end-to-end (F.4c), deploy/env dev (F.8). Xem `docs/design/CHECKLIST.md`.
