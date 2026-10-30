# Bảng việc nhóm FPT Chat — checklist theo dõi

Thiết kế: [`group-workboard.md`](group-workboard.md) (v3, Codex + Fable ACCEPT). Tiến độ kỹ thuật chi tiết:
[`CHECKLIST.md` §8](CHECKLIST.md). Extension: `D:\Claude-cli\fpt-chat-quick-reply` (git local, `HANDOFF.md`,
`PROTOCOL.md`). Nhóm thử: **New Group** `6883495a299a0f54343608e3`.

Ký hiệu: ✅ xong · 🟡 đang làm / xong một phần · ⬜ chưa làm · ⛔ bị chặn · 👤 việc của BẠN.

**Tình trạng 30/10:** mọi phần tôi làm được mà không cần bạn đã xong (0b → 2b, Codex ACCEPT từng phần). Còn lại
đều chờ bạn: bật / chạy thử (A1), danh bạ + quyền (A2), quyết định & cho phép (A3), repo khác (A4).

---

## A. Việc của bạn (bot không tự làm được)

### A1. Bật những gì đã code xong (chưa chạy thật ở đâu)
- [ ] 👤 **Đưa code lên ubuntu-c trước tiên**: máy Windows đi trước `origin` 13 commit (Pha 0b → 3), ubuntu-c đang ở
      `c974fdb` (= origin) ⇒ `git push` từ Windows rồi `git pull` trên ubuntu-c (supervise tự nạp code mới).
- [ ] 👤 Cài lại extension trong Chrome (`chrome://extensions` → Load unpacked / Reload) → v1.8.0.
- [ ] 👤 Đồng bộ `work/task_lists.json` sang ubuntu-c: `python agentkit.py secret_sync push ubuntu-c --only work/task_lists.json`.
- [ ] 👤 Đặt `FCHAT_BOARD_GROUPS=6883495a299a0f54343608e3` (dashboard → Cài đặt, hoặc
      `python agentkit.py toolkit_settings set FCHAT_BOARD_GROUPS 6883495a299a0f54343608e3`).
- [ ] 👤 Restart tab `group_watch` trên ubuntu-c (đổi `.env` không tự nạp lại; commit mới thì tự nạp).
- [ ] 👤 (khuyên) Watchdog + báo Telegram khi bot treo: lệnh supervise của tab `group_watch` thêm
      `--heartbeat group_watch-6883495a299a0f54343608e3 --stale-after 180 --alert`.
- [ ] 👤 Thử trong nhóm: `@bot ping`, `@bot tôi là ai`, một link MR → thẻ R-n ra sau ≤ 3′ hoặc kèm kết quả.
- [ ] 👤 Sau 1–2 ngày thấy ổn: bật báo lệnh bị lỡ `FCHAT_CATCHUP_REPLY=1` (hiện chỉ ghi log).
- [ ] 👤 Bật bản đồ môi trường: `FCHAT_ENV_REPOS=etask`. Prod ẩn; muốn hiện: `FCHAT_ENV_SHOW_PROD=1`.
      Thử `@bot env etask`, `@bot MR 1395 etask ở đâu?`.
- [ ] 👤 Thêm tab daemon `env_watch` trên ubuntu-c (chạy SHADOW mặc định — không gửi gì):
      `python supervise.py --name env_watch --heartbeat env_watch --stale-after 1800 --alert -- python <toolkit>/.claude/skills/tchat-automation/tools/env_watch.py`
      Sau ~2 ngày: xem `temp/env_watch/shadow.jsonl` có báo sai không → đúng thì đặt `FCHAT_ENV_WATCH_MODE=send`.
      (giờ tổng hợp `FCHAT_ENV_DIGEST_AT`, mặc định 08:30; env gom `FCHAT_ENV_DIGEST_ENVS`, mặc định dev)

### A2. Danh bạ + quyền (`work/team.json`, hiện CHƯA có file ⇒ ai trong nhóm cũng có review + task)
- [ ] 👤 Tạo hồ sơ thành viên: `python agentkit.py team bootstrap --stdin` (từ danh sách participant) hoặc
      `team set <key> --name … --fchat-id … --gitlab … --atask-id …`.
- [ ] 👤 Xác nhận liên kết từng người: `team verify <key> --by <tên bạn>`.
- [ ] 👤 Quyết: ai có `deploy:dev|sandbox|demo`, ai có `approve:<env>` → `team perms <key> --add deploy:dev`.
      (Mặc định ai cũng `review` + `task`; muốn bỏ: `--remove task`.)

### A3. Quyết định / cho phép còn mở
- [x] 👤 **S1** — đã cho phép (30/10). ⛔ vẫn chặn Pha 3/4 vì **chưa kết luận**: Saved Messages đòi nội dung mã hoá,
      server từ chối khung (`oaep decoding error`) trước khi xét định danh, không lưu tin.
- [x] **S1 lần 2 ✅ (30/10)** — nhóm "BOT": server ghi đè `senderId` giả bằng tài khoản của token; tin thử đã thu hồi.
      Kèm S2 ✅ (requestId được trả lại). ⇒ Pha 3/4 hết bị chặn bởi S1.
- [ ] 👤 **S4** — bạn tự sửa / reply / thả reaction / ghim 1 tin trong Saved Messages trong lúc bot nghe WS (chỉ đọc)
      → biết định dạng để sửa thẻ tại chỗ (Pha 5).
- [x] 👤 List aTask: mặc định **#6 Backend Project Task › Task BE**, cho chọn `list: <mã>` trong `work/task_lists.json` (30/10).
- [x] 👤 **Pipeline deploy (S8 ❌):** chọn **(b) cho dev** (30/10) — dev deploy "HEAD lúc chạy" (`allow_unpinned`), bot so
      SHA thực sau build. Sandbox/demo qua chat vẫn chặn tới khi pipeline nhận SHA/tag.
- [x] Giờ tổng hợp: dùng 08:30 (đổi bằng `FCHAT_ENV_DIGEST_AT`).

### A4. Repo khác ngoài etask (registry `work/projects.json`, mục `environments.<env>.envmap`)
Bot cần workload k8s, container chính, tiền tố git tag của MỖI env; etask đã khai (dev `dev.`, demo `dc/idaas-etask` +
`demo.`, sandbox `prod-econ.`). Repo khác: thêm `{"workload": "deployment/<tên>" hoặc "dc/<tên>", "container": "<tên>",
"git_tag_prefix": "<tiền tố>."}` — hoặc bảo tôi dò (pod + GitLab tags + log Jenkins) như etask.
- [ ] 👤 Chọn repo cần theo dõi (ngoài etask) rồi thêm vào `FCHAT_ENV_REPOS`.
- [ ] 👤 Kiểm quyền k8s trên **ubuntu-c**: `python agentkit.py k8s access --project etask --env dev|sandbox|demo`
      (máy Windows: sandbox/demo/prod đọc được, dev lúc được lúc timeout).

---

## B. Tiến độ theo pha

| Pha | Nội dung | Trạng thái | Commit / ghi chú |
|---|---|---|---|
| 0b | Outbox (phân biệt tin bot / chủ nhóm), inbox bền, catch-up không chạy lệnh cũ, heartbeat | ✅ | `d0b06cd` — Codex ACCEPT |
| 1a | Thẻ R-n thay tin "🔎 Review…", không ack ngay, chờ bot 3′, quá hạn 30′, head chưa đổi ⇒ trỏ thẻ cũ | ✅ | `3364820` — Codex ACCEPT (7 vòng) |
| 1b | `review lại R-n`, `@bot tôi là ai`, `@bot ping`, `@bot help`, quyền theo `team.json` | ✅ | `aaf8eb9` — Codex ACCEPT (2 vòng) |
| 2a | `@bot env <repo>`, `@bot MR <số> <repo> ở đâu?` + ✅ qua Jenkins, tag mới hơn, cờ revert | ✅ | `c42c9d1` + `cc61114` — Codex ACCEPT |
| 2b | Thẻ `E-<repo>-<env>` tự động + bản tổng hợp; shadow mặc định | 🟡 | `cc61114` — Codex ACCEPT (3 vòng); còn: chạy shadow 2 ngày (A1) |
| 3 | Task aTask: nháp T-n → `xác nhận T-n` → tạo; `xong T-n` (hỏi lại) | 🟡 | code xong, Codex ACCEPT (3 vòng); còn: 1 task thật bạn xác nhận (A1) |
| 4 | Deploy dev: D-n → người KHÁC duyệt → Jenkins → đợi rollout | ⬜ | S1 ✅; còn chờ quyền deploy/approve (A2); pipeline: (b) dev "HEAD lúc chạy" |
| 5 | Extension bật lệnh mới; sửa thẻ tại chỗ nếu S4 đạt | 🟡 | v1.8.0: bật Task; Deploy chờ pha 4 |

### Chi tiết từng pha
**1b**
- [x] Danh bạ + quyền mặc định (`core/identity.py`), policy review (`core/policy.py`)
- [x] Lệnh neo đầu dòng (`workflows/group_board/commands.py`) + nối vào `group_watch`
- [x] `team.py perms / verify`
- [x] Cảnh báo Telegram khi heartbeat dừng: `supervise.py --alert` (≤ 1 lần/30′/daemon)

**2a — bản đồ môi trường (đọc)**
- [x] Spike S5 GitLab ✅ · S6 k8s ✅ (4 env) · S6 Jenkins ✅ (log có SHA, digest đã push, tag đã push)
- [x] `k8s.py workload` · `jenkins.py provenance` · `integrations/gitlab` (tag, ancestor, port, compare, commit→MR)
- [x] `envmap`: ✅ đã xác minh / 🟡 / ❔ / ⚠ + "MR có ở env X?" 4 mức + giờ quan sát
- [x] "Có bản build mới hơn chưa chạy ở đây" (vd dev.0.0.1274 có tag, pod vẫn 1273) · cờ "có revert — kiểm tay"
- [~] So tay: etask 3 env khớp (sandbox/demo ✅, dev 🟡); repo khác chờ A4

**2b — báo tự động**
- [x] `env_watch.py` (daemon dưới supervise, heartbeat, shadow mặc định, trạng thái riêng theo mode)
- [x] Thẻ E-<repo>-<env> khi đổi phiên bản (MR thêm/mất theo mã R), gửi đủ nhóm mới dời mốc, GitLab lỗi ⇒ hoãn
- [x] Bản tổng hợp dev hằng ngày (lỗi ⇒ thử lại, không trùng)
- [ ] 👤 Shadow 2 ngày trên ubuntu-c → không báo sai → `FCHAT_ENV_WATCH_MODE=send` (A1)

**3 — task aTask** (⛔ S1)
- [x] Spike S7: `create_task` không có tham số người tạo ⇒ thẻ T sẽ ghi "tạo bằng tài khoản <chủ nhóm>"
- [x] Lệnh `@bot tạo task: … | giao: @X | hạn: … | mô tả: … | list: <mã>` → thẻ T nháp; `xác nhận / huỷ / xong / xác nhận xong T-n`; `@bot list task`
- [x] Idempotency (marker mã thẻ trong mô tả) + đối soát mọi trang khi timeout/5xx; đua 2 worker chỉ 1 lượt tạo
- [ ] 👤 1 task thật: trong nhóm gõ `@bot tạo task: Thử bot | giao: tôi` → `xác nhận T-1` → kiểm trên aTask (list Task BE)
- [ ] 👤 Người được giao cần hồ sơ `work/team.json` có `--fchat-id` + `--atask-id` (A2); chưa có ⇒ chỉ tạo không giao

**4 — deploy** (⛔ S1 + pipeline)
- [x] Spike S8: job không nhận SHA/tag, không có job `-ops` ⇒ chờ lựa chọn A3
- [ ] Thẻ D + payload chốt (repo, env, job, nhánh, commit, hash cấu hình)
- [ ] Duyệt 4 mắt (người khác, đã xác minh), một lần, có hạn; bạn duyệt qua Telegram
- [ ] Khoá repo:env, đợi build + rollout, so SHA build thực (đã có `jenkins.py provenance`)

**5 — extension**
- [x] v1.6.0: thẻ chỉ từ bot, nhãn 🤖 Bot / Bạn, chế độ chủ nhóm, thẻ E, banner bot im lặng
- [x] v1.7.0: nút "Review lại R-n", bật `@bot tôi là ai / ping / help`
- [x] v1.7.1: bật `@bot env` / `@bot MR … ở đâu`
- [x] v1.8.0: bật Task (ô List, nút Xong cho cả người yêu cầu, nút Xác nhận xong)
- [x] Đưa thư mục extension vào git (local, `179fb78`)
- [ ] Bật Task / Deploy khi bot hỗ trợ (pha 3/4)

---

## C. Đã biết nhưng chưa xử lý
- Chưa chạy daemon thật với code mới ⇒ chưa có thẻ nào trong nhóm; mọi kiểm chứng là test + chạy thử chỉ đọc / shadow.
- Prod etask 0.197.45 không có git tag ⇒ commit prod = không xác định (prod ẩn mặc định).
- Bản port khác tiêu đề (vd !1395 → !1396) không tự nhận là "nghi port" — trả "không thấy commit gốc" + link so sánh.
- Danh sách MR của một lần đổi env chỉ tính 50 commit mới nhất (ghi rõ trên tin).
- Test đỏ từ trước, không liên quan: `test_store_engine.test_ctx_run_kills_descendants_too`, `test_k8s.test_home`.
- Extension: chưa kiểm bản cài trong Chrome của bạn và nút "Gửi" bằng click thật.
